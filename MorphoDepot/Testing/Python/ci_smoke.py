"""Cross-platform smoke test of MorphoDepot's tool plumbing, run INSIDE Slicer by CI.

Slicer runs this with ``--python-script`` after loading the checked-out MorphoDepot module
(``--additional-module-paths``).  It builds the real ``MorphoDepotLogic`` -- so git and gh are
resolved exactly as they are for a user -- and then clones a small public repository along the
two paths every MorphoDepot clone or push actually takes:

1. ``logic.gh(["repo", "clone", ...])`` -- gh runs ``git`` BY NAME in the child environment the
   logic builds (Slicer's startup environment plus the configured tool directories, see
   ``toolPathEnvironmentUpdate``).  On Linux Slicer packages, ``<Slicer>/bin/git`` is a wrapper
   script that needs the launcher's ``APPLAUNCHER_*`` variables; put it on gh's PATH without them
   and it re-executes itself forever (the 2026-09-21 MorphoCloud hang).
2. ``git.Repo.clone_from(...)`` -- GitPython inside Slicer's own environment, using the git the
   logic resolved.  This is the direction that needs the wrapper on Linux (the system git fails
   there with a libssh symbol error) and a working portable git on Windows.

A failure or a timeout on either path fails the job.  Nothing here needs the intake App, org
membership, or object storage: it is deliberately only the part that keeps breaking per platform.

Run locally (any platform) with the same command CI uses, e.g. on macOS::

    /Applications/Slicer.app/Contents/MacOS/Slicer --no-splash --no-main-window \
        --additional-module-paths <checkout>/MorphoDepot \
        --python-script <checkout>/MorphoDepot/Testing/Python/ci_smoke.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import slicer

PUBLIC_REPO = "MorphoDepot/docs"        # small, public, ours
CLONE_TIMEOUT_SECONDS = 120             # a clone of a 600 KB repo takes seconds; a loop never ends

results = []
# Everything printed here also goes to this file when set: on Windows the GUI launcher does not
# hand the app's console output back to the calling shell, so CI prints the file afterwards.
LOG_PATH = os.environ.get("MD_SMOKE_LOG")


def say(line):
    print(line, flush=True)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except Exception:
            pass


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    say(f"[smoke] {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def killProcessesMentioning(text):
    """Best-effort cleanup of children a timed-out clone leaves behind (the looping wrapper
    outlives gh's kill).  POSIX only; harmless when nothing matches."""
    if os.name == "nt":
        return
    try:
        subprocess.run(["pkill", "-f", text], capture_output=True, timeout=10)
    except Exception:
        pass


def main():
    workDir = tempfile.mkdtemp(prefix="md-smoke-")
    say(f"[smoke] platform={sys.platform} slicer={slicer.app.applicationVersion} "
        f"home={slicer.app.slicerHome} workDir={workDir}")

    import MorphoDepot
    logic = MorphoDepot.MorphoDepotLogic(progressMethod=lambda *a: None)
    gitPath, ghPath = logic.gitExecutablePath, logic.ghExecutablePath
    say(f"[smoke] resolved git={gitPath!r} gh={ghPath!r}")
    childPath = logic.toolPathEnvironmentUpdate().get("PATH", "(startup PATH unchanged)")
    say(f"[smoke] PATH for gh's child: {childPath}")
    record("git resolved", bool(gitPath), gitPath or "no git found")
    record("gh resolved", bool(ghPath), ghPath or "no gh found")
    # Slicer's own bin must never be on the child PATH: on Linux its git is a wrapper that only
    # works inside the launcher environment and exec-loops outside it (see toolPathEnvironmentUpdate).
    slicerHome = os.path.normcase(os.path.normpath(slicer.app.slicerHome))

    def underSlicerHome(entry):  # same test as the production check: separator-aware, drive-safe
        try:
            return os.path.commonpath([slicerHome, os.path.normcase(os.path.normpath(entry))]) == slicerHome
        except ValueError:
            return False

    leaked = [entry for entry in logic.toolPathEnvironmentUpdate().get("PATH", "").split(os.pathsep)
              if entry and underSlicerHome(entry)]
    record("child PATH excludes Slicer's own directories", not leaked, ", ".join(leaked) or "none on it")
    if not (gitPath and ghPath):
        shutil.rmtree(workDir, ignore_errors=True)
        return

    # --- 1. gh --version through the logic (exercises launchConsoleProcess + the child env) ---
    try:
        out = logic.gh(["--version"], timeout=60)
        record("gh runs via logic", True, out.strip().splitlines()[0])
    except Exception as e:
        record("gh runs via logic", False, str(e).splitlines()[0])

    # --- 2. gh repo clone: gh finds git BY NAME on the child PATH the logic built ---
    # Run under a watchdog thread: logic.gh() kills gh after its timeout, but then waits on the
    # child's pipes, and a grandchild that keeps looping (the Linux wrapper) still holds them, so
    # that wait never returns.  Killing the leftovers by their command line closes the pipes.
    target = os.path.join(workDir, "via-gh")
    outcome = {}

    def ghClone():
        try:
            logic.gh(["repo", "clone", PUBLIC_REPO, target], timeout=CLONE_TIMEOUT_SECONDS)
            outcome["error"] = None
        except Exception as e:
            outcome["error"] = str(e).splitlines()[0]

    import threading
    t0 = time.time()
    worker = threading.Thread(target=ghClone, daemon=True)
    worker.start()
    worker.join(CLONE_TIMEOUT_SECONDS + 30)
    if worker.is_alive():
        record("gh repo clone", False,
               f"still blocked after {time.time() - t0:.0f}s (gh timed out and its children never exited)")
        killProcessesMentioning(target)
        worker.join(30)
    elif outcome.get("error"):
        record("gh repo clone", False, f"after {time.time() - t0:.0f}s: {outcome['error']}")
        killProcessesMentioning(target)
    else:
        record("gh repo clone", os.path.isdir(os.path.join(target, ".git")), f"{time.time() - t0:.1f}s")

    # --- 3. GitPython clone inside Slicer's own environment with the resolved git ---
    target = os.path.join(workDir, "via-gitpython")
    t0 = time.time()
    try:
        import git
        # GitPython rejects kill_after_timeout on Windows ("feature is not supported"), so the
        # clone runs unbounded there; the job's timeout-minutes is the backstop.
        options = {} if os.name == "nt" else {"kill_after_timeout": CLONE_TIMEOUT_SECONDS}
        git.Repo.clone_from(f"https://github.com/{PUBLIC_REPO}.git", target, **options)
        ok = os.path.isdir(os.path.join(target, ".git"))
        record("GitPython clone", ok, f"{time.time() - t0:.1f}s")
    except Exception as e:
        record("GitPython clone", False, f"after {time.time() - t0:.0f}s: {str(e).splitlines()[0]}")
        killProcessesMentioning(target)

    shutil.rmtree(workDir, ignore_errors=True)


exitCode = 1
try:
    main()
    failed = [name for name, ok, _ in results if not ok]
    say(f"[smoke] {len(results) - len(failed)}/{len(results)} passed"
        + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    exitCode = 1 if (failed or not results) else 0
except Exception:
    traceback.print_exc()
    say("[smoke] FAIL  unexpected error: " + traceback.format_exc().strip().splitlines()[-1])
finally:
    sys.stdout.flush()
    slicer.util.exit(exitCode)
