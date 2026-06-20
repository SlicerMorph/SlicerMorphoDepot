"""One-line MCP entry point: exec this file; it (re)loads the harness + tests,
runs them, and leaves the result in __result. Re-exec after editing either file."""
import os
import slicer
_here = "/Users/amaga/Desktop/MorphoDepotOrg/SlicerMorphoDepot/MorphoDepot/Testing/Python"
exec(open(os.path.join(_here, "md_harness.py")).read(), globals())
exec(open(os.path.join(_here, "md_tests.py")).read(), globals())

H = Harness()
H.patchDialogs(default=True)          # auto-answer + record any dialog a test trips
try:
    __result = run_tests(TESTS)
finally:
    H.restoreDialogs()
