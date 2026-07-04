"""MorphoDepotLogic SearchMixin (split from MorphoDepot.py)."""
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


class SearchMixin:
    def refreshSearchCache(self):
        """Gets accession data from all repositories via RepoClerk journals."""
        # Reset up front so a failed refresh yields an empty cache (search then returns nothing)
        # rather than silently serving stale results from a previous successful run.
        self.repoDataByNameWithOwner = {}
        clonePath = self.refreshRepoClerk()
        if clonePath:
            journals = self.repoClerkJournals(clonePath)
            if journals:
                for j in journals:
                    try:
                        owner, repo = j["nameWithOwner"].split("/", 1)
                        key = f"{owner}^{repo}"
                        repoData = dict(j.get("accession", {}))
                        repoData["pushedAt"] = j.get("pushedAt", "")
                        repoData["screenshotCount"] = j.get("screenshotCount", 0)
                        repoData["screenshotCaptions"] = j.get("screenshotCaptions", [])
                        repoData["volumeSize"] = j.get("volumeSize")
                        self.repoDataByNameWithOwner[key] = repoData
                        self.progressMethod(f"Loaded {key} from RepoClerk")
                    except Exception as e:
                        logging.warning(f"Could not process journal {j.get('nameWithOwner', '?')}: {e}")
                self.progressMethod("Finished loading from RepoClerk")
                return
        logging.warning("RepoClerk journals unavailable — search cache not populated")

    def search(self, criteria):
        if self.repoDataByNameWithOwner == {}:
            return {}

        excludedRepos = set()
        for nameWithOwner, repoData in self.repoDataByNameWithOwner.items():
            for question in criteria:
                # Repository tier is decided by OWNER, not the self-declared accession repoType: a
                # repo is "Archival" only if it lives in the MorphoDepot org (the gated, reviewed
                # home); everything else (personal accounts, other orgs) is "Personal".  This ignores
                # what a repo *claims* in its accession, so old personal test repos that picked
                # "Archival" are correctly classified as Personal.  (nameWithOwner is "{owner}^{repo}".)
                if question == "tier":
                    tier = "Archival" if nameWithOwner.split("^", 1)[0] == self.morphoDepotOrg else "Personal"
                    if tier not in criteria["tier"]:
                        excludedRepos.add(nameWithOwner)
                    continue

                if question == "subjectType":
                    # if subjectType is not present, assume "Biological specimen"
                    repoValue = repoData.get("subjectType", (None, "Biological specimen"))[1]
                    if repoValue not in criteria["subjectType"]:
                        excludedRepos.add(nameWithOwner)
                    continue

                # Handle other criteria
                if question in repoData:
                    repoValue = repoData[question][1]
                    if isinstance(repoValue, list):
                        # Exclude only if the repo HAS value(s) for this field and NONE match the
                        # criteria — decided AFTER scanning all values.  (The old in-loop check
                        # excluded on the first non-match, and a set can't un-exclude a later match.)
                        # An EMPTY/unspecified multi-select does NOT exclude the repo — mirroring the
                        # scalar branch below (which ignores an empty value).  Otherwise a repo that
                        # simply left this field blank (e.g. a whole-specimen atlas with no
                        # anatomicalAreas) becomes invisible in EVERY search, even the default browse.
                        if repoValue and not any(value in criteria[question] for value in repoValue):
                            excludedRepos.add(nameWithOwner)
                    else:
                        if repoValue != "" and repoValue not in criteria[question]:
                            excludedRepos.add(nameWithOwner)

        matchString = f"*{criteria['freeText'].lower()}*"
        matchingRepos = set()
        textFields = ["githubRepoName", "species"]
        if "Other" in criteria.get("subjectType", []):
            textFields.append("otherSubjectDescription")
        for nameWithOwner, repoData in self.repoDataByNameWithOwner.items():
            if fnmatch.fnmatch(nameWithOwner, matchString):
                matchingRepos.add(nameWithOwner)
            for textField in textFields:
                if textField in repoData:
                    if fnmatch.fnmatch(repoData[textField][1].lower(), matchString):
                        matchingRepos.add(nameWithOwner)

        results = {}
        for nameWithOwner in matchingRepos:
            if nameWithOwner not in excludedRepos:
                results[nameWithOwner] = self.repoDataByNameWithOwner[nameWithOwner]

        return results
