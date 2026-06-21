"""MorphoDepotLogic GitHubMixin (split from MorphoDepot.py)."""
import os
import re
import sys
import csv
import glob
import json
import time
import math
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


class GitHubMixin:
    def gh(self, command, timeout=300):
        """Execute `gh` command.  Multiline input string accepted for readablity.
        Do not include `gh` in the command string.  `timeout` (seconds) bounds each attempt so a
        hung/auth-prompting child can't block the UI thread; pass timeout=None for the few genuinely
        long operations (large release upload/download)."""
        if not self.ghExecutablePath or self.ghExecutablePath == "":
            logging.error("Error, gh not found")
            return "Error, gh not found"
        if command.__class__() == "":
            commandList = command.replace("\n", " ").split()
        elif command.__class__() == []:
            commandList = command
        else:
            raise TypeError("gh command must be a string or list")
        self.progressMethod(" ".join(commandList))
        fullCommandList = [self.ghExecutablePath] + commandList

        baseDelay = 1
        attempts = 4
        for attempt in range(attempts):
            # S6: give gh a UTF-8 locale via the child's environment.  The previous code mutated the
            # Python process C-locale, which (a) raised locale.Error and broke every gh call on hosts
            # where en_US.UTF-8 isn't generated (minimal Linux/CI), and (b) doesn't even propagate to
            # the spawned child -- the subprocess env is what gh actually reads.
            process = slicer.util.launchConsoleProcess(
                fullCommandList,
                updateEnvironment={"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})
            try:
                # S7: a hung or auth-prompting gh child must not block the Slicer UI thread forever.
                result = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.communicate()
                except Exception:
                    pass
                # S7: a timeout is fatal (not retried, unlike the transient 503 below) -- a process
                # still alive after `timeout`s is not a transient condition.
                raise RuntimeError(f"gh command timed out after {timeout}s: {' '.join(commandList)}")
            # S11: gh writes errors to stderr, so the 503 retry signal can be on either stream.
            combined = (result[0] or "") + (result[1] or "")
            needRetry = "error: 503" in combined or "HTTP 503" in combined
            if process.returncode == 0 or not needRetry:
                if attempt > 0:
                    print(f"Command succeeded after try {attempt}")
                break
            error_message = f"gh command failed: {' '.join(commandList)}\nCode: {process.returncode}, Output: {result}"
            print(error_message)
            delay = baseDelay * (2 ** attempt)
            self.progressMethod(f"gh returned {process.returncode}, sleeping {delay} seconds before retry")
            time.sleep(delay)
        if process.returncode != 0:
            error_message = f"gh command failed: {' '.join(commandList)}\nOutput: {result}"
            logging.error(error_message)
            self.progressMethod(f"gh command error: {result}")
            raise RuntimeError(error_message)
        self.progressMethod(f"gh command finished: {result}")
        return result[0]

    def ghJSON(self, command):
        """Wrapper around gh that returns json loaded data or an empty list on error"""
        jsonString = self.gh(command)
        if jsonString:
            try:
                return json.loads(jsonString)
            except (ValueError, json.JSONDecodeError):
                logging.warning(f"ghJSON: could not parse gh output for {command!r}")
        return []

    def getGitConfig(self, key):
        """Get a value from the global git config."""
        if not self.gitExecutablePath:
            return ""
        try:
            command = [self.gitExecutablePath, 'config', '--global', key]
            result = subprocess.check_output(command, universal_newlines=True, timeout=10).strip()
            return result
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def setGitConfig(self, key, value):
        """Set a value in the global git config."""
        if not self.gitExecutablePath:
            return
        try:
            subprocess.check_call([self.gitExecutablePath, 'config', '--global', key, value], timeout=10)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
            logging.error(f"Failed to set git config {key}: {e}")

    def hasWorkflowScope(self):
        """True if the active gh token carries the `workflow` OAuth scope, which GitHub requires
        to push or create files under `.github/workflows/`.  Used to gate the optional auto-assign
        workflow at repo creation: without it, a push that includes a workflow file is rejected,
        so the option is offered only when the scope is present.  Reads GitHub's authoritative
        `X-Oauth-Scopes` response header via `gh api -i`."""
        try:
            output = self.gh("api -i user")
        except Exception as e:
            logging.warning(f"Could not determine gh token scopes: {e}")
            return False
        for line in output.splitlines():
            if line.lower().startswith("x-oauth-scopes:"):
                scopes = [s.strip() for s in line.split(":", 1)[1].split(",")]
                return "workflow" in scopes
        return False

    def whoami(self):
        """ Get the active gh login.  Parse the '... account <login> ...' token by regex (robust to
        gh's status-line wording) rather than a fixed word index; fall back to the stable API."""
        status = self.gh("auth status --active") or ""
        match = re.search(r"account\s+(\S+)", status)
        if match:
            return match.group(1)
        return self.gh("api user --jq .login").strip()

    def ghUserProfile(self):
        """Return {login, name, email} for the active gh account.

        `email` is the account's verified **primary** email from `user/emails`, so it is
        available even when the user keeps their profile email private — this needs the
        `user:email` scope, which the prerequisites instruct everyone to grant
        (`gh auth login -s user:email`).  Falls back to the public profile email (`gh api user`)
        if `user/emails` is unavailable (e.g. the scope was not granted), then to empty.
        Returns empty strings on any error so callers can fall back to asking the user."""
        try:
            data = self.ghJSON(["api", "user"])
        except Exception as e:
            logging.warning(f"Could not fetch gh user profile: {e}")
            return {"login": "", "name": "", "email": ""}
        if not isinstance(data, dict):
            return {"login": "", "name": "", "email": ""}
        # Verified primary email (works for private emails) via the user:email scope.
        email = ""
        try:
            emails = self.ghJSON(["api", "user/emails"])
            if isinstance(emails, list):
                primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")), None)
                if primary:
                    email = primary.get("email") or ""
        except Exception as e:
            logging.warning(f"Could not read user/emails — is the 'user:email' scope granted? ({e})")
        if not email:
            email = data.get("email") or ""  # fall back to the public profile email
        return {
            "login": data.get("login") or "",
            "name": data.get("name") or "",
            "email": email,
        }

    def userOrganizations(self):
        """Return the list of GitHub organization logins the active gh user belongs to.

        A user may belong to several organizations; all are returned so the Create tab
        can offer them as repository destinations.  Returns an empty list on any error
        (e.g. gh not configured) so callers can fall back to the personal account.
        """
        try:
            data = self.ghJSON(["api", "user/orgs", "--paginate"])
        except Exception as e:
            logging.warning(f"Could not fetch user organizations: {e}")
            return []
        if not isinstance(data, list):
            return []
        return [org["login"] for org in data if isinstance(org, dict) and "login" in org]

    def repoExists(self, nameWithOwner):
        """Read-only check: does a repo at {owner}/{name} exist and is it visible to us?

        Used as the pre-create / pre-publish collision preflight.  Creates nothing.  Returns
        True if the GET succeeds, False on a 404 (or any error, treated as 'available')."""
        try:
            self.gh(["api", f"/repos/{nameWithOwner}", "--silent"])
            return True
        except RuntimeError:
            return False

    def setRepoVisibility(self, nameWithOwner, public=True):
        """Flip a repository's visibility via the REST API.

        Using the API avoids `gh repo edit --visibility`'s required
        --accept-visibility-change-consequences confirmation flag."""
        privateValue = "false" if public else "true"
        self.gh(["api", "--method", "PATCH", f"/repos/{nameWithOwner}",
                 "-F", f"private={privateValue}"])

    def addMorphoTopics(self, nameWithOwner, speciesTopicString):
        """Publish topic transition (last step of go-live): add the discoverability topic(s)
        and REMOVE the `morphodepot-staging` marker, so the repo becomes findable by RepoClerk
        and the extension and simultaneously leaves the unpublished list.  The species topic is
        skipped when unknown (e.g. recovered repos with no species)."""
        command = ["repo", "edit", nameWithOwner, "--add-topic", "morphodepot",
                   "--remove-topic", self.stagingTopic]
        if speciesTopicString:
            command = ["repo", "edit", nameWithOwner, "--add-topic", "morphodepot",
                       "--add-topic", f"md-{speciesTopicString}", "--remove-topic", self.stagingTopic]
        self.gh(command)

    def issueList(self):
        me = self.whoami()
        repoData = self.ghTopicData()
        issueList = []
        for repo in repoData:
            for issue in repo['issues']['nodes']:
                assignees = [node['login'] for node in issue['assignees']['nodes']]
                if me in assignees:
                    repoName = repo['nameWithOwner'].split("/")[1]
                    issueList.append({'number': issue['number'],
                                      'title': issue['title'],
                                      'repository': { 'name': repoName, 'nameWithOwner': repo['nameWithOwner']}})
        return issueList

    def administratedRepoList(self):
        # Releases are ARCHIVAL-ONLY (org-design Sec.9.6): only org repos the user curates can be
        # released, so the Release tab lists only those.  Personal short-term repos are excluded —
        # they cannot be released, and showing them is misleading.  A member's archival repo is owned
        # by the MorphoDepot org (owner != me); the journaled CURATOR (RepoClerk schema v3) is the
        # authoritative "this is mine to release" signal.  (curator is None on pre-v3 journals, so an
        # org repo lights up once RepoClerk re-drains with the field.)
        me = self.whoami()
        returnRepos = []
        for repo in self.morphoRepos():
            ownerLogin = repo['owner']['login']
            isArchival = ownerLogin != me           # archival repos live in the org, not on my account
            if isArchival and repo.get('curator') == me:
                repo['nameWithOwner'] = f"{ownerLogin}/{repo['name']}"
                returnRepos.append(repo)
        return returnRepos

    def prList(self, role="segmenter"):
        """
        Fetch a list of open pull requests for the user, either as 'segmenter' or reviewer.
        Returns PRs, their associated issue titles, and repository topics.
        """
        me = self.whoami()
        repoData = self.ghTopicData()
        prList = []
        for repo in repoData:
            for pr in repo['pullRequests']['nodes']:
                prAuthor = (pr.get('author') or {}).get('login')  # None for a deleted/ghost account
                if role == "segmenter":
                    parties = [prAuthor] if prAuthor else []
                elif role == "reviewer":
                    parties = [issue['repository']['owner']['login'] for issue in pr['closingIssuesReferences']['nodes']]
                    # In-org (archival) repos are owned by the MorphoDepot org, not the curator's
                    # login, so owner-keying alone hides every in-org PR from its curator.  Add the
                    # journaled curator — the same owner-vs-curator fix administratedRepoList() applies.
                    if repo.get('curator'):
                        parties.append(repo['curator'])
                else:
                    raise BaseException(f"Unknown role {role}")
                issueTitles = [issue['title'] for issue in pr['closingIssuesReferences']['nodes']]
                if me in parties:
                    repoName = repo['nameWithOwner'].split("/")[1]
                    prList.append({'number': pr['number'],
                                      'title': pr['title'],
                                      'issueTitles': issueTitles,
                                      'isDraft': pr['isDraft'],
                                      'author': {'login': prAuthor},
                                      'repository': { 'name': repoName, 'nameWithOwner': repo['nameWithOwner']}})
        return prList

    def repositoryList(self):
        # --limit 1000: gh defaults to 30, which would make loadIssue's fork-exists check
        # (forkExists = name in repositoryList) miss a real fork for users with many repos.
        repositories = self.ghJSON("repo list --json name --limit 1000") or []
        repositoryList = [r['name'] for r in repositories]
        return repositoryList

    def nameWithOwner(self, remote):
        branchName = self.localRepo.active_branch.name
        repo = self.localRepo.remote(name=remote)
        repoURL = list(repo.urls)[0]
        if repoURL.find("@") != -1:
            # git ssh prototocol
            repoURL = "/".join(repoURL.split(":"))
            repoNameWithOwner = "/".join(repoURL.split("/")[-2:]).split(".")[0]
        elif repoURL.startswith("https://"):
            # https protocol
            repoNameWithOwner = "/".join(repoURL.split("/")[-2:]).split(".")[0]
        elif repoURL.startswith("git@"):
            # git@github.com:owner/repo.git
            repoNameWithOwner = repoURL.split(":")[1].replace(".git", "")
        elif os.path.exists(repoURL):
            # local path
            # this case happens during repo creation before pushing to remote
            return None
        else:
            # https protocol
            repoNameWithOwner = "/".join(repoURL.split("/")[-2:]).split(".")[0]
        return repoNameWithOwner

    def issuePR(self, role="segmenter"):
        """Find the issue for the issue currently being worked on or None if there isn't one"""
        if role not in ("segmenter", "reviewer"):
            raise ValueError(f"Invalid role {role}")
        if not self.localRepo:
            return None
        branchName = self.localRepo.active_branch.name
        try:
            upstreamNameWithOwner = self.nameWithOwner("upstream")
        except ValueError:
            return None
        issuePR = None
        prs = self.prList(role=role)
        for pr in prs:
            prRepoNameWithOwner = pr['repository']['nameWithOwner']
            if prRepoNameWithOwner == upstreamNameWithOwner and pr['title'] == branchName:
                issuePR = pr
        return issuePR

    def _openPRForBranch(self, upstreamNameWithOwner, headOwner, branchName):
        """Authoritative check for an already-open PR whose head is headOwner:branchName into the
        upstream repo, queried DIRECTLY from GitHub (gh api) rather than the lagging RepoClerk
        journal that prList()/issuePR() read.  Returns the PR dict or None (None also on any query
        error, so the caller falls through to PR creation, which is itself guarded against
        duplicates).  The journal lags minutes behind GitHub, so it cannot answer "does a PR exist
        for this branch right now" — the moment that matters when deciding whether to open one."""
        try:
            prs = self.ghJSON(["api",
                               f"repos/{upstreamNameWithOwner}/pulls?head={headOwner}:{branchName}&state=open"])
        except Exception as e:
            logging.warning(f"Could not check for an existing PR on {branchName}: {e}")
            return None
        return prs[0] if prs else None

    def _ghToken(self):
        """The user's GitHub token (from gh) used to authenticate to the App control plane."""
        try:
            out = subprocess.run([self.ghExecutablePath, "auth", "token"],
                                 capture_output=True, text=True, timeout=15)
            if out.returncode != 0:
                return ""  # gh failed: never treat a stray stdout diagnostic as a Bearer token
            return (out.stdout or "").strip()
        except Exception as e:
            raise RuntimeError(f"Could not get a GitHub token from gh: {e}")
