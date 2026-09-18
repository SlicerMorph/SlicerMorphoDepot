"""MorphoDepot implementation package.

The Slicer module factory requires the four classes (MorphoDepot,
MorphoDepotWidget, MorphoDepotLogic, MorphoDepotTest) to live in MorphoDepot.py.
Everything else is split into this package by domain; the main file keeps those
four thin and inherits behavior from per-domain mixins here. See
docs/refactor-and-test-plan.md.
"""
import os
import shutil
import subprocess

# GitPython runs `git version` when it is first imported and raises ImportError when no git
# executable can be found.  Nearly every module in this package imports it, so on a machine
# without git that ImportError propagates all the way out of MorphoDepot.py and Slicer reports
# "Fail to instantiate module MorphoDepot" -- the module simply disappears from the module list
# instead of telling the user what is missing.  A missing git is a condition MorphoDepot reports
# and lets the user fix (install it, or point at it in the Configure tab), never a reason to fail
# to load, so GitPython is imported HERE, once, with its executable search silenced.  Every
# `import git` in this package then gets this already-initialized module.  GitPython is given a
# real git later, by MorphoDepotLogic.refreshGitPython().
#
# GIT_PYTHON_REFRESH silences a git that cannot be FOUND and nothing else: GitPython's own
# refresh catches GitCommandNotFound and PermissionError, so a git that IS found and then fails
# raises GitCommandError straight out of the import and the module disappears anyway.  macOS 26
# does exactly that on Apple Silicon -- Slicer is an x86_64 build running under Rosetta,
# /usr/bin/git is Apple's xcrun shim, and Command Line Tools 27 no longer ship an x86_64 libxcrun,
# so `git version` exits 1 for every child of Slicer.  The git GitPython is about to pick is
# therefore run here first, and when it does not work GitPython is pointed at a path that cannot
# exist so its import takes the "no git found" route instead.  Either way the import succeeds with
# no git configured, which is the state refreshGitPython() expects, and checkGitDependencies()
# reports the broken git the same way it reports a missing one -- so the user can select a working
# git in the Configure tab, which is the whole point of loading rather than failing.
#
# GIT_PYTHON_REFRESH is read only while the git package is being imported, so it is restored
# immediately: leaving it set would silence the same error for unrelated code in the Slicer
# process (another extension importing GitPython would get a quiet, broken module rather than a
# clear ImportError), and would override a value the user set deliberately.  GIT_PYTHON_GIT_EXECUTABLE
# is restored for the same reasons, and is honored rather than overridden when the user set one
# that works.
#
# This runs in the package initializer, which Python executes before any MorphoDepotLib module,
# so importing any part of this package -- from MorphoDepot.py, a test, or the Python console --
# is safe.  Slicer's own git-using code imports GitPython lazily, inside functions, for the same
# reason.


def _gitPythonWouldFindAWorkingGit():
    """True when the git GitPython would pick exists and runs."""
    candidate = os.environ.get("GIT_PYTHON_GIT_EXECUTABLE") or shutil.which("git")
    if not candidate:
        return False
    popenArguments = {}
    if os.name == "nt":
        # Hide the console window, the way slicer.util.launchConsoleProcess does.
        startupInfo = subprocess.STARTUPINFO()
        startupInfo.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startupInfo.wShowWindow = 0
        popenArguments["startupinfo"] = startupInfo
    try:
        # A timeout for the same reason checkCommand() has one: this runs on the UI thread, while
        # Slicer is building its module list, so it can never be allowed to hang.
        completedProcess = subprocess.run([candidate, "version"], capture_output=True, timeout=15, **popenArguments)
    except Exception:
        return False
    return completedProcess.returncode == 0


_previousRefreshSetting = os.environ.get("GIT_PYTHON_REFRESH")
_previousExecutableSetting = os.environ.get("GIT_PYTHON_GIT_EXECUTABLE")
os.environ["GIT_PYTHON_REFRESH"] = "quiet"
if not _gitPythonWouldFindAWorkingGit():
    # A path GitPython cannot find, so it takes the quiet route rather than running the broken git
    # a second time.  refreshGitPython() hands it the git the Configure tab resolved.  The last
    # component sits under a directory that does not exist, so no file can ever occupy this path.
    os.environ["GIT_PYTHON_GIT_EXECUTABLE"] = os.path.join(os.path.dirname(__file__), "no-git-found-at-import-time", "git")
try:
    import git  # noqa: F401  (imported for its side effect: initializing GitPython quietly)
finally:
    for _settingName, _settingValue in (("GIT_PYTHON_REFRESH", _previousRefreshSetting),
                                        ("GIT_PYTHON_GIT_EXECUTABLE", _previousExecutableSetting)):
        if _settingValue is None:
            os.environ.pop(_settingName, None)
        else:
            os.environ[_settingName] = _settingValue
    del _previousRefreshSetting, _previousExecutableSetting, _settingName, _settingValue
del _gitPythonWouldFindAWorkingGit
