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
            # A timeout so a dependency check can never hang Slicer.  This runs on the UI thread at
            # module entry, and `gh auth status` makes a network round trip to validate the token --
            # on an unreachable network that call would otherwise block forever.  15s is a hang-guard,
            # not a tight SLA (the command normally returns in well under a second); a timeout raises
            # TimeoutExpired, which the except below already turns into a graceful "not available".
            completedProcess = subprocess.run(command, capture_output=True, timeout=15)
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

    def configureRepositoryCredentials(self, repo):
        """Make this repository sign in to GitHub as the gh account MorphoDepot is actually using.

        Written into the repository's OWN config, not the user's global one (which is what
        `gh auth setup-git` edits).  Three things that buys:

        * It authenticates as the RIGHT account.  A host-keyed helper -- osxkeychain, or Windows'
          Credential Manager -- answers for github.com with whatever credential was stored there
          by whoever stored it, and `gh auth switch` does not touch it.  On a shared teaching
          machine, in the three-persona test harness, or on any computer where the user pushed to
          GitHub before installing MorphoDepot, that means commits going up as somebody else
          while whoami() reports the account they picked.  Delegating to `gh auth git-credential`
          follows the active gh account by construction, so the question cannot arise.
        * It changes nothing outside MorphoDepot's own clones.  setup-git rewrites the global
          config, and with no --hostname it does so for EVERY host the user is logged into, not
          just github.com.
        * It does not depend on setup-git succeeding -- which it cannot when gh has no git to run
          (#214), the failure this whole change exists to fix.

        The empty value written first is what makes the first point hold: git ACCUMULATES helpers
        across config scopes, so a global osxkeychain entry would still answer first and win.  An
        empty credential.helper resets the accumulated list, so only this one runs.  (That is the
        same two-entry shape `gh auth setup-git` writes globally.)

        The reset also suppresses a helper the user configured DELIBERATELY for github.com --
        inside MorphoDepot's own clones, and nowhere else.  That is the intent (it is what closes
        the wrong-account hazard above), but it is a real behavior change for anyone with a
        curated credential setup, so it is written down rather than left to be discovered.

        Returns True when the repository was configured.  Callers do not check it: the only way
        to fail early is a missing gh, which checkGitDependencies() has already made impossible
        by gating the UI, and a later failure is undone below rather than left half-applied.
        """
        if not self.ghExecutablePath:
            logging.warning("MorphoDepot: no gh to configure repository credentials with")
            return False
        # Quoted because the path routinely contains spaces (C:\Program Files\GitHub CLI), and
        # git hands this string to a shell.  The escape covers the pathological apostrophe.
        quotedGhPath = self.ghExecutablePath.replace("'", "'\\''")
        helper = f"!'{quotedGhPath}' auth git-credential"
        helperKey = "credential.https://github.com.helper"
        try:
            repo.git.config("--local", "--replace-all", helperKey, "")
            repo.git.config("--local", "--add", helperKey, helper)
            # GIT_TERMINAL_PROMPT stops git asking on a terminal, but it does not stop a HELPER
            # from opening its own window: Git Credential Manager has a separate interactivity
            # switch, and without this one it can raise a browser sign-in flow that nothing in
            # the UI asked for.  Belt and braces, since the reset above already leaves GCM out of
            # the chain for this repository.
            repo.git.config("--local", "credential.interactive", "false")
        except Exception as e:
            logging.warning(f"MorphoDepot: could not configure repository credentials: {e}")
            # Undo a PARTIAL write.  The two commands are not atomic, and the order matters: if
            # the reset lands but the helper does not, the repository is left WORSE than before
            # this ran -- an empty accumulated list suppresses the machine's own helpers with
            # nothing put in their place, so the push fails and commitAndPush tells the user to
            # run `gh auth login`, which cannot fix a local config write.  Unsetting the key
            # returns the repository to its previous behavior (whatever global helper it had),
            # which may well work.  Failing open beats failing closed and misdiagnosed.
            try:
                repo.git.config("--local", "--unset-all", helperKey)
            except Exception:
                pass
            return False
        return True

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
