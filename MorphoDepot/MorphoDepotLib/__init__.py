"""MorphoDepot implementation package.

The Slicer module factory requires the four classes (MorphoDepot,
MorphoDepotWidget, MorphoDepotLogic, MorphoDepotTest) to live in MorphoDepot.py.
Everything else is split into this package by domain; the main file keeps those
four thin and inherits behavior from per-domain mixins here. See
docs/refactor-and-test-plan.md.
"""
import os

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
# GIT_PYTHON_REFRESH is read only while the git package is being imported, so it is restored
# immediately: leaving it set would silence the same error for unrelated code in the Slicer
# process (another extension importing GitPython would get a quiet, broken module rather than a
# clear ImportError), and would override a value the user set deliberately.
#
# This runs in the package initializer, which Python executes before any MorphoDepotLib module,
# so importing any part of this package -- from MorphoDepot.py, a test, or the Python console --
# is safe.  Slicer's own git-using code imports GitPython lazily, inside functions, for the same
# reason.
_previousRefreshSetting = os.environ.get("GIT_PYTHON_REFRESH")
os.environ["GIT_PYTHON_REFRESH"] = "quiet"
try:
    import git  # noqa: F401  (imported for its side effect: initializing GitPython quietly)
finally:
    if _previousRefreshSetting is None:
        del os.environ["GIT_PYTHON_REFRESH"]
    else:
        os.environ["GIT_PYTHON_REFRESH"] = _previousRefreshSetting
    del _previousRefreshSetting
