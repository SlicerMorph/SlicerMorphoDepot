"""MorphoDepotLogic RepoMixin (split from MorphoDepot.py)."""
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
import hashlib
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


class RepoMixin:
    def localRepositoryDirectory(self):
        repoDirectory = os.path.normpath(slicer.util.settingsValue("MorphoDepot/repoDirectory", "") or "")
        if repoDirectory == "" or repoDirectory == ".":
            defaultScenePath = os.path.normpath(slicer.app.defaultScenePath)
            defaultRepoDir = os.path.join(defaultScenePath, "MorphoDepot")
            self.setLocalRepositoryDirectory(defaultRepoDir)
            repoDirectory = defaultRepoDir

        if repoDirectory and not os.path.exists(repoDirectory):
            message = f"The repository directory does not exist:\n\n{repoDirectory}\n\nCreate it now?"
            if slicer.util.confirmOkCancelDisplay(message, windowTitle="Create Directory"):
                try:
                    os.makedirs(repoDirectory)
                except OSError as e:
                    logging.error(f"Could not create repository directory {repoDirectory}: {e}")

        return repoDirectory

    def setLocalRepositoryDirectory(self, repoDir):
        qt.QSettings().setValue("MorphoDepot/repoDirectory", repoDir)

    def _ensureUpstream(self, canonicalNameWithOwner):
        """Point the 'upstream' remote at the canonical repo (not the fork) — set explicitly rather
        than inferred from origin, so reviewer/release actions always resolve the right base repo."""
        url = f"https://github.com/{canonicalNameWithOwner}.git"
        if "upstream" in [remote.name for remote in self.localRepo.remotes]:
            self.localRepo.git.remote("set-url", "upstream", url)
        else:
            self.localRepo.create_remote("upstream", url)

    def _userForkOf(self, sourceRepository):
        """The active user's fork of sourceRepository as 'owner/name', or None.  Identifies the fork
        by its PARENT (GitHub's fork list), so a suffixed fork (e.g. name-1, created when the user
        already owned an unrelated repo of that name) is found, and an unrelated same-named repo is
        never mistaken for the fork.  (R-1/R-2)"""
        me = self.whoami()
        name = sourceRepository.split("/")[1]
        try:  # fast path: the usual case is a same-named fork at {me}/{name}
            info = self.ghJSON(["repo", "view", f"{me}/{name}", "--json", "parent"])
        except Exception:
            info = None
        parent = (info or {}).get("parent") or {}
        parentLogin = (parent.get("owner") or {}).get("login")
        # `gh repo view --json parent` gives parent.{name, owner.login} (no nameWithOwner), so build it.
        if parentLogin and parent.get("name") and f"{parentLogin}/{parent['name']}" == sourceRepository:
            return f"{me}/{name}"
        # slow path: a differently-named (suffixed) fork — ask the source's fork list directly.
        # Guarded like the fast path: on a gh/network error return None so loadIssue falls through to
        # fork creation rather than aborting.
        try:
            out = self.gh(["api", f"repos/{sourceRepository}/forks", "--paginate",
                           "--jq", f'.[] | select(.owner.login=="{me}") | .full_name']) or ""
        except Exception as e:
            logging.warning(f"Could not list forks of {sourceRepository}: {e}")
            return None
        forks = [line.strip() for line in out.splitlines() if line.strip()]
        return forks[0] if forks else None

    def loadIssue(self, issue, repoDirectory):
        self.currentIssue = issue
        self.progressMethod(f"Loading issue {issue} into {repoDirectory}")
        issueNumber = issue['number']
        branchName=f"issue-{issueNumber}"
        sourceRepository = issue['repository']['nameWithOwner']
        repositoryName = issue['repository']['name']
        localDirectory = os.path.join(repoDirectory, f"{repositoryName}-{branchName}")

        self.cacheOldVersion(localDirectory)

        # Resolve where to clone from.  The contributor works in their FORK of the canonical repo;
        # the fork is identified by its parent (not its name), so an unrelated same-named repo, or a
        # suffixed fork (name-1) from a prior name collision / a deleted+recreated source, is never
        # mistaken for it.  The owner of the canonical repo clones it directly (no fork).  (R-1)
        me = self.whoami()
        if sourceRepository.split("/")[0] == me:
            cloneTarget = sourceRepository
            isFork = False
        else:
            cloneTarget = self._userForkOf(sourceRepository)
            if not cloneTarget:
                self.gh(["repo", "fork", sourceRepository, "--clone=false"])
                cloneTarget = self._userForkOf(sourceRepository) or f"{me}/{repositoryName}"
            isFork = True
        self.gh(["repo", "clone", cloneTarget, localDirectory])
        self.localRepo = git.Repo(localDirectory)
        self._ensureUpstream(sourceRepository)

        # D2: keep a genuine fork's default branch current with upstream (GitHub forks do not
        # auto-sync, so a pre-existing fork drifts behind every merge/release).  Best-effort
        # server-side fast-forward (no --force, so it cannot clobber a diverged fork); a failure
        # never aborts — D1 below cuts the new branch from upstream/main regardless.
        if isFork and cloneTarget != sourceRepository:
            try:
                self.gh(["repo", "sync", cloneTarget, "--source", sourceRepository])
            except Exception as e:
                logging.warning(f"Could not sync fork {cloneTarget} from {sourceRepository}: {e}")

        originBranches = self.localRepo.remotes.origin.fetch()
        originBranchIDs = [ob.name for ob in originBranches]
        originBranchID = f"origin/{branchName}"

        # D1: fetch upstream so a new issue branch can be cut from the current upstream default
        # branch (the latest published baseline), not the fork's possibly-stale main.
        try:
            self.localRepo.remote("upstream").fetch()
        except Exception as e:
            logging.warning(f"Could not fetch upstream: {e}")

        localIssueBranch = None
        for branch in self.localRepo.branches:
            if branch.name == branchName:
                localIssueBranch = branch
                break

        logging.debug("Making new branch")
        if originBranchID in originBranchIDs:
            logging.debug("Checking out existing from origin")
            self.localRepo.git.execute(f"git checkout --track {originBranchID}".split())
        else:
            # D1: branch off upstream/main (latest published state).  Fall back to origin/main
            # only if upstream/main is somehow unavailable, preserving the prior behavior.
            base = "upstream/main"
            try:
                self.localRepo.git.rev_parse("--verify", base)
            except Exception:
                base = "origin/main"
            logging.debug("Nothing in origin for %s; creating it from %s", branchName, base)
            self.localRepo.git.checkout(base)
            self.localRepo.git.branch(branchName)
            self.localRepo.git.checkout(branchName)

        self.loadFromLocalRepository()

    def loadPR(self, pr, repoDirectory):
        baseRepo = pr['repository']['nameWithOwner']
        baseName = pr['repository']['name']
        branchName = pr['title']
        # Resolve the ACTUAL head repo + branch from GitHub — never construct {author}/{name}: a
        # contributor's fork may be suffixed (name-1) after a name collision, and an in-repo-branch
        # PR has head == base.  Fall back to the constructed name if the query is unavailable.  (R-2)
        headNameWithOwner = None
        number = pr.get('number')
        if number is not None:
            head = None
            try:
                head = self.ghJSON(["pr", "view", str(number), "--repo", baseRepo,
                                    "--json", "headRepository,headRepositoryOwner,headRefName"])
            except Exception as e:
                logging.warning(f"Could not resolve PR #{number} head repo: {e}")
            if head:
                headOwner = (head.get("headRepositoryOwner") or {}).get("login")
                headName = (head.get("headRepository") or {}).get("name")
                if headOwner and headName:
                    headNameWithOwner = f"{headOwner}/{headName}"
                if head.get("headRefName"):
                    branchName = head["headRefName"]
        if not headNameWithOwner:
            headNameWithOwner = f"{pr['author']['login']}/{baseName}"  # legacy fallback
        localDirectory = os.path.join(repoDirectory, f"{baseName}-{branchName}")
        self.progressMethod(f"Loading PR from {headNameWithOwner} into {localDirectory}")

        self.cacheOldVersion(localDirectory)

        self.gh(["repo", "clone", headNameWithOwner, localDirectory])
        self.localRepo = git.Repo(localDirectory)
        self._ensureUpstream(baseRepo)
        self.localRepo.remotes.origin.fetch()
        self.localRepo.git.checkout(branchName)

        self.loadFromLocalRepository(configuration="reviewer")
        return True

    def loadRepoForRelease(self, repoData):
        repoName = repoData['name']
        repoNameWithOwner = repoData['nameWithOwner'] # this is owner/name
        localDirectory = os.path.join(self.localRepositoryDirectory(), repoName)

        self.cacheOldVersion(localDirectory)

        # clone the main repo, not a fork
        self.gh(["repo", "clone", repoNameWithOwner, localDirectory])

        self.localRepo = git.Repo(localDirectory)
        self.localRepo.git.checkout("main")
        self.loadFromLocalRepository(remoteName="origin", configuration="release")
        return True

    def loadRepoForPreview(self, repoNameWithOwner):
        repoName = repoNameWithOwner.split('/')[1]
        localDirectory = os.path.join(self.localRepositoryDirectory(), repoName)

        self.cacheOldVersion(localDirectory)

        self.gh(["repo", "clone", repoNameWithOwner, localDirectory])

        self.localRepo = git.Repo(localDirectory)
        self.localRepo.git.checkout("main")
        self.loadFromLocalRepository(remoteName="origin", configuration="preview")
        return True

    def _fileMatchesChecksum(self, filePath, checksum):
        """True if filePath's digest matches `checksum` ('<algo>:<hex>', e.g. 'SHA256:ab...', or a
        bare SHA-256 hex).  Used to re-verify the name-keyed volume cache on every load (review S4)."""
        algo, sep, expected = checksum.partition(":")
        if not sep:
            algo, expected = "sha256", checksum  # bare hex -> assume SHA-256
        try:
            h = hashlib.new(algo.lower())
            with open(filePath, "rb") as fp:
                for block in iter(lambda: fp.read(1024 * 1024), b""):
                    h.update(block)
        except (ValueError, OSError):
            return False  # unknown algorithm or unreadable file -> treat as mismatch
        return h.hexdigest().lower() == expected.strip().lower()

    def loadFromLocalRepository(self, remoteName="upstream", configuration="segment"):
        localDirectory = self.localRepo.working_dir
        branchName = self.localRepo.active_branch.name
        remoteNameWithOwner = self.nameWithOwner(remoteName)

        self.progressMethod(f"Loading {branchName} into {localDirectory}")

        self.colorTableNode = None
        try:
            colorPath = glob.glob(f"{localDirectory}/*.csv")[0]
            self.colorTableNode = slicer.util.loadColorTable(colorPath)
        except IndexError:
            try:
                colorPath = glob.glob(f"{localDirectory}/*.ctbl")[0]
                self.colorTableNode = slicer.util.loadColorTable(colorPath)
            except IndexError:
                self.progressMethod("No color table found")

        # TODO: move from single volume file to segmentation specification json
        volumePath = os.path.join(localDirectory, "source_volume")
        if not os.path.exists(volumePath):
            volumePath = os.path.join(localDirectory, "master_volume") # for backwards compatibility
        volumeRef = open(volumePath).read().strip()
        # The source_volume pointer is "releases/download/v1/{originalName}.nrrd";
        # remember the original name so the UI can display it.
        self.sourceVolumeName = os.path.basename(volumeRef).rsplit('.nrrd', 1)[0]
        volumeURL = self.resolveVolumeURL(volumeRef, remoteNameWithOwner)
        cacheDirectory = os.path.join(self.localRepositoryDirectory(), "MorphoDepotCaches", "Volumes")
        os.makedirs(cacheDirectory, exist_ok=True)
        nrrdPath = os.path.join(cacheDirectory, f"{remoteNameWithOwner.replace('/', '-')}-volume.nrrd")
        checksum = None
        checksumFilePath = os.path.join(localDirectory, "source_volume_checksum")
        if os.path.exists(checksumFilePath):
            with open(checksumFilePath) as fp:
                checksum = fp.read().strip()
        else:
            # S4: no committed checksum -- integrity can't be verified.  Warn rather than silently
            # trusting whatever bytes are served/cached for legacy repos.
            self.progressMethod("Warning: no source_volume_checksum committed; cannot verify volume integrity.")
        if checksum and os.path.exists(nrrdPath) and not self._fileMatchesChecksum(nrrdPath, checksum):
            # S4: the cache is keyed by repo name, not content, so a stale (new release) or tampered
            # cached file would otherwise be reused unverified -- re-fetch instead of trusting it.
            self.progressMethod("Cached source volume failed checksum; re-downloading.")
            try:
                os.remove(nrrdPath)
            except OSError as e:
                # S4: if we can't evict the bad file we must NOT fall through and load it (the
                # `if not os.path.exists` below would otherwise skip re-download and loadVolume the
                # known-bad cache file, defeating the integrity check).
                raise RuntimeError(
                    f"Cached source volume failed checksum and could not be removed for re-download: {e}")
        if not os.path.exists(nrrdPath):
            slicer.util.downloadFile(volumeURL, nrrdPath, checksum=checksum)
        volumeNode = slicer.util.loadVolume(nrrdPath)
        # Keep a handle to the source volume: it is the reference geometry used to hash a
        # baseline segmentation's voxels (release "baseline unchanged" check).
        self.sourceVolumeNode = volumeNode

        # Load all segmentations
        segmentationNodesByName = {}
        for segmentationPath in glob.glob(f"{localDirectory}/*.seg.nrrd"):
            name = os.path.split(segmentationPath)[1].split(".")[0]
            segmentationNodesByName[name] = slicer.util.loadSegmentation(segmentationPath)
        # Default for the New release "baseline segmentation" picker: prefer a segmentation
        # named "baseline" if present, otherwise fall back to whatever loaded.
        self.baselineSegmentationNode = (
            segmentationNodesByName.get("baseline")
            or (next(iter(segmentationNodesByName.values()), None))
        )

        if configuration in ("segment", "reviewer"):
            for segmentationNode in segmentationNodesByName.values():
                segmentationNode.GetDisplayNode().SetVisibility(False)

            # Switch to Segment Editor module
            pluginHandlerSingleton = slicer.qSlicerSubjectHierarchyPluginHandler.instance()
            pluginHandlerSingleton.pluginByName("Default").switchToModule("SegmentEditor")
            editorWidget = slicer.modules.segmenteditor.widgetRepresentation().self()

            self.segmentationPath = os.path.join(localDirectory, branchName) + ".seg.nrrd"
            if branchName in segmentationNodesByName.keys():
                self.segmentationNode = segmentationNodesByName[branchName]
                self.segmentationNode.GetDisplayNode().SetVisibility(True)
            else:
                self.segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
                self.segmentationNode.CreateDefaultDisplayNodes()
                self.segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
                self.segmentationNode.SetName(branchName)
                if "baseline" in segmentationNodesByName.keys():
                    baselineSegmentation = segmentationNodesByName["baseline"].GetSegmentation()
                    newSegmentation = self.segmentationNode.GetSegmentation()
                    for segmentID in baselineSegmentation.GetSegmentIDs():
                        newSegmentation.CopySegmentFromSegmentation(baselineSegmentation, segmentID)

            editorWidget.parameterSetNode.SetAndObserveSegmentationNode(self.segmentationNode)
            editorWidget.parameterSetNode.SetAndObserveSourceVolumeNode(volumeNode)

    def cacheOldVersion(self, directoryPath):
        """If directoryPath exists, move it to an archive in the cache."""
        if os.path.exists(directoryPath):
            self.progressMethod(f"Archiving old version of {directoryPath}")
            cacheDirectory = os.path.join(self.localRepositoryDirectory(), "MorphoDepotCaches", "OldRepositories")
            os.makedirs(cacheDirectory, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            archiveName = f"{os.path.basename(directoryPath)}-{timestamp}"
            archivePath = os.path.join(cacheDirectory, archiveName)
            shutil.move(directoryPath, archivePath)
