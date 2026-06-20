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
        """Tri-state membership against the MorphoDepot org via the App control plane (`/me`):
        'member' | 'non_member' | 'unknown' (could not determine — App unreachable / invalid token).
        The App verifies membership with its OWN token, so the user's gh token needs no org scope.
        Caches only a CONFIRMED result; 'unknown' is never trusted from cache, so a transient failure
        re-checks (and never reports a real member's outage as a membership problem)."""
        org = org or self.morphoDepotOrg
        cache = getattr(self, "_orgMemberCache", None)
        if cache is not None and cache[0] == org and cache[1] in ("member", "non_member"):
            return cache[1]
        try:
            info = self.controlPlaneRequest("me", {})
            status = "member" if bool(info.get("is_member")) else "non_member"
        except Exception as e:
            # Do NOT cache 'unknown' (it would also evict a prior confirmed result) — a transient
            # failure must re-check on the next call rather than stick.
            logging.warning(f"Membership check failed (status unknown): {e}")
            return "unknown"
        self._orgMemberCache = (org, status)
        return status

    def userIsOrgMember(self, org=None):
        """True only when membership is CONFIRMED.  Boolean callers treat 'unknown' as non-member;
        the create path uses orgMembershipStatus() directly to distinguish 'not a member' from
        'could not verify' so a transient outage is never reported as a membership problem.  (Asks
        the App `/me`, which checks membership with the App token — the user's gh token needs no
        org scope.  Reload the module after switching gh accounts or joining the org.)"""
        return self.orgMembershipStatus(org) == "member"

    def controlPlaneRequest(self, path, body):
        """POST to the intake App control plane, authenticated by the user's gh token.
        Returns the parsed JSON, or raises with the server's error detail."""
        import requests
        token = self._ghToken()
        if not token:
            raise RuntimeError("Not signed in to GitHub — run `gh auth login` first.")
        r = requests.post(f"{self.controlPlaneBase()}/{path}", json=body,
                          headers={"Authorization": f"Bearer {token}"}, timeout=120)
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"Control plane '{path}' failed ({r.status_code}): {detail}")
        return r.json()
