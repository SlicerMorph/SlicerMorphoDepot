"""MorphoDepotLogic ContributeMixin (split from MorphoDepot.py)."""
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


class ContributeMixin:
    # What git says when nothing could supply a GitHub credential and it has no one to ask.
    #
    # Only the FIRST of these is load-bearing, and it is worth knowing why it is safe: git raises
    # it from prompt.c as die("could not read %s%s", ...), which upstream does NOT wrap in _().
    # That string is therefore never translated, so matching it survives a non-English install --
    # the kind of assumption someone later "fixes" by forcing a locale, so it is written down.
    #
    # The other two are historical and defensive.  "terminal prompts disabled" is the tail of the
    # same message once GIT_TERMINAL_PROMPT=0 is in force (see gitNonInteractiveEnvironment), and
    # "failed to execute prompt script" is a Git-for-Windows flavor, not an upstream string at all
    # -- current git emits "unable to read askpass response from ..." or "cannot exec ..." there.
    # Those are deliberately absent: every one of them is accompanied by "could not read Username"
    # on the following line, so the first marker already covers them.
    #
    # Matched so the user gets told what to fix instead of a traceback, at the one moment that is
    # most expensive: the end of a segmentation session.
    missingCredentialMarkers = (
        "could not read Username",
        "terminal prompts disabled",
        "failed to execute prompt script",
    )

    def isMissingCredentialError(self, error):
        """True when a git failure is 'no GitHub credential available', not a real push rejection."""
        text = f"{getattr(error, 'stderr', '') or ''}\n{error}"
        return any(marker in text for marker in self.missingCredentialMarkers)

    def commitAndPush(self, message):
        """Create a PR if needed and push current segmentation
        Mark the PR as a draft
        """
        if not self.segmentationNode:
            return False
        if not slicer.util.saveNode(self.segmentationNode, self.segmentationPath, properties={'useCompression': True}):
            logging.error(f"Segmentation save failed: path is {self.segmentationPath}")
            return False
        self.localRepo.index.add([self.segmentationPath])
        self.localRepo.index.commit(message)

        branchName = self.localRepo.active_branch.name
        remote = self.localRepo.remote(name="origin")

        # The segmentation is saved and committed locally by this point, so anything that fails
        # from here on has cost the user nothing but the upload -- which is what the message says.
        try:
            # rebase branch if it exists in case other changes have been made (e.g. on another machine)
            branchNames = [branch.name.split("/")[1] for branch in self.localRepo.remotes['origin'].refs]
            if branchName in branchNames:
                pullResult = self.localRepo.git.pull(f"--rebase", "origin", branchName)
                self.progressMethod(pullResult)

            # Workaround for missing origin.push().raise_if_error() in 3.1.14
            # (see https://github.com/gitpython-developers/GitPython/issues/621):
            # https://github.com/gitpython-developers/GitPython/issues/621
            pushInfoList = remote.push(branchName)
        except git.exc.GitCommandError as e:
            if not self.isMissingCredentialError(e):
                raise
            # This repository is configured to sign in through gh (configureRepositoryCredentials),
            # so reaching here means gh itself could not produce a credential -- signed out, or a
            # token that has expired or been revoked.  There is nothing about git configuration
            # for the user to fix, so the message names the one thing that does help.
            raise RuntimeError(
                "Your segmentation was saved on this computer, but could not be sent to GitHub "
                "because MorphoDepot could not sign in. Nothing has been lost -- the work is "
                "here and will go up once you are signed in again.\n\n"
                "Open a terminal window, run:  gh auth login\n"
                "and complete every step, then come back and click Commit again.") from e
        for pi in pushInfoList:
            for flag in [pi.REJECTED, pi.REMOTE_REJECTED, pi.REMOTE_FAILURE, pi.ERROR]:
                if pi.flags & flag:
                    self.progressMethod(f"Push failed with {flag}")
                    return False

        # Create a PR if one does not already exist.  Check AUTHORITATIVELY (direct gh), NOT via the
        # RepoClerk journal that issuePR()/prList() read: the journal lags minutes behind GitHub, so
        # right after the first push it still reports "no PR" and the old `if not self.issuePR()`
        # guard would try to open a SECOND PR for the same head->base.  gh rejects that ("a pull
        # request ... already exists"), which surfaced as a spurious "Failed to commit and push"
        # even though the push succeeded -- for repo owners and outside segmenters alike.
        upstreamNameWithOwner = self.nameWithOwner("upstream")
        originNameWithOwner = self.nameWithOwner("origin")
        originOwner = originNameWithOwner.split("/")[0]
        if not self._openPRForBranch(upstreamNameWithOwner, originOwner, branchName):
            # S11: branchName is normally "issue-<issueNumber>" (from loadIssue); guard a malformed
            # name instead of IndexError-ing out of the push handler.
            parts = branchName.split("-")
            issueNumber = parts[1] if len(parts) > 1 else None
            prBody = f"Fixes #{issueNumber}" if issueNumber else "Resolves the linked issue."
            if self.currentIssue and 'author' in self.currentIssue and 'login' in self.currentIssue['author']:
                authorLogin = self.currentIssue['author']['login']
                prBody = f"Started work on this issue for @{authorLogin}. {prBody}"
            commandList = ["pr", "create", "--draft",
                           "--repo", upstreamNameWithOwner,
                           "--base", "main",
                           "--title", branchName,
                           "--head", f"{originOwner}:{branchName}",
                           "--body", prBody]
            try:
                self.gh(commandList)
            except RuntimeError as e:
                # Backstop for the race between the check above and now: gh refuses a duplicate
                # head->base PR.  The push already updated that PR, so "already exists" is success;
                # re-raise anything else.
                if "already exists" not in str(e):
                    raise
                self.progressMethod("A pull request for this issue already exists; your push updated it.")
            try:
                self.notifyRepoClerk(upstreamNameWithOwner)
            except Exception as e:
                logging.warning(f"Could not notify RepoClerk: {e}")
        return True

    def requestReview(self):
        upstreamNameWithOwner = self.nameWithOwner("upstream")
        pr = self.issuePR(role="segmenter")
        if not pr:
            # The RepoClerk journal that issuePR() reads lags, so a just-opened PR may not appear
            # there yet -- fall back to an authoritative direct lookup before giving up (same root
            # cause as the commitAndPush duplicate-PR bug).
            branchName = self.localRepo.active_branch.name
            originOwner = self.nameWithOwner("origin").split("/")[0]
            pr = self._openPRForBranch(upstreamNameWithOwner, originOwner, branchName)
        if not pr:
            logging.error("No pull request found for the current issue branch.")
            return

        self.gh(["pr", "ready", str(pr['number']), "--repo", upstreamNameWithOwner])
        try:
            self.notifyRepoClerk(upstreamNameWithOwner)
        except Exception as e:
            logging.warning(f"Could not notify RepoClerk: {e}")

    def requestChanges(self, message=""):
        pr = self.issuePR(role="reviewer")
        if not pr:
            branch = self.localRepo.active_branch.name if self.localRepo else "this branch"
            raise RuntimeError(
                f"No open pull request found for '{branch}'. It may already be merged or closed "
                f"(the Review list can lag a minute behind GitHub). Click 'Refresh Github' to update it.")
        upstreamNameWithOwner = self.nameWithOwner("upstream")
        # GitHub forbids submitting a review (--request-changes) on your OWN pull request, exactly like
        # --approve (see approvePR).  When the reviewer is also the PR author (their own contribution,
        # or testing), post the feedback as a plain comment instead; the PR is still set back to draft
        # below so the contributor knows to revise.
        me = self.whoami()
        selfAuthored = (pr.get("author") or {}).get("login") == me
        if selfAuthored:
            # Always post a comment -- an empty message would otherwise set the PR back to draft
            # (below) with zero feedback to the contributor.
            body = message if message != "" else "Changes requested (no additional comment)."
            self.gh(["pr", "comment", str(pr['number']), "--repo", upstreamNameWithOwner,
                     "--body", body])
        else:
            commandList = ["pr", "review", str(pr['number']), "--request-changes",
                           "--repo", upstreamNameWithOwner]
            if message != "":
                commandList += ["--body", message]
            else:
                commandList += ["--no-body"]   # else recent gh opens an editor + hangs headless
            self.gh(commandList)
        self.gh(["pr", "ready", str(pr['number']), "--undo", "--repo", upstreamNameWithOwner])
        try:
            self.notifyRepoClerk(upstreamNameWithOwner)
        except Exception as e:
            logging.warning(f"Could not notify RepoClerk: {e}")

    def approvePR(self, message=""):
        pr = self.issuePR(role="reviewer")
        if not pr:
            branch = self.localRepo.active_branch.name if self.localRepo else "this branch"
            raise RuntimeError(
                f"No open pull request found for '{branch}'. It may already be merged or closed "
                f"(the Review list can lag a minute behind GitHub). Click 'Refresh Github' to update it.")
        upstreamNameWithOwner = self.nameWithOwner("upstream")
        # GitHub forbids approving your OWN pull request (addPullRequestReview fails with
        # "Can not approve your own pull request"), regardless of repo role — an "approve" review
        # is by definition independent sign-off.  When the curator/reviewer is also the PR author
        # (their own baseline contribution, or testing), skip the approval review and just merge:
        # merging your own PR IS allowed for write/admin, only the review verdict is restricted.
        me = self.whoami()
        selfAuthored = (pr.get("author") or {}).get("login") == me
        if not selfAuthored:
            commandList = ["pr", "review", str(pr['number']), "--approve",
                           "--repo", upstreamNameWithOwner]
            if message != "":
                commandList += ["--body", message]
            else:
                commandList += ["--no-body"]   # else recent gh opens an editor + hangs headless
            self.gh(commandList)
        commandList = ["pr", "merge", str(pr['number']), "--repo", upstreamNameWithOwner, "--squash"]
        commandList += ["--body", message if (selfAuthored and message) else "Merging and closing"]
        self.gh(commandList)
        try:
            self.notifyRepoClerk(upstreamNameWithOwner)
        except Exception as e:
            logging.warning(f"Could not notify RepoClerk: {e}")
