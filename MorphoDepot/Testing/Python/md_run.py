"""One-line MCP entry point: exec this file; it (re)loads the harness + tests,
runs them, and leaves the result in __result. Re-exec after editing either file."""
import os
import slicer
# Derive our own location from the installed module path (portable; works under
# exec(open(...).read()) where __file__ is not set).
_here = os.path.join(os.path.dirname(slicer.util.modulePath("MorphoDepot")), "Testing", "Python")
exec(open(os.path.join(_here, "md_harness.py")).read(), globals())
exec(open(os.path.join(_here, "md_tests.py")).read(), globals())

H = Harness()
H.patchDialogs(default=True)          # auto-answer + record any dialog a test trips
try:
    __result = run_tests(TESTS)
finally:
    H.restoreDialogs()
