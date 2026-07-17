"""MorphoDepotLogic ControlPlaneMixin (split from MorphoDepot.py)."""
import os
import re
import sys
import csv
import glob
import json
import time
import math
import locale
import random
import shutil
import logging
import platform
import datetime
import fnmatch
import tempfile
import traceback
import subprocess
from contextlib import contextmanager
import git
import requests
import qt
import ctk
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate


class ControlPlaneMixin:
    def controlPlaneBase(self):
        base = qt.QSettings().value(
            "MorphoDepot/controlPlaneBase", "https://join.morphodepot.org").rstrip("/")
        # S5: the user's gh token is sent here as a Bearer credential; refuse a non-HTTPS endpoint so
        # a poisoned/typo'd QSettings value can't exfiltrate it in cleartext or to an http host.
        if not str(base).startswith("https://"):
            raise RuntimeError(f"Refusing non-HTTPS control plane endpoint: {base!r}")
        return base

    def orgMembershipStatus(self, org=None):
        """Tri-state membership against the MorphoDepot org, asked DIRECTLY of GitHub via
        `gh api /user/memberships/orgs/{org}`:
        'member' | 'non_member' | 'unknown' (could not determine — GitHub unreachable, or the token
        lacks read:org).  GitHub is authoritative and fast (~0.5s).  This deliberately does NOT go
        through the control-plane App: the App's `/me` only proxied this same GitHub endpoint with
        its own token (to spare the user's token an org scope), but `gh auth login` already grants
        read:org, and the App's host is intermittently 20-40s just to accept a TCP connection —
        which froze the UI thread for that long on every Create-tab activation.  (The App is still
        used elsewhere — e.g. to read the member's private contact email.)
        Caches only a CONFIRMED result; 'unknown' is never trusted from cache, so a transient failure
        re-checks (and never reports a real member's outage as a membership problem)."""
        org = org or self.morphoDepotOrg
        cache = getattr(self, "_orgMemberCache", None)
        if cache is not None and cache[0] == org and cache[1] in ("member", "non_member"):
            return cache[1]
        try:
            # GitHub returns the caller's OWN membership: state 'active' (full member) or 'pending'
            # (invited, not yet accepted — treated as not-yet-a-member, matching the App's prior
            # is_active_member semantics).
            # Bound the timeout: this runs on the UI thread at module enter() to drive gating, so a
            # dead network must degrade to 'unknown' (fail-open) in seconds, never hang the module.
            state = (self.gh(["api", f"/user/memberships/orgs/{org}", "--jq", ".state"],
                             timeout=10, quietErrors=True) or "").strip()
            if state == "active":
                status = "member"
            elif state:
                # Any other non-empty state ('pending' = invited-not-yet-accepted, or any future
                # GitHub state) is deliberately treated as not-yet-a-member, not an error.
                status = "non_member"
            else:
                # exit 0 but no state (a 200 with an unexpected JSON shape) is indeterminate, NOT a
                # confirmed non-member — return 'unknown' without caching, so a malformed response
                # can't silently lock out a real member until the next module reload.
                return "unknown"
        except Exception as e:
            # A 404 from this endpoint is authoritative: the user is not a member of the org (gh
            # prints the literal "HTTP 404" on its non-zero exit).  Any other failure (network,
            # missing read:org scope) is genuinely 'unknown'.  Do NOT cache 'unknown' — it would
            # also evict a prior confirmed result, and a transient failure must re-check on the next
            # call rather than stick.
            if "HTTP 404" in str(e):
                status = "non_member"
            else:
                logging.warning(f"Membership check failed (status unknown): {e}")
                return "unknown"
        self._orgMemberCache = (org, status)
        return status

    def userIsOrgMember(self, org=None):
        """True only when membership is CONFIRMED.  Boolean callers treat 'unknown' as non-member;
        the create path uses orgMembershipStatus() directly to distinguish 'not a member' from
        'could not verify' so a transient outage is never reported as a membership problem.  (Asks
        GitHub directly via orgMembershipStatus(); reload the module after switching gh accounts or
        joining the org so the cached result refreshes.)"""
        return self.orgMembershipStatus(org) == "member"

    def controlPlaneRequest(self, path, body, timeout=120):
        """POST to the intake App control plane, authenticated by the user's gh token.
        Returns the parsed JSON, or raises with the server's error detail.  `timeout` (seconds) can
        be shortened for best-effort calls so a slow/down App doesn't stall the UI for the full
        default."""
        import requests
        token = self._ghToken()
        if not token:
            raise RuntimeError("Not signed in to GitHub — run `gh auth login` first.")
        r = requests.post(f"{self.controlPlaneBase()}/{path}", json=body,
                          headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"Control plane '{path}' failed ({r.status_code}): {detail}")
        return r.json()

    # --- reviewer tools (repoadmin-gated; org-design Sec.11.6) ---
    # The org team whose members may use the App-proxied candidate-inspection tools. It is an
    # AUTHORIZATION LIST the App checks, never a repo grant. The App is the hard gate; this
    # client-side check only decides whether to SHOW the reviewer UI.
    repoAdminTeam = "repoadminteam"

    def repoAdminStatus(self, forceRefresh=False):
        """Whether the active user is a member of the reviewer team (repoadminteam), asked DIRECTLY
        of GitHub (fast; avoids the intermittently-slow App). Returns 'yes' | 'no' | 'unknown'.
        Caches only a CONFIRMED result. Used to show/hide the Review-tab reviewer tools — fail-CLOSED
        for the UI (a non-'yes' hides the section); the App enforces access regardless (Sec.11.6).
        ``forceRefresh`` drops the cache and re-queries GitHub, so adding/removing the user from the
        team is reflected on the next Review-tab Refresh without a full module reload."""
        org = self.morphoDepotOrg
        team = self.repoAdminTeam
        if forceRefresh:
            self._repoAdminCache = None
        cache = getattr(self, "_repoAdminCache", None)
        if cache is not None and cache[0] == (org, team) and cache[1] in ("yes", "no"):
            return cache[1]
        me = self.whoami()
        try:
            # 'active' = a member OR maintainer of the team (both count); 'pending' = invited but not
            # yet accepted (not a reviewer yet). A 404 exits non-zero with "HTTP 404" = not on the team.
            state = (self.gh(["api", f"/orgs/{org}/teams/{team}/memberships/{me}", "--jq", ".state"],
                             timeout=10, quietErrors=True) or "").strip()
            if state == "active":
                status = "yes"
            elif state:
                status = "no"
            else:
                # exit 0 but no state (unexpected shape) -> indeterminate; don't cache.
                return "unknown"
        except Exception as e:
            if "HTTP 404" in str(e):
                status = "no"
            else:
                logging.warning(f"repoadmin check failed (status unknown): {e}")
                return "unknown"
        self._repoAdminCache = ((org, team), status)
        return status

    def isRepoAdmin(self, forceRefresh=False):
        """True only when repoadminteam membership is CONFIRMED (fail-closed: 'unknown' -> False).
        ``forceRefresh`` re-queries GitHub (used when re-evaluating the reviewer-section visibility
        on a manual Refresh, so a team change takes effect without a module reload)."""
        return self.repoAdminStatus(forceRefresh=forceRefresh) == "yes"

    def reviewQueue(self):
        """The reviewer queue from the App (repoadmin-gated + staging-only server-side): a list of
        staged candidates awaiting review. Raises on transport/policy error so the caller surfaces it."""
        result = self.controlPlaneRequest("reviews/queue", {}, timeout=60)
        return result.get("candidates", []) if isinstance(result, dict) else []

    def inspectCandidate(self, repoName):
        """Fetch the App-served review payload for a staged candidate (repoadmin-gated, staging-only,
        read-only server-side). `repoName` is the bare repo name; returns the inspection payload
        (source_volume pointer + checksum, color table, segmentation(s))."""
        return self.controlPlaneRequest("reviews/inspect", {"repo": repoName}, timeout=120)

    def reviewStatus(self, repoNames):
        """The caller's own per-repo review state {name: 'approved'|'pending'|'none'} from the control
        plane, used to label the unpublished list.  Best-effort -> {} on any error so the list still
        renders (e.g. a non-member, or the App being briefly unreachable)."""
        if not repoNames:
            return {}
        try:
            result = self.controlPlaneRequest("repos/review-status", {"repos": list(repoNames)})
            # Defensive: only a dict is usable by the caller (statuses.get(...)).  Any other valid-but-
            # unexpected JSON (list/bool/str) would otherwise crash the whole list render, not degrade.
            return result if isinstance(result, dict) else {}
        except Exception as e:
            logging.warning(f"Could not fetch review status: {e}")
            return {}
