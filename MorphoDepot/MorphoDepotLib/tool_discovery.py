"""Find working git and gh executables without relying on PATH alone.

PATH is not enough on macOS: an app started from the Dock or Finder gets launchd's PATH,
/usr/bin:/bin:/usr/sbin:/sbin, not the shell's.  /etc/paths.d/homebrew and shell profiles only
reach login shells, so a Homebrew gh is invisible to a Dock-launched Slicer even though the same
Slicer started from a terminal finds it.  And the one git that IS on that PATH, /usr/bin/git, is
Apple's xcrun shim, which fails for every child of Slicer on Apple Silicon (Slicer is x86_64 under
Rosetta, and Command Line Tools no longer ship an x86_64 libxcrun) -- while the real arm64 binary
behind the shim runs fine from the same process.

So candidates are PATH first (whatever the user's environment prefers), then the platform's
common install locations, and a candidate is accepted only when it actually runs.

Imports only the standard library so it can be unit-tested outside Slicer
(Testing/Python/test_tool_discovery.py).
"""
import os
import shutil
import subprocess
import sys


def commonLocations(name, platform=None, environ=None):
    """Platform-specific places `name` is commonly installed, most preferred first.

    Homebrew is only searched on macOS.  Linux distributions install git and gh into /usr/bin,
    which is always on PATH, so nothing is added there.
    """
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform == "darwin":
        # Homebrew on Apple Silicon, then Homebrew on Intel (also Rosetta or migrated installs on
        # Apple Silicon, and the official gh .pkg installer), then MacPorts.
        directories = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
        if name == "git":
            directories += [
                "/usr/local/git/bin",  # git-scm.com installer
                # The real binaries behind the /usr/bin/git xcrun shim.
                "/Library/Developer/CommandLineTools/usr/bin",
                "/Applications/Xcode.app/Contents/Developer/usr/bin",
            ]
        elif name == "gh":
            directories.append(os.path.expanduser("~/.local/bin"))  # webi
        return [os.path.join(directory, name) for directory in directories]
    if platform == "win32":
        programFiles = [environ.get(variable) for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
        programFiles = [directory for directory in dict.fromkeys(programFiles) if directory]
        localPrograms = os.path.join(environ["LOCALAPPDATA"], "Programs") if environ.get("LOCALAPPDATA") else None
        if name == "git":
            roots = programFiles + ([localPrograms] if localPrograms else [])
            return [os.path.join(root, "Git", "cmd", "git.exe") for root in roots]
        if name == "gh":
            return [os.path.join(root, "GitHub CLI", "gh.exe") for root in programFiles]
    return []


def executableRuns(path, timeout=10):
    """True when `path --version` exits 0.  Never raises, never hangs past `timeout`."""
    popenArguments = {}
    if os.name == "nt":
        # Hide the console window, the way slicer.util.launchConsoleProcess does.
        startupInfo = subprocess.STARTUPINFO()
        startupInfo.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startupInfo.wShowWindow = 0
        popenArguments["startupinfo"] = startupInfo
    try:
        completedProcess = subprocess.run([path, "--version"], capture_output=True, timeout=timeout, **popenArguments)
    except Exception:
        return False
    return completedProcess.returncode == 0


def findExecutable(name, savedPath="", which=shutil.which, runs=executableRuns, locations=None):
    """Resolve the `name` executable (git or gh) to use.

    A saved path that runs is always kept.  Otherwise PATH and then the platform's common
    locations are searched for one that runs.  When nothing runs, the saved path is kept if there
    is one (it is what the user chose, and checkGitDependencies() reports it as broken), else the
    first candidate that exists (so the broken tool is reported rather than a missing one), else "".
    """
    if savedPath and runs(savedPath):
        return savedPath
    candidates = []
    onPath = which(name)
    if onPath:
        candidates.append(onPath)
    candidates += commonLocations(name) if locations is None else locations
    seen = {os.path.normcase(os.path.normpath(savedPath))} if savedPath else set()
    existing = []
    for candidate in candidates:
        key = os.path.normcase(os.path.normpath(candidate))
        if key in seen or not os.path.isfile(candidate):
            continue
        seen.add(key)
        existing.append(candidate)
        if runs(candidate):
            return candidate
    if savedPath:
        return savedPath
    return existing[0] if existing else ""
