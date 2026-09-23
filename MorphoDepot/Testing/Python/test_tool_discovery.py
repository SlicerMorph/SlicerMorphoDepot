#!/usr/bin/env python3
"""Unit tests for MorphoDepotLib/tool_discovery.py -- finding git and gh beyond PATH.

Run: python3 MorphoDepot/Testing/Python/test_tool_discovery.py   (no Slicer, no deps, no network)

The failure these guard against: Slicer started from the macOS Dock gets launchd's PATH
(/usr/bin:/bin:/usr/sbin:/sbin), so shutil.which("gh") missed /opt/homebrew/bin/gh and
shutil.which("git") returned the /usr/bin/git xcrun shim, which does not run under Rosetta.
"""
import importlib.util
import os
import tempfile
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2] / "MorphoDepotLib" / "tool_discovery.py"
_spec = importlib.util.spec_from_file_location("tool_discovery", _LIB)
discovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(discovery)


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


def makeFiles(root, *names):
    paths = {}
    for name in names:
        path = os.path.join(root, name)
        Path(path).touch()
        paths[name] = path
    return paths


def test_common_locations():
    print("commonLocations")
    macGh = discovery.commonLocations("gh", platform="darwin")
    macGit = discovery.commonLocations("git", platform="darwin")
    check("macOS gh: Apple Silicon Homebrew first", macGh[0] == "/opt/homebrew/bin/gh")
    check("macOS gh: Intel Homebrew searched", "/usr/local/bin/gh" in macGh)
    check("macOS git: real binary behind the xcrun shim searched",
          "/Library/Developer/CommandLineTools/usr/bin/git" in macGit)
    check("macOS git: /usr/bin/git shim is not a common location", "/usr/bin/git" not in macGit)
    linux = discovery.commonLocations("gh", platform="linux") + discovery.commonLocations("git", platform="linux")
    check("Linux: no Homebrew", not any("brew" in path for path in linux))
    windows = discovery.commonLocations("gh", platform="win32", environ={"ProgramFiles": r"C:\Program Files"})
    check("Windows gh: installer default", windows == [os.path.join(r"C:\Program Files", "GitHub CLI", "gh.exe")])
    check("Windows: no Homebrew", not any("brew" in path for path in windows))


def test_find_executable():
    print("findExecutable")
    with tempfile.TemporaryDirectory() as root:
        f = makeFiles(root, "shim-git", "real-git", "brew-gh", "saved-gh", "broken-gh")
        working = {f["real-git"], f["brew-gh"], f["saved-gh"]}
        runs = lambda path: path in working

        # The reported case: Dock PATH has only the broken shim, the real git is elsewhere.
        found = discovery.findExecutable("git", which=lambda name: f["shim-git"], runs=runs,
                                         locations=[f["real-git"]])
        check("broken git on PATH is skipped for a working common location", found == f["real-git"])

        # The reported case: gh is not on PATH at all.
        found = discovery.findExecutable("gh", which=lambda name: None, runs=runs, locations=[f["brew-gh"]])
        check("gh off PATH is found in a common location", found == f["brew-gh"])

        # A stale saved path (an earlier detection wrote /usr/bin/git to settings) is re-detected.
        found = discovery.findExecutable("git", savedPath=f["shim-git"], which=lambda name: f["shim-git"],
                                         runs=runs, locations=[f["real-git"]])
        check("broken saved path is replaced by a working one", found == f["real-git"])

        found = discovery.findExecutable("gh", savedPath=f["saved-gh"], which=lambda name: None, runs=runs,
                                         locations=[f["brew-gh"]])
        check("working saved path is kept over a common location", found == f["saved-gh"])

        found = discovery.findExecutable("gh", savedPath=f["broken-gh"], which=lambda name: None, runs=runs,
                                         locations=[])
        check("broken saved path is kept when nothing works", found == f["broken-gh"])

        found = discovery.findExecutable("git", which=lambda name: f["shim-git"], runs=runs, locations=[])
        check("broken PATH git is returned when nothing works (reported, not 'missing')", found == f["shim-git"])

        found = discovery.findExecutable("gh", which=lambda name: None, runs=runs,
                                         locations=[os.path.join(root, "does-not-exist")])
        check("nothing found anywhere gives empty path", found == "")


def test_executable_runs():
    print("executableRuns")
    check("missing executable does not raise", discovery.executableRuns("/no/such/executable") is False)


if __name__ == "__main__":
    test_common_locations()
    test_find_executable()
    test_executable_runs()
    print("all passed")
