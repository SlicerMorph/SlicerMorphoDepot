"""MorphoDepotLogic VersionMixin (issue #203): update detection and manual update.

Slicer's extension server only builds MorphoDepot for the Slicer version that is current,
so users on an older Slicer silently stop receiving updates.  This mixin notices that and
offers to apply the update itself.

Threading contract
------------------
`installedVersion()` and `applyUpdate()` read Slicer application state and MUST run on the
main thread.  `fetchUpdateStatus()` is the only method the background check thread calls;
it touches nothing but `subprocess` and `json`.  In particular it does NOT go through
`MorphoDepotLogic.gh()`: that helper reports progress via `progressMethod()`, which calls
`slicer.app.processEvents()` -- driving the Qt event loop from a worker thread would be a
crash waiting to happen.  The caller resolves anything Qt-flavored (the installed version,
the startup environment) on the main thread and hands the results to the worker.

Install shapes
--------------
The gate on offering an update is whether writing is SAFE, not how the user installed:

  extension   Extension Manager install; revision comes from the .s4ext metadata.
  clone       git clone on an additional module path; clean, on the release branch, and
              pointing at the canonical repository.
  devclone    a clone that is dirty, on another branch, detached, or pointing at a fork.
              Reported, never written to -- this is somebody's working tree.
  buildtree   built from source with CMake; the next build would revert any write.
  unknown     anything else (for example an unpacked zip).  Reported, never written to.
"""

import datetime
import json
import logging
import os
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import time

import git
import requests
import slicer
from slicer.i18n import tr as _


VERSION_CHECK_REPO = "SlicerMorph/SlicerMorphoDepot"
VERSION_CHECK_BRANCH = "main"

# Locations this repository used to live at.  Their URLs still redirect here, so a clone
# made before the move is a canonical clone and should still be offered updates.
PREVIOUS_REPOSITORIES = ("MorphoCloud/SlicerMorphoDepot",)

# Backups are the undo for an in-place update, but they should not accumulate forever.
BACKUPS_TO_KEEP = 3

# The installed module is exactly the repository's MorphoDepot/ directory minus the two
# entries the extension build strips.  Verified by diffing an Extension Manager install
# against the repository tree -- keep this in sync if MorphoDepot/CMakeLists.txt changes
# what it ships.
MODULE_SUBDIRECTORY = "MorphoDepot"
UPDATE_EXCLUDED_FILES = ("CMakeLists.txt",)
UPDATE_EXCLUDED_DIRECTORIES = ("Testing", "__pycache__")

# GitHub caps the compare endpoint's file list at 300 entries; past that we cannot tell
# whether module files changed, so we assume they did rather than under-report.
COMPARE_FILE_CAP = 300

# Written into the installed tree after an in-place update.  An official Extension Manager
# update removes the whole install directory first, so this file cannot outlive the patch
# it describes; the recorded .s4ext revision catches anything that replaces metadata
# without removing files.
MARKER_FILENAME = ".applied_update.json"

SHAPE_EXTENSION = "extension"
SHAPE_CLONE = "clone"
SHAPE_DEV_CLONE = "devclone"
SHAPE_BUILD_TREE = "buildtree"
SHAPE_UNKNOWN = "unknown"

UPDATABLE_SHAPES = (SHAPE_EXTENSION, SHAPE_CLONE)


