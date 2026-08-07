"""MorphoDepotLogic DepsMixin (split from MorphoDepot.py)."""
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


class DepsMixin:
    def slicerVersionCheck(self):
        return hasattr(slicer.vtkSegment, "SetTerminology")

    def checkPythonDependencies(self):
        """See if pygbif is available (used for the taxon-name check and species search).
        The GitPython package is installed by default in slicer.
        """
        try:
            import pygbif
        except ModuleNotFoundError:
            return False

        return True

    def installPythonDependencies(self):
        """Install pygbif if needed
        """
        try:
            import pygbif
        except ModuleNotFoundError:
            self.progressMethod(f"Installing pygbif")
            slicer.util.pip_install("pygbif")
            import pygbif

    def checkCommand(self, command):
        try:
            completedProcess = subprocess.run(command, capture_output=True)
            returnCode = completedProcess.returncode
            stdout = completedProcess.stdout
            stderr = completedProcess.stderr
        except Exception as e:
            stdout =  ""
            stderr = str(e)
            returnCode = -1
        if returnCode != 0:
            self.progressMethod(f"{command} failed to run, returned {returnCode}")
            self.progressMethod(stdout)
            self.progressMethod(stderr)
            return False
        return True

    # Environment that makes git fail instead of trying to ask a human for a password.  git only
    # reaches for a prompt when no credential helper answered, and inside Slicer there is nobody
    # to ask: there is no console for the terminal prompt, and an askpass program inherited from
    # whatever launched Slicer (VS Code sets GIT_ASKPASS for the terminals it spawns) is no longer
    # connected to anything.  Without this, a push with no credential dies deep in git with
    # "failed to execute prompt script" / "could not read Username", which says nothing about the
    # actual problem; with it, the failure is immediate and identifiable ("terminal prompts
    # disabled"), which is what commitAndPush turns into an explanation the user can act on.
    #
    # Empty strings rather than deletions: these variables can be INHERITED from the process that
    # launched Slicer, and GitPython layers its own overrides over os.environ, so removing them
    # here would not keep them out of the child.  git tests the askpass value with `*askpass`, so
    # an empty string reads as "none configured" -- which is exactly what is wanted.
    gitNonInteractiveEnvironment = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    }

    def applyGitNonInteractiveEnvironment(self):
        """Apply gitNonInteractiveEnvironment to this process, for every git child that follows.

        Set process-wide rather than per-repository on purpose: GitPython copies os.environ at
        each invocation, so this reaches the pushes in logic_contribute/logic_release/
        logic_accession and any added later, with no per-call-site plumbing to forget.  The
        scoped alternative exists -- repo.git.update_environment() applies to one Repo -- and is
        rejected for that reason, not because nothing else is affected: Slicer's own Extension
        Wizard pushes through GitPython in this same process (ExtensionWizard.py), so once this
        logic has been built, an extension publish from the same session also gets prompting
        disabled.  That is judged acceptable because a git prompt inside Slicer has nowhere to
        appear either way -- the Wizard user gets a clear failure instead of a hang.

        PATH is deliberately NOT set this way (#214): prepending a portable git's directory would
        put its bash.exe and sh.exe ahead of everything for every subprocess Slicer launches.
        """
        os.environ.update(self.gitNonInteractiveEnvironment)

    def refreshGitPython(self):
        """Point GitPython at the git executable this logic resolved.

        GitPython's import-time search for a git executable is silenced (see __init__.py), so
        it starts up with no git configured and every git.Repo() call would fail until it is
        refreshed here.  This is also what makes a git chosen in the Configure tab usable when it
        is not on PATH: without it that setting would reach the gh/subprocess calls but leave
        GitPython pointed at nothing.

        Returns True when GitPython has a working git.  A missing or unusable git is expected (the
        user has not installed or configured one yet) and is reported by checkGitDependencies(),
        which is what gates the UI -- so it is logged here, not raised.
        """
        # Done here because this runs before any git child does, whether or not a git was resolved.
        self.applyGitNonInteractiveEnvironment()
        if not self.gitExecutablePath:
            return False
        try:
            git.refresh(path=self.gitExecutablePath)
        except Exception as e:
            logging.warning(f"MorphoDepot: GitPython could not use {self.gitExecutablePath}: {e}")
            return False
        return bool(git.GIT_OK)

    def toolPathEnvironmentUpdate(self):
        """PATH override that lets a child process find the git and gh we were configured with.

        MorphoDepot always calls git and gh by absolute path, but those two tools call each other
        BY NAME: `gh repo clone` runs git, and the credential helper `gh auth setup-git` installs
        runs gh.  So a tool the user selected in the Configure tab -- a portable install that was
        never added to PATH, which is the whole reason that setting exists -- is invisible to the
        other one, and gh fails with "unable to find git executable in PATH".

        Returns a dict suitable for slicer.util.launchConsoleProcess's updateEnvironment (which
        REPLACES a variable rather than extending it, so the full value is built here), or an
        empty dict when the child's PATH already covers both directories.  The child environment
        is the startup environment, not this process's, so that is what is extended.
        """
        # Windows paths are case-insensitive and a path typed into the Configure tab need not
        # match the one a QFileDialog produced, so every comparison here is on the normalized
        # form: C:\Program Files\GitHub CLI and c:\program files\github cli\ are one directory
        # and must not be prepended twice -- whether the duplication comes from git and gh
        # living together or from the directory already being on the child's PATH.
        def pathKey(path):
            return os.path.normcase(os.path.normpath(path))

        directories = []
        keys = set()
        for executablePath in (self.gitExecutablePath, self.ghExecutablePath):
            directory = os.path.dirname(executablePath) if executablePath else ""
            if not directory or pathKey(directory) in keys:
                continue
            keys.add(pathKey(directory))
            directories.append(directory)
        if not directories:
            return {}
        try:
            basePath = slicer.util.startupEnvironment().get("PATH", "")
        except Exception:
            basePath = os.environ.get("PATH", "")
        entries = [entry for entry in basePath.split(os.pathsep) if entry]
        present = {pathKey(entry) for entry in entries}
        missing = [directory for directory in directories if pathKey(directory) not in present]
        if not missing:
            return {}
        return {"PATH": os.pathsep.join(missing + entries)}

    def gitCredentialsConfigured(self):
        """True when git can obtain a GitHub credential without asking a human.

        The credential helper is written by `gh auth setup-git` (run for the user as part of
        `gh auth login`), and that can only happen if gh can RUN git -- which is not the case on a
        machine whose git is a portable install that was never put on PATH.  That is #214's
        dependency in the other direction, and it fails silently: gh authenticates fine, `gh auth
        status` is green, the Configure tab is green, every gh-driven operation works, and the one
        operation that goes through GitPython instead -- the push in commitAndPush -- is the one
        that fails, after the segmentation work is done.

        Asks git rather than reading config, so any working helper counts (osxkeychain on macOS,
        manager on Windows) and not just the one gh installs.  `credential fill` consults the
        helpers and returns what git would use for a push; with prompting disabled it fails
        outright when nothing answers, so this never blocks on input.

        HTTPS only, deliberately: MorphoDepot builds its remotes as https://github.com/... itself
        (_ensureUpstream, and the origin set by the accession path), so an HTTPS credential is
        needed for the normal workflow no matter what protocol gh was configured to clone with.
        A user who has switched a remote to SSH by hand still needs one to reach upstream, so a
        False here is not a false alarm for them either -- but their push, alone, would work.

        The timeout is a backstop against a credential helper that HANGS, not a budget for the
        normal path: this runs on every module enter, and when nothing is configured git exits
        immediately, so the cost of the common failing case is milliseconds.  Kept short anyway,
        because enter() must not stall the UI (see its own no-hang contract).

        Note that a helper CAN put a dialog on screen from here: an osxkeychain entry whose
        keychain is locked prompts the user to unlock it, so the first entry into the module on
        such a machine may raise a system password box that was not asked for.  Answering it
        leaves the probe true; dismissing it reads as "no credential", which is also what the
        subsequent push would hit -- so the warning that follows is not a false alarm.
        """
        if not self.gitExecutablePath:
            return False
        environment = os.environ.copy()
        environment.update(self.gitNonInteractiveEnvironment)
        popenArguments = {}
        if os.name == "nt":
            # Hide the console window, the way slicer.util.launchConsoleProcess does.
            startupInfo = subprocess.STARTUPINFO()
            startupInfo.dwFlags = subprocess.STARTF_USESHOWWINDOW
            startupInfo.wShowWindow = subprocess.SW_HIDE
            popenArguments["startupinfo"] = startupInfo
        try:
            completed = subprocess.run(
                [self.gitExecutablePath, "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
                **popenArguments)
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning(f"MorphoDepot: could not ask git for a GitHub credential: {e}")
            return False
        # NEVER log or progressMethod this stdout: on success it contains the access token itself.
        return completed.returncode == 0 and "password=" in (completed.stdout or "")

    def ensureGitCredentialHelper(self):
        """Make sure git has a credential source for github.com, installing one if it has none.

        Running `gh auth setup-git` through self.gh() is what makes this work in the case that
        breaks: gh is handed a PATH containing the configured git (#214), so it can write the
        credential config even when git is not on the system PATH and the same command typed in a
        terminal would fail.  Returns True when git ends up able to answer.

        A False is reported to the user, not raised: searching and browsing work without a push
        credential, so this warns rather than gating the module.
        """
        # Cached like whoami(), and for the same reason: this runs on every module enter, and a
        # helper that answered once will answer again for the life of this logic instance.  Only
        # the success is cached -- a user who goes and fixes their sign-in gets a fresh check on
        # the next enter rather than being told it is still broken.
        if getattr(self, "_gitCredentialsCache", False):
            return True
        if self.gitCredentialsConfigured():
            self._gitCredentialsCache = True
            return True
        # Said plainly because this WRITES to the user's global git configuration, affecting git
        # everywhere else on their machine -- and a probe failure is not proof that no helper
        # exists (a locked keychain reads the same way), so the change can be made on a false
        # premise.  Judged worth it for this audience: the alternative is a segmenter losing a
        # session's work to a failure they cannot diagnose.
        self.progressMethod("Adding GitHub sign-in to your global git configuration...")
        try:
            # Bounded well below gh()'s 300s default: this sits in front of module entry, which
            # must not stall, and it runs again on every enter for as long as the problem lasts.
            # Retrying rather than attempting once per session is deliberate -- a user who fixes
            # the underlying cause (finishes a `gh auth login`, installs git) is then repaired
            # automatically on the way back in, instead of having to know setup-git exists.  The
            # cost of that retry is small: when gh cannot do the job it fails in well under a
            # second, and the timeout is only a backstop against a wedged child.
            self.gh(["auth", "setup-git"], timeout=20)
        except Exception as e:
            logging.warning(f"MorphoDepot: `gh auth setup-git` did not succeed: {e}")
            return False
        self._gitCredentialsCache = self.gitCredentialsConfigured()
        return self._gitCredentialsCache

    def checkGitDependencies(self):
        """Check that git, and gh are available
        """
        if not (self.gitExecutablePath and self.ghExecutablePath):
            self.progressMethod("git/gh paths are not set")
            return False
        if not (os.path.exists(self.gitExecutablePath) and os.path.exists(self.ghExecutablePath)):
            self.progressMethod("bad git/gh paths")
            self.progressMethod(f"git path is {self.gitExecutablePath}")
            self.progressMethod(f"gh path is {self.ghExecutablePath}")
            return False
        if not self.checkCommand([self.gitExecutablePath, '--version']):
            return False
        if not self.checkCommand([self.ghExecutablePath, 'auth', 'status']):
            return False
        return True
