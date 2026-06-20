"""MorphoDepotLogic RepoClerkMixin (split from MorphoDepot.py)."""
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


class RepoClerkMixin:
    def ghTopicClearCache(self):
        self.gh("config clear-cache")

    REPOCLERK_URL = "https://github.com/MorphoDepot/RepoClerk"
    REPOCLERK_SIZE_LIMIT_MB = 100

    def refreshRepoClerk(self):
        """Maintain a shallow clone of RepoClerk and pull latest journals.
        Returns the clone path on success, or None on failure (triggers fallback to direct API)."""
        clonePath = os.path.join(self.localRepositoryDirectory(), "MorphoDepotCaches", "RepoClerk")
        try:
            if os.path.exists(clonePath):
                total = sum(
                    os.path.getsize(os.path.join(root, f))
                    for root, _, files in os.walk(clonePath)
                    for f in files
                )
                if total / 1e6 > self.REPOCLERK_SIZE_LIMIT_MB:
                    shutil.rmtree(clonePath)
            if not os.path.exists(clonePath):
                subprocess.run(
                    [self.gitExecutablePath, 'clone', '--depth', '1', self.REPOCLERK_URL, clonePath],
                    check=True, capture_output=True
                )
            else:
                subprocess.run(
                    [self.gitExecutablePath, 'pull'],
                    cwd=clonePath, check=True, capture_output=True
                )
            return clonePath
        except Exception as e:
            logging.warning(f"RepoClerk refresh failed: {e}")
            return None

    def repoClerkJournals(self, clonePath):
        """Read all journal JSON files from a RepoClerk clone. Returns list of journal dicts."""
        journals = []
        journalsDir = os.path.join(clonePath, "journals")
        if not os.path.exists(journalsDir):
            return journals
        for entry in os.scandir(journalsDir):
            if entry.name.endswith(".json"):
                try:
                    with open(entry.path) as f:
                        journals.append(json.load(f))
                except Exception as e:
                    logging.warning(f"Failed to read journal {entry.path}: {e}")
        return journals

    def notifyRepoClerk(self, nameWithOwner):
        """Request a RepoClerk journal update by opening an update-request issue.
        Issue creation works for any authenticated GitHub user (no write access to RepoClerk needed).
        The drain loop picks up and closes these issues as it processes them."""
        self.gh([
            "issue", "create",
            "--repo", "MorphoDepot/RepoClerk",
            "--title", f"update {nameWithOwner}",
            "--label", "update-request",
            "--body", "",
        ])

    def hasRepoClerkUpdatePending(self):
        """Returns True if there are open update-request issues in RepoClerk.
        Issues are only closed once the drain loop has finished processing them,
        so this covers the full window from queuing through page rebuild completion."""
        result = self.gh("issue list --repo MorphoDepot/RepoClerk --state open --label update-request --json number")
        if result:
            try:
                return len(json.loads(result)) > 0
            except Exception:
                pass
        return False

    def _journalsToTopicData(self, journals):
        """Convert RepoClerk journal list to the dict shape that ghTopicData() returned."""
        result = []
        for j in journals:
            issues_nodes = [
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "url": issue["url"],
                    "author": {"login": issue["author"]} if issue.get("author") else None,
                    "assignees": {"nodes": [{"login": a} for a in issue.get("assignees", [])]},
                }
                for issue in j.get("openIssues", [])
            ]
            prs_nodes = [
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "isDraft": pr["isDraft"],
                    "url": pr["url"],
                    "author": {"login": pr["author"]} if pr.get("author") else None,
                    "closingIssuesReferences": {
                        "nodes": [
                            {
                                "number": pr["closingIssue"]["number"],
                                "title": pr["closingIssue"]["title"],
                                "repository": {"owner": {"login": pr["closingIssue"]["repoOwner"]}},
                            }
                        ] if pr.get("closingIssue") else []
                    },
                }
                for pr in j.get("openPRs", [])
            ]
            result.append({
                "nameWithOwner": j["nameWithOwner"],
                "curator": j.get("curator"),
                "pullRequests": {"nodes": prs_nodes},
                "issues": {"nodes": issues_nodes},
            })
        return result

    def ghTopicData(self, topic="MorphoDepot"):
        clonePath = self.refreshRepoClerk()
        if clonePath:
            journals = self.repoClerkJournals(clonePath)
            if journals:
                return self._journalsToTopicData(journals)
        logging.warning("RepoClerk journals unavailable — returning empty topic data")
        return []

    def morphoRepos(self):
        clonePath = self.refreshRepoClerk()
        if clonePath:
            journals = self.repoClerkJournals(clonePath)
            if journals:
                result = []
                for j in journals:
                    openIssues = j.get("openIssues", []) or []
                    openPRs = j.get("openPRs", []) or []
                    # Carry issue/PR totalCount + nodes from the journal so the Release tab's counts,
                    # announcement targets, and tooltip are accurate (they previously read 0 / empty).
                    issueNodes = [
                        {"number": i.get("number"), "title": i.get("title"),
                         "author": {"login": i.get("author")} if i.get("author") else None,
                         "assignees": {"nodes": [{"login": a} for a in (i.get("assignees") or [])]}}
                        for i in openIssues
                    ]
                    prNodes = [
                        {"number": p.get("number"), "title": p.get("title"),
                         "isDraft": p.get("isDraft"),
                         "author": {"login": p.get("author")} if p.get("author") else None}
                        for p in openPRs
                    ]
                    result.append({
                        "name": j["nameWithOwner"].split("/")[1],
                        "owner": {"login": j["nameWithOwner"].split("/")[0]},
                        "pushedAt": j.get("pushedAt", ""),
                        "curator": j.get("curator"),
                        "issues": {"totalCount": len(openIssues), "nodes": issueNodes},
                        "pullRequests": {"totalCount": len(openPRs), "nodes": prNodes},
                    })
                return result
        logging.warning("RepoClerk journals unavailable — returning empty repo list")
        return []