class VersionMixin:

    # ------------------------------------------------------------- what to track

    def versionCheckRepository(self):
        """The repository to compare against.

        Overridable so the check can be pointed at a branch under development, and so a
        repository move -- this one has already moved from MorphoCloud to SlicerMorph --
        does not strand installed copies until they are updated.
        """
        return slicer.util.settingsValue(
            "MorphoDepot/versionCheck/repository", VERSION_CHECK_REPO) or VERSION_CHECK_REPO

    def versionCheckBranch(self):
        return slicer.util.settingsValue(
            "MorphoDepot/versionCheck/branch", VERSION_CHECK_BRANCH) or VERSION_CHECK_BRANCH

    # ------------------------------------------------------------------ identity

    def moduleDirectory(self):
        """Absolute, symlink-resolved directory holding MorphoDepot.py.

        realpath matters on macOS, where the Extension Manager path can be reached through
        /private symlinks; without it the containment test below fails and an ordinary
        extension install is misreported as an unknown shape.
        """
        try:
            modulePath = slicer.modules.morphodepot.path
        except AttributeError:
            return ""
        return os.path.realpath(os.path.dirname(modulePath))

    def _buildTreeRoot(self, directory):
        """Return the enclosing CMake build directory, or "" if there is none."""
        current = directory
        for _depth in range(6):
            if os.path.exists(os.path.join(current, "CMakeCache.txt")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return ""

    def _extensionsManagerModel(self):
        """The extensions manager, or None.

        Slicer compiles the accessor out entirely when built without
        Slicer_BUILD_EXTENSIONMANAGER_SUPPORT, so the attribute can be missing rather than
        merely returning None.
        """
        accessor = getattr(slicer.app, "extensionsManagerModel", None)
        if accessor is None:
            return None
        try:
            return accessor()
        except Exception:
            return None

    def _extensionInstallDirectory(self):
        """Root of the Extension Manager install of MorphoDepot, or ""."""
        model = self._extensionsManagerModel()
        if model is None:
            return ""
        try:
            if not model.isExtensionInstalled("MorphoDepot"):
                return ""
            installPath = model.extensionInstallPath("MorphoDepot")
        except Exception as e:
            logging.debug(f"MorphoDepot version check: extension install path unavailable: {e}")
            return ""
        return os.path.realpath(installPath) if installPath else ""

    def _extensionRevision(self):
        """The upstream revision the installed extension was built from, or ""."""
        model = self._extensionsManagerModel()
        if model is None:
            return ""
        try:
            metadata = model.extensionMetadata("MorphoDepot")
        except Exception as e:
            logging.debug(f"MorphoDepot version check: extension metadata unavailable: {e}")
            return ""
        if not metadata:
            return ""
        try:
            return str(metadata["revision"]).strip()
        except (KeyError, TypeError):
            return ""

    # -------------------------------------------------------------------- marker

    def _markerPath(self, moduleDirectory):
        return os.path.join(moduleDirectory, "MorphoDepotLib", MARKER_FILENAME)

    def _readMarker(self, moduleDirectory, extensionRevision):
        """Revision recorded by our own in-place update, or None.

        Ignored unless the .s4ext revision still matches what it recorded, so that any
        reinstall by the Extension Manager wins over a stale marker.
        """
        markerPath = self._markerPath(moduleDirectory)
        if not os.path.exists(markerPath):
            return None
        try:
            with open(markerPath) as markerFile:
                marker = json.load(markerFile)
        except (OSError, ValueError) as e:
            logging.debug(f"MorphoDepot version check: unreadable update marker: {e}")
            return None
        if not isinstance(marker, dict) or not marker.get("sha"):
            return None
        if marker.get("s4extRevision", "") != extensionRevision:
            logging.debug("MorphoDepot version check: update marker predates the installed extension; ignoring it")
            return None
        return marker

    def _writeMarker(self, moduleDirectory, sha, date, extensionRevision):
        marker = {
            "sha": sha,
            "date": date,
            "s4extRevision": extensionRevision,
            "appliedAt": datetime.datetime.now().astimezone().isoformat(),
        }
        markerPath = self._markerPath(moduleDirectory)
        try:
            with open(markerPath, "w") as markerFile:
                json.dump(marker, markerFile, indent=1)
        except OSError as e:
            logging.warning(f"MorphoDepot: could not record the applied update revision: {e}")

    # ------------------------------------------------------------- write safety

    def _directoryIsWritable(self, directory):
        if not os.path.isdir(directory):
            return False
        try:
            handle, probePath = tempfile.mkstemp(prefix=".morphodepot-write-probe", dir=directory)
        except OSError:
            return False
        os.close(handle)
        try:
            os.remove(probePath)
        except OSError:
            pass
        return True

    # ------------------------------------------------------------- installed side

    def _remoteIsCanonical(self, remoteUrl):
        """Whether a clone's origin is the repository the update check tracks.

        Matches owner AND name.  Matching the name alone would accept a fork at
        github.com/someone/SlicerMorphoDepot, and fast-forwarding a clone whose origin is
        a fork would quietly install somebody else's code.  Both URL forms carry
        "owner/name": https://github.com/Owner/Name.git and git@github.com:Owner/Name.git.
        """
        if not remoteUrl:
            return False
        normalized = remoteUrl.lower()
        candidates = [self.versionCheckRepository().lower()]
        candidates += [previous.lower() for previous in PREVIOUS_REPOSITORIES]
        return any(candidate in normalized for candidate in candidates)

    def _cloneDescription(self, moduleDirectory):
        """Describe the git clone containing the module, or None if there is not one.

        The repository must hold the module at <root>/MorphoDepot -- the layout of this
        repository -- so that an install that merely happens to sit inside some unrelated
        checkout (a dotfiles repository under $HOME, say) is not mistaken for a clone.
        """
        try:
            repository = git.Repo(moduleDirectory, search_parent_directories=True)
        except (git.exc.InvalidGitRepositoryError, git.exc.NoSuchPathError):
            return None
        except Exception as e:
            logging.debug(f"MorphoDepot version check: git inspection failed: {e}")
            return None

        workingTree = repository.working_tree_dir
        if not workingTree:
            return None
        expected = os.path.realpath(os.path.join(workingTree, MODULE_SUBDIRECTORY))
        if expected != moduleDirectory:
            return None

        description = {
            "repositoryRoot": os.path.realpath(workingTree),
            "sha": "",
            "date": "",
            "branch": "",
            "blockedReason": "",
        }
        try:
            commit = repository.head.commit
            description["sha"] = commit.hexsha
            description["date"] = datetime.datetime.fromtimestamp(commit.committed_date).strftime("%Y-%m-%d")
        except Exception as e:
            logging.debug(f"MorphoDepot version check: could not read HEAD: {e}")

        reasons = []
        try:
            if repository.is_dirty(untracked_files=False):
                reasons.append(_("it has uncommitted changes"))
        except Exception:
            pass
        try:
            branch = repository.active_branch.name
            description["branch"] = branch
            if branch != self.versionCheckBranch():
                reasons.append(_("it is on branch '{branch}'").format(branch=branch))
        except TypeError:
            description["branch"] = _("detached HEAD")
            reasons.append(_("it has a detached HEAD"))
        except Exception:
            pass
        try:
            remoteUrl = repository.remotes.origin.url
        except Exception:
            remoteUrl = ""
        if not self._remoteIsCanonical(remoteUrl):
            reasons.append(_("its origin remote is not the MorphoDepot repository"))

        if reasons:
            description["blockedReason"] = _(
                "This is a working clone of MorphoDepot ({reasons}), so MorphoDepot will not "
                "modify it.  Update it yourself with git.").format(reasons=", ".join(reasons))
        return description

    def installedVersion(self):
        """Where MorphoDepot is installed, which revision it is, and whether we may write.

        MAIN THREAD ONLY -- reads slicer.app and slicer.modules.
        """
        installed = {
            "shape": SHAPE_UNKNOWN,
            "sha": "",
            "date": "",
            "moduleDirectory": "",
            "repositoryRoot": "",
            "branch": "",
            "extensionRevision": "",
            "canUpdate": False,
            "blockedReason": "",
        }

        moduleDirectory = self.moduleDirectory()
        if not moduleDirectory:
            installed["blockedReason"] = _("The MorphoDepot module directory could not be located.")
            return installed
        installed["moduleDirectory"] = moduleDirectory

        buildTree = self._buildTreeRoot(moduleDirectory)
        if buildTree:
            installed["shape"] = SHAPE_BUILD_TREE
            installed["blockedReason"] = _(
                "MorphoDepot is running from a CMake build tree, so an update would be undone by "
                "the next build.  Update the source checkout and rebuild.")
            clone = self._cloneDescription(moduleDirectory)
            if clone:
                installed["sha"] = clone["sha"]
                installed["date"] = clone["date"]
                installed["branch"] = clone["branch"]
                installed["repositoryRoot"] = clone["repositoryRoot"]
            return installed

        extensionDirectory = self._extensionInstallDirectory()
        if extensionDirectory and (moduleDirectory + os.sep).startswith(extensionDirectory + os.sep):
            extensionRevision = self._extensionRevision()
            installed["shape"] = SHAPE_EXTENSION
            installed["extensionRevision"] = extensionRevision
            installed["sha"] = extensionRevision
            marker = self._readMarker(moduleDirectory, extensionRevision)
            if marker:
                installed["sha"] = marker["sha"]
                installed["date"] = marker.get("date", "")
            if not installed["sha"]:
                installed["blockedReason"] = _(
                    "The installed extension does not record which revision it was built from.")
                return installed
            if not self._directoryIsWritable(moduleDirectory):
                installed["blockedReason"] = _(
                    "MorphoDepot cannot write to its own install directory, so it cannot update "
                    "itself.  Reinstall through the Extension Manager, or reinstall Slicer "
                    "somewhere you can write to.")
                return installed
            installed["canUpdate"] = True
            return installed

        clone = self._cloneDescription(moduleDirectory)
        if clone:
            installed["sha"] = clone["sha"]
            installed["date"] = clone["date"]
            installed["branch"] = clone["branch"]
            installed["repositoryRoot"] = clone["repositoryRoot"]
            if clone["blockedReason"]:
                installed["shape"] = SHAPE_DEV_CLONE
                installed["blockedReason"] = clone["blockedReason"]
                return installed
            installed["shape"] = SHAPE_CLONE
            if not self._directoryIsWritable(installed["repositoryRoot"]):
                installed["blockedReason"] = _("MorphoDepot cannot write to its git clone.")
                return installed
            installed["canUpdate"] = True
            return installed

        installed["blockedReason"] = _(
            "MorphoDepot cannot tell which version is installed.  Install it through the "
            "Extension Manager or from a git clone to get update notifications.")
        return installed

    # ---------------------------------------------------------------- remote side

    def _runGh(self, arguments, environment=None, timeout=30):
        """Run gh and return stdout, or None on any failure.

        Deliberately not MorphoDepotLogic.gh(): see this module's threading contract.
        """
        ghPath = self.ghExecutablePath
        if not ghPath:
            return None
        childEnvironment = dict(environment) if environment else None
        popenArguments = {}
        if os.name == "nt":
            # Hide the console window, the way slicer.util.launchConsoleProcess does.
            startupInfo = subprocess.STARTUPINFO()
            startupInfo.dwFlags = 1
            startupInfo.wShowWindow = 0
            popenArguments["startupinfo"] = startupInfo
        try:
            completed = subprocess.run(
                [ghPath] + arguments,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=childEnvironment,
                **popenArguments)
        except (OSError, subprocess.SubprocessError) as e:
            logging.debug(f"MorphoDepot version check: gh could not be run: {e}")
            return None
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            if "422" in stderr:
                logging.warning(
                    "MorphoDepot version check: GitHub could not resolve the installed revision "
                    f"(HTTP 422). The abbreviated SHA may be ambiguous: {stderr}")
            else:
                logging.debug(f"MorphoDepot version check: gh exited {completed.returncode}: {stderr}")
            return None
        return completed.stdout

    def _ghJson(self, arguments, environment=None, timeout=30):
        output = self._runGh(arguments, environment=environment, timeout=timeout)
        if not output:
            return None
        try:
            parsed = json.loads(output)
        except ValueError as e:
            logging.debug(f"MorphoDepot version check: unparsable gh output: {e}")
            return None
        return parsed if isinstance(parsed, dict) else None

    def moduleFilesChanged(self, filenames, fileCount):
        """True when a compare result touches files that ship inside the module."""
        if fileCount >= COMPARE_FILE_CAP:
            # Truncated file list: assume the module changed rather than miss an update.
            return True
        modulePrefix = MODULE_SUBDIRECTORY + "/"
        testingPrefix = modulePrefix + "Testing/"
        for filename in filenames:
            if filename.startswith(modulePrefix) and not filename.startswith(testingPrefix):
                return True
        return False

    def fetchUpdateStatus(self, installedSha, environment=None, repository=None, branch=None):
        """Compare the installed revision against upstream.

        BACKGROUND THREAD SAFE -- subprocess and json only.  `repository` and `branch` are
        resolved by the caller on the main thread, since reading them touches QSettings.
        Returns a dict with `available`, `latestSha`, `latestDate`, `aheadBy`, `compareUrl`
        and `error`.
        """
        repository = repository or VERSION_CHECK_REPO
        branch = branch or VERSION_CHECK_BRANCH
        status = {
            "available": False,
            "latestSha": "",
            "latestDate": "",
            "aheadBy": 0,
            "compareUrl": "",
            "compareStatus": "",
            "error": "",
        }

        if not installedSha:
            latest = self._ghJson(
                ["api", f"repos/{repository}/commits/{branch}",
                 "--jq", "{sha: .sha, date: .commit.committer.date}"],
                environment=environment)
            if latest is None:
                status["error"] = _("Could not reach GitHub to check for updates.")
                return status
            status["latestSha"] = latest.get("sha", "")
            status["latestDate"] = (latest.get("date") or "")[:10]
            return status

        jqFilter = (
            "{status: .status, ahead_by: .ahead_by, html_url: .html_url,"
            " head_sha: (.commits | last | .sha),"
            " head_date: (.commits | last | .commit.committer.date),"
            " file_count: ((.files // []) | length),"
            " files: [(.files // [])[].filename]}")
        comparison = self._ghJson(
            ["api", f"repos/{repository}/compare/{installedSha}...{branch}",
             "--jq", jqFilter],
            environment=environment)
        if comparison is None:
            status["error"] = _("Could not reach GitHub to check for updates.")
            return status

        status["compareUrl"] = comparison.get("html_url", "") or ""
        # "identical", "ahead" (the branch has commits we do not), "behind" (we have
        # commits the branch does not -- a development build), or "diverged".
        status["compareStatus"] = comparison.get("status", "") or ""
        status["aheadBy"] = comparison.get("ahead_by", 0) or 0
        status["latestSha"] = comparison.get("head_sha") or installedSha
        status["latestDate"] = (comparison.get("head_date") or "")[:10]

        if comparison.get("status") == "diverged":
            # The installed revision is not an ancestor of the release branch, so "behind"
            # is not a meaningful thing to say about it.
            status["error"] = _("The installed revision is not on the MorphoDepot release branch.")
            return status

        if status["aheadBy"] > 0:
            status["available"] = self.moduleFilesChanged(
                comparison.get("files", []) or [], comparison.get("file_count", 0) or 0)
        return status

    # -------------------------------------------------------------- applying it

    def applyUpdate(self, installed, targetSha):
        """Update the installed module to `targetSha`.

        MAIN THREAD ONLY.  Returns (succeeded, message).
        """
        if installed.get("shape") == SHAPE_CLONE:
            return self._updateClone(installed)
        if installed.get("shape") == SHAPE_EXTENSION:
            return self._updateExtensionInPlace(installed, targetSha)
        return False, _("MorphoDepot does not know how to update this installation.")

    def _updateClone(self, installed):
        repositoryRoot = installed.get("repositoryRoot", "")
        self.progressMethod(f"Updating the MorphoDepot clone at {repositoryRoot}")
        try:
            repository = git.Repo(repositoryRoot)
            repository.git.pull("--ff-only", "origin", self.versionCheckBranch())
        except Exception as e:
            logging.error(f"MorphoDepot update: git pull failed: {e}")
            return False, _("Could not fast-forward the clone: {error}").format(error=str(e))
        return True, _("The MorphoDepot clone was updated.")

    def _backupRoot(self):
        settingsFilePath = getattr(slicer.app, "slicerUserSettingsFilePath", "")
        root = os.path.dirname(settingsFilePath) if settingsFilePath else tempfile.gettempdir()
        return os.path.join(root, "MorphoDepotUpdateBackups")

    def _updateExtensionInPlace(self, installed, targetSha):
        """Replace the installed module files with `targetSha` from GitHub.

        The module is pure Python, so there is nothing to compile: the installed
        qt-scripted-modules tree is the repository's MorphoDepot/ directory minus
        CMakeLists.txt and Testing/.  The old tree is backed up outside the module
        directory first -- a backup left beside the module would be scanned by Slicer's
        module factory and register duplicate modules.
        """
        moduleDirectory = installed.get("moduleDirectory", "")
        if not moduleDirectory or not os.path.isdir(moduleDirectory):
            return False, _("The MorphoDepot module directory could not be located.")
        if not self._directoryIsWritable(moduleDirectory):
            return False, _("MorphoDepot cannot write to its own install directory.")

        workDirectory = tempfile.mkdtemp(prefix="morphodepot-update-")
        backupPath = ""
        try:
            sourceDirectory = self._downloadModuleTree(targetSha, workDirectory)
            files = self._moduleFileList(sourceDirectory)
            if not files or "MorphoDepot.py" not in files:
                return False, _("The downloaded MorphoDepot archive is not what was expected.")

            backupPath = self._backupModuleTree(moduleDirectory, installed.get("sha", ""))
            self.progressMethod(f"Installing {len(files)} MorphoDepot files")
            for relativePath in files:
                destination = os.path.join(moduleDirectory, relativePath)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                self._copyWithRetry(os.path.join(sourceDirectory, relativePath), destination)

            self._removeRetiredFiles(moduleDirectory, files)
            self._purgeCompiledFiles(moduleDirectory)

            problem = self._verifyModuleTree(sourceDirectory, moduleDirectory, files)
            if problem:
                raise RuntimeError(problem)

            self._writeMarker(
                moduleDirectory, targetSha, datetime.datetime.now().strftime("%Y-%m-%d"),
                installed.get("extensionRevision", ""))
            self._pruneBackups(keep=backupPath)
        except Exception as e:
            logging.error(f"MorphoDepot update failed: {e}")
            if backupPath:
                restored = self._restoreModuleTree(backupPath, moduleDirectory)
                if not restored:
                    return False, _(
                        "The update failed and the previous version could not be restored "
                        "automatically.  A copy of it is at {path}.").format(path=backupPath)
                return False, _("The update failed and the previous version was restored: {error}").format(error=str(e))
            return False, _("The update failed: {error}").format(error=str(e))
        finally:
            shutil.rmtree(workDirectory, ignore_errors=True)

        return True, _("MorphoDepot was updated.  The previous version was kept at {path}.").format(path=backupPath)

    def _downloadModuleTree(self, sha, workDirectory):
        """Download and unpack the repository at `sha`; return its MorphoDepot/ directory."""
        archivePath = os.path.join(workDirectory, "morphodepot.tar.gz")
        url = f"https://codeload.github.com/{self.versionCheckRepository()}/tar.gz/{sha}"
        self.progressMethod(f"Downloading MorphoDepot {sha[:7]}")
        # (connect, read) rather than one long budget: the archive is well under a
        # megabyte, so a slow or hung server should give up quickly instead of holding the
        # user-initiated update -- and the UI with it -- for minutes.
        response = requests.get(url, timeout=(10, 60), stream=True)
        response.raise_for_status()
        with open(archivePath, "wb") as archiveFile:
            for chunk in response.iter_content(chunk_size=65536):
                archiveFile.write(chunk)

        with tarfile.open(archivePath, "r:gz") as archive:
            names = archive.getnames()
            if not names:
                raise RuntimeError("the downloaded archive is empty")
            # GitHub names the top level <repo>-<ref>, where <ref> depends on what was
            # requested; read it rather than reconstructing it.
            topLevel = names[0].split("/")[0]
            self._extractArchive(archive, workDirectory)

        sourceDirectory = os.path.join(workDirectory, topLevel, MODULE_SUBDIRECTORY)
        if not os.path.isdir(sourceDirectory):
            raise RuntimeError(f"the downloaded archive has no {MODULE_SUBDIRECTORY} directory")
        return sourceDirectory

    def _extractArchive(self, archive, workDirectory):
        """Unpack, refusing anything that would write outside `workDirectory`.

        tarfile's `filter="data"` only exists on Python 3.12 and later, and MorphoDepot
        supports Slicer versions that ship older ones, so the members are checked here
        rather than relying on a guard that silently is not there.
        """
        for member in archive.getmembers():
            memberPath = posixpath.normpath(member.name)
            if posixpath.isabs(memberPath) or memberPath.startswith(".."):
                raise RuntimeError(f"archive entry outside the extraction directory: {member.name}")
            if not (member.isfile() or member.isdir()):
                # GitHub source tarballs are plain files and directories; a link or device
                # entry means this is not the archive we think it is.
                raise RuntimeError(f"unexpected archive entry type: {member.name}")
        try:
            archive.extractall(path=workDirectory, filter="data")
        except TypeError:
            archive.extractall(path=workDirectory)

    def _moduleFileList(self, sourceDirectory):
        """Relative paths of the files an extension build ships."""
        files = []
        for root, directories, names in os.walk(sourceDirectory):
            directories[:] = [d for d in directories if d not in UPDATE_EXCLUDED_DIRECTORIES]
            relativeRoot = os.path.relpath(root, sourceDirectory)
            relativeRoot = "" if relativeRoot == "." else relativeRoot
            for name in names:
                relativePath = os.path.join(relativeRoot, name) if relativeRoot else name
                if relativePath in UPDATE_EXCLUDED_FILES:
                    continue
                files.append(relativePath)
        return sorted(files)

    def _copyWithRetry(self, source, destination, attempts=3):
        """Copy, retrying briefly: on Windows a sync client or scanner can hold a file open."""
        for attempt in range(attempts):
            try:
                shutil.copy2(source, destination)
                return
            except OSError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.5)

    def _backupModuleTree(self, moduleDirectory, installedSha):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        label = (installedSha or "unknown")[:7]
        backupRoot = self._backupRoot()
        os.makedirs(backupRoot, exist_ok=True)
        # mkdtemp rather than a bare timestamp: retrying a failed update within the same
        # second would otherwise collide with the backup the first attempt left behind.
        backupPath = tempfile.mkdtemp(prefix=f"MorphoDepot-{label}-{stamp}-", dir=backupRoot)
        self.progressMethod(f"Backing up the installed MorphoDepot to {backupPath}")
        shutil.copytree(
            moduleDirectory, backupPath, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return backupPath

    def _pruneBackups(self, keep=""):
        """Drop all but the most recent backups.

        Called only after an update succeeds, so the copy that was just made -- and the
        one before it, in case a problem only shows up later -- always survive.
        """
        backupRoot = self._backupRoot()
        try:
            candidates = [os.path.join(backupRoot, name) for name in os.listdir(backupRoot)]
        except OSError:
            return
        directories = [path for path in candidates if os.path.isdir(path)]
        try:
            directories.sort(key=os.path.getmtime, reverse=True)
        except OSError:
            return
        for stale in directories[BACKUPS_TO_KEEP:]:
            if stale == keep:
                continue
            shutil.rmtree(stale, ignore_errors=True)

    def _restoreModuleTree(self, backupPath, moduleDirectory):
        """Copy the backup back over the module directory.

        Copies rather than replacing wholesale: a failed rmtree partway through would
        leave the user with no module at all, whereas leftover new files are inert once
        MorphoDepot.py is the old one again and no longer imports them.
        """
        try:
            shutil.copytree(backupPath, moduleDirectory, dirs_exist_ok=True)
            self._purgeCompiledFiles(moduleDirectory)
        except Exception as e:
            logging.error(f"MorphoDepot update: restoring the previous version failed: {e}")
            return False
        return True

    def _removeRetiredFiles(self, moduleDirectory, files):
        """Delete installed files that upstream no longer ships."""
        keep = set(files)
        for root, directories, names in os.walk(moduleDirectory):
            directories[:] = [d for d in directories if d != "__pycache__"]
            for name in names:
                relativePath = os.path.relpath(os.path.join(root, name), moduleDirectory)
                if relativePath in keep or name == MARKER_FILENAME or name.endswith(".pyc"):
                    continue
                try:
                    os.remove(os.path.join(root, name))
                    logging.info(f"MorphoDepot update: removed retired file {relativePath}")
                except OSError as e:
                    logging.warning(f"MorphoDepot update: could not remove {relativePath}: {e}")

    def _purgeCompiledFiles(self, moduleDirectory):
        """Drop cached bytecode so nothing stale can shadow the new sources."""
        for root, directories, names in os.walk(moduleDirectory, topdown=False):
            for name in names:
                if name.endswith(".pyc"):
                    try:
                        os.remove(os.path.join(root, name))
                    except OSError:
                        pass
            for directory in directories:
                if directory == "__pycache__":
                    shutil.rmtree(os.path.join(root, directory), ignore_errors=True)

    def _verifyModuleTree(self, sourceDirectory, moduleDirectory, files):
        """Return a problem description, or "" when the installed tree looks sound.

        Compiling without executing catches a truncated or corrupted write -- the failure
        mode of copying onto a network drive -- while the backup is still around.
        """
        for relativePath in files:
            destination = os.path.join(moduleDirectory, relativePath)
            if not os.path.exists(destination):
                return f"{relativePath} is missing after the update"
            if os.path.getsize(destination) != os.path.getsize(os.path.join(sourceDirectory, relativePath)):
                return f"{relativePath} is the wrong size after the update"
            if relativePath.endswith(".py"):
                try:
                    with open(destination, "rb") as sourceFile:
                        compile(sourceFile.read(), destination, "exec")
                except (SyntaxError, ValueError) as e:
                    return f"{relativePath} did not survive the update ({e})"
        return ""
