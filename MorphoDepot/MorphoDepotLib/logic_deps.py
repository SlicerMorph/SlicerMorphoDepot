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
        directories = []
        for executablePath in (self.gitExecutablePath, self.ghExecutablePath):
            directory = os.path.dirname(executablePath) if executablePath else ""
            if directory and directory not in directories:
                directories.append(directory)
        if not directories:
            return {}
        try:
            basePath = slicer.util.startupEnvironment().get("PATH", "")
        except Exception:
            basePath = os.environ.get("PATH", "")
        entries = [entry for entry in basePath.split(os.pathsep) if entry]
        # Windows paths are case-insensitive and the Configure tab hands back a normalized path,
        # so compare normalized: C:\Program Files\GitHub CLI and c:\program files\github cli\ are
        # the same directory and must not be prepended twice.
        present = {os.path.normcase(os.path.normpath(entry)) for entry in entries}
        missing = [directory for directory in directories
                   if os.path.normcase(os.path.normpath(directory)) not in present]
        if not missing:
            return {}
        return {"PATH": os.pathsep.join(missing + entries)}

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
