"""MorphoDepot UI-driving test harness (runs INSIDE Slicer via the MCP).

Signal-level, testingMode-OFF driving of the real widget. Modal dialogs are
monkeypatched (not racing a timer against a blocking nested loop): the real
wiring and branching still run; only the human click is simulated by a queued
answer, and every dialog call is recorded so tests can assert it appeared.

Usage (from the MCP):
    exec(open(".../Testing/Python/md_harness.py").read(), globals())
    H = Harness()          # grabs the live widget
then drive H.* and assert on widget state / H.dialogLog.
"""
import slicer
import qt


class Harness:
    # slicer.util helpers that RETURN a bool (a user yes/no/ok-cancel choice)
    BOOL_DIALOGS = ["confirmOkCancelDisplay", "confirmYesNoDisplay",
                    "confirmRetryCloseDisplay"]
    # slicer.util helpers that just SHOW something (return None)
    VOID_DIALOGS = ["messageBox", "warningDisplay", "errorDisplay", "infoDisplay"]

    def __init__(self):
        self.w = slicer.modules.morphodepot.widgetRepresentation().self()
        self.logic = self.w.logic
        self.dialogLog = []
        self._answers = []
        self._default = True
        self._orig = {}

    # ---- modal-dialog monkeypatching --------------------------------------
    def patchDialogs(self, answers=None, default=True):
        """Install stubs for the slicer.util dialog helpers. `answers` is a FIFO
        queue of return values consumed by the bool dialogs (a workflow may pop
        several); `default` is used once the queue is empty. Resets the log."""
        import slicer.util as su
        self.dialogLog = []
        self._answers = list(answers or [])
        self._default = default
        for n in self.BOOL_DIALOGS + self.VOID_DIALOGS:
            self._orig.setdefault(n, getattr(su, n, None))

        def mkbool(name):
            def f(text="", *a, **k):
                self.dialogLog.append({"fn": name, "text": str(text)})
                return self._answers.pop(0) if self._answers else self._default
            return f

        def mkvoid(name):
            def f(text="", *a, **k):
                self.dialogLog.append({"fn": name, "text": str(text)})
                return None
            return f

        for n in self.BOOL_DIALOGS:
            if self._orig.get(n) is not None:
                setattr(su, n, mkbool(n))
        for n in self.VOID_DIALOGS:
            if self._orig.get(n) is not None:
                setattr(su, n, mkvoid(n))

    def restoreDialogs(self):
        import slicer.util as su
        for n, fn in self._orig.items():
            if fn is not None:
                setattr(su, n, fn)

    def shown(self):
        """Names of the dialogs shown since the last patchDialogs()."""
        return [d["fn"] for d in self.dialogLog]

    def shownTextContains(self, needle):
        return any(needle.lower() in d["text"].lower() for d in self.dialogLog)

    # ---- navigation / driving ---------------------------------------------
    def goTab(self, name):
        tw = self.w.tabWidget
        for i in range(tw.count):
            if tw.tabText(i) == name:
                tw.currentIndex = i
                slicer.app.processEvents()
                return True
        raise AssertionError(f"no tab named {name!r}")

    def pump(self, ms=50):
        """Let the event loop run briefly (for debounced timers etc.)."""
        import time
        end = time.time() + ms / 1000.0
        while time.time() < end:
            slicer.app.processEvents()

    def click(self, button):
        button.click()
        slicer.app.processEvents()

    # ---- create-form helpers ----------------------------------------------
    def form(self):
        return self.w.createUI.accessionForm

    def setQ(self, key, value):
        """Set an accession-form answer the way a human would, so the connected
        validator fires: CLICK option buttons (setAnswer only sets .checked and
        does NOT emit clicked), set .text on line edits (emits textChanged)."""
        q = self.form().questions[key]
        if hasattr(q, "optionButtons"):
            if isinstance(value, (list, tuple)):          # checkboxes
                for opt, btn in q.optionButtons.items():
                    if btn.checked != (opt in value):
                        btn.click()
            else:                                          # radio
                q.optionButtons[value].click()
        else:                                              # text / species
            q.answerText.text = value
        slicer.app.processEvents()

    def fillValidForm(self, name="test-harness-repo", shortTerm=True):
        """Populate every required accession field with valid values (clicking,
        so validators run). Leaves the form in a should-be-valid state."""
        f = self.form()
        self.setQ("subjectType", "Biological specimen")
        self.setQ("specimenSource", "Non-accessioned")
        self.setQ("species", "Testudo exempli")
        self.setQ("biologicalSex", "Unknown")
        self.setQ("developmentalStage", "Adult")
        self.setQ("modality", "Micro CT (or synchrotron)")
        self.setQ("contrastEnhancement", "No")
        self.setQ("imageContents", "Whole specimen")
        f.userEditedRepoName = True            # keep our explicit name (F4 won't overwrite)
        f.questions["githubRepoName"].answerText.text = name
        self.setQ("redistributionAcknowledgement",
                  ["I have the right to allow redistribution of this data."])
        (self.w.createUI.shortTermRadio if shortTerm
         else self.w.createUI.archivalRadio).click()
        self.pump(80)

    def resetNameField(self):
        """Clear the repo-name field + F4 state so a fresh suggestion can apply."""
        f = self.form()
        f.userEditedRepoName = False
        f._lastSuggestedName = ""
        f.questions["githubRepoName"].answerText.text = ""
        slicer.app.processEvents()


def run_tests(tests):
    """tests: list of (name, fn). Each fn() asserts; returns a results dict."""
    out = {"pass": 0, "fail": 0, "cases": []}
    for name, fn in tests:
        rec = {"name": name}
        try:
            fn()
            rec["ok"] = True
            out["pass"] += 1
        except Exception as e:
            import traceback
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
            rec["tb"] = traceback.format_exc().splitlines()[-3:]
            out["fail"] += 1
        out["cases"].append(rec)
    return out
