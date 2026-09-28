#!/usr/bin/env python3
"""Unit tests for the #238 term-lookup integration: the page address, the names sent to the page,
and the checks that run at staging/release (baseline vs. color table, species the table was built for).

Run: python3 MorphoDepot/Testing/Python/test_termlookup.py   (no Slicer, no deps, no network)

widget_validation.py and widget_termlookup.py import slicer/qt at module level, so minimal stand-ins
are installed in sys.modules first; the logic under test only calls the handful of node methods faked
below.  The dialog itself (qSlicerWebWidget) needs a running Slicer and is not covered here.
"""
import importlib.util
import sys
import types
import urllib.parse
from pathlib import Path

# --- stand-ins for Slicer ------------------------------------------------------------------------
dialogs = []
slicerStub = types.ModuleType("slicer")
slicerStub.util = types.SimpleNamespace(
    settingsValue=lambda key, default: default,
    errorDisplay=lambda text, windowTitle="": dialogs.append(("error", windowTitle, text)),
    confirmOkCancelDisplay=lambda text, windowTitle="": dialogs.append(("confirm", windowTitle, text)) or confirmAnswer[0],
)
confirmAnswer = [True]
i18n = types.ModuleType("slicer.i18n")
i18n.tr = lambda text: text
slicerStub.i18n = i18n
slicerStub.vtkSegment = types.SimpleNamespace(GetTerminologyEntryTagName=lambda: "TerminologyEntry")
sys.modules["slicer"] = slicerStub
sys.modules["slicer.i18n"] = i18n
sys.modules["qt"] = types.ModuleType("qt")

_LIB = Path(__file__).resolve().parents[2] / "MorphoDepotLib"
sys.modules["MorphoDepotLib"] = types.ModuleType("MorphoDepotLib")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"MorphoDepotLib.{name}", _LIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"MorphoDepotLib.{name}"] = module
    spec.loader.exec_module(module)
    return module


termlookup = _load("widget_termlookup")
validation = _load("widget_validation")


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


# --- fake nodes ----------------------------------------------------------------------------------
TISSUE = "~SCT^85756007^Tissue~SCT^85756007^Tissue~^^~~^^~^^"
ANATOMY = "SCT^123037004^Anatomical Structure"


def uberon(code, label):
    return f"~{ANATOMY}~UBERON^http://purl.obolibrary.org/obo/UBERON_{code}^{label}~^^~~^^~^^"


class FakeColor:
    def __init__(self, name, entries, kind="File"):
        self.name, self.entries, self.kind, self.attributes = name, entries, kind, {}

    def GetName(self): return self.name
    def GetTypeAsString(self): return self.kind
    def GetNumberOfColors(self): return len(self.entries)
    def GetColorName(self, i): return self.entries[i][0]
    def GetTerminologyAsString(self, i): return self.entries[i][1]
    def GetAttribute(self, key): return self.attributes.get(key)


class FakeSegment:
    def __init__(self, name, terminology=""):
        self.name, self.terminology = name, terminology

    def GetName(self): return self.name
    def GetTerminology(self): return self.terminology


class FakeSegmentation:
    def __init__(self, name, segments):
        self.name, self.segments = name, segments

    def GetName(self): return self.name
    def GetSegmentation(self): return self
    def GetNumberOfSegments(self): return len(self.segments)
    def GetNthSegment(self, i): return self.segments[i]


class Widget(validation.ValidationMixin):
    testingMode = False


w = Widget()
mouseTable = FakeColor("Mouse_heart", [
    ("Background", "~^^~^^~^^~~^^~^^"),
    ("atrium", uberon("0002081", "cardiac atrium")),
    ("4th ventricle", uberon("0002422", "fourth ventricle")),
    ("skull", TISSUE),
])
labels = FakeColor("Labels", [("Black", ""), ("jake", ""), ("Peach", "")], kind="Labels")


print("page address")
url = termlookup.buildTermLookupUrl(species="Mus musculus", terms=["atrium", "4th ventricle"],
                                    tableNameSuggestion="mouse-heart")
query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
check("starts at the term-lookup site", url.startswith(termlookup.TERM_LOOKUP_URL + "?"))
check("embedded mode is requested", query.get("embedded") == ["1"])
check("species survives encoding", query.get("species") == ["Mus musculus"])
check("names are joined with | and survive encoding", query.get("terms") == ["atrium|4th ventricle"])
check("table name is passed", query.get("name") == ["mouse-heart"])
nonBioQuery = urllib.parse.parse_qs(urllib.parse.urlparse(
    termlookup.buildTermLookupUrl(species="ignored", nonBio=True)).query)
check("non-biological sends nonbio=1 and no species", nonBioQuery.get("nonbio") == ["1"] and "species" not in nonBioQuery)

print("reading the page's answer")
X = termlookup._EXPORT_JS
check("the widget's own evalJS (webkitHidden) is ignored", termlookup.readExportResult("document.webkitHidden = false", "false") == (False, None))
ours, data = termlookup.readExportResult(X, '{"ready": true, "name": "t"}')
check("our request + export JSON -> accepted", ours and data == {"ready": True, "name": "t"})
check("our request + unreadable answer -> ours, no data", termlookup.readExportResult(X, "") == (True, None))
check("our request + non-object answer -> ours, no data", termlookup.readExportResult(X, "false") == (True, None))
ours, data = termlookup.readExportResult(None, '{"ready": false, "reason": "x"}')
check("signal without js: export-shaped answer is accepted", ours and data["reason"] == "x")
check("signal without js: anything else is ignored silently", termlookup.readExportResult(None, "false") == (False, None))

print("table name from the page")
check("a valid name is kept", termlookup.safeTableName("Mus_musculus-brain.v2") == "Mus_musculus-brain.v2")
check("path separators cannot escape the folder", "/" not in termlookup.safeTableName("../../etc/passwd"))
check("dots-only falls back to the default", termlookup.safeTableName("..") == "terminology_color_table")
check("spaces are replaced", termlookup.safeTableName("mouse brain") == "mouse_brain")
check("empty falls back to the default", termlookup.safeTableName("") == "terminology_color_table")

print("names sent to the page")
baseline = FakeSegmentation("baseline", [FakeSegment("atrium"), FakeSegment(" Atrium "), FakeSegment(""),
                                         FakeSegment("skull")])
check("baseline segments, trimmed and de-duplicated", w._termLookupNames(baseline, mouseTable) == ["atrium", "skull"])
check("no baseline: the table's entries (not label 0)", w._termLookupNames(None, mouseTable) == ["atrium", "4th ventricle", "skull"])
check("empty baseline falls back to the table", w._termLookupNames(FakeSegmentation("e", []), mouseTable) == ["atrium", "4th ventricle", "skull"])
check("Slicer's built-in Labels table is never used", w._termLookupNames(None, labels) == [])
check("a shipped file table (GenericAnatomyColors) is never used",
      w._termLookupNames(None, FakeColor("GenericAnatomyColors", [("", ""), ("heart", TISSUE)])) == [])

print("term parsing")
check("type fields from a color entry", w._terminologyTypeFields(uberon("0002081", "cardiac atrium"))
      == ("UBERON", "http://purl.obolibrary.org/obo/UBERON_0002081", "cardiac atrium"))
check("no type code -> None", w._terminologyTypeFields("~^^~^^~^^~~^^~^^") is None)
check("empty -> None", w._terminologyTypeFields("") is None)

print("baseline vs color table")
matching = FakeSegmentation("baseline", [FakeSegment("Atrium", "Some context" + uberon("0002081", "cardiac atrium")),
                                         FakeSegment("skull", TISSUE)])
check("matching names (any case) and terms -> no problems", w._baselineColorTableMismatches(matching, mouseTable) == [])
placeholders = FakeSegmentation("baseline", [FakeSegment("1"), FakeSegment("2"), FakeSegment("2a")])
check("placeholder names 1, 2, 2a are all reported", len(w._baselineColorTableMismatches(placeholders, mouseTable)) == 3)
wrongTerm = FakeSegmentation("baseline", [FakeSegment("atrium", TISSUE)])
problems = w._baselineColorTableMismatches(wrongTerm, mouseTable)
check("a different term is reported with both labels", len(problems) == 1 and "'Tissue'" in problems[0] and "cardiac atrium" in problems[0])
noTerm = FakeSegmentation("baseline", [FakeSegment("atrium", "")])
check("a segment with no term is reported", "has no term" in w._baselineColorTableMismatches(noTerm, mouseTable)[0])
check("a table entry with no term: the name match is enough",
      w._baselineColorTableMismatches(FakeSegmentation("b", [FakeSegment("jake")]), labels) == [])

dialogs.clear()
check("Organizational + mismatch -> blocked", w._confirmBaselineMatchesColorTable(placeholders, mouseTable, True, "staged") is False)
check("... with an error naming the rule", dialogs and dialogs[-1][0] == "error" and "can't be staged" in dialogs[-1][2])
dialogs.clear()
confirmAnswer[0] = False
check("Personal + mismatch + Cancel -> stops", w._confirmBaselineMatchesColorTable(placeholders, mouseTable, False, "staged") is False)
confirmAnswer[0] = True
check("Personal + mismatch + OK -> continues", w._confirmBaselineMatchesColorTable(placeholders, mouseTable, False, "staged") is True)
check("... after a warning, not an error", all(kind == "confirm" for kind, _, _ in dialogs))
dialogs.clear()
check("matching baseline -> no dialog", w._confirmBaselineMatchesColorTable(matching, mouseTable, True, "staged") and not dialogs)

print("species the table was built for")
built = FakeColor("t", [("", "")])
check("table not from the tool -> passes silently", w._confirmColorTableSpecies(built, "Mus musculus", False) and not dialogs)
built.attributes[termlookup.SPECIES_ATTRIBUTE] = "Mus musculus"
check("same species (spacing/case differ) -> no warning", w._confirmColorTableSpecies(built, " mus  Musculus", False) and not dialogs)
confirmAnswer[0] = False
check("different species -> warns, Cancel stops", w._confirmColorTableSpecies(built, "Rattus norvegicus", False) is False)
check("... and says what it was built for", "'Mus musculus'" in dialogs[-1][2] and "'Rattus norvegicus'" in dialogs[-1][2])
dialogs.clear()
check("now non-biological -> warns", w._confirmColorTableSpecies(built, "", True) is False and "non-biological" in dialogs[-1][2])
confirmAnswer[0] = True
built.attributes[termlookup.SPECIES_ATTRIBUTE] = termlookup.NON_BIOLOGICAL
dialogs.clear()
check("built non-biological, still non-biological -> no warning", w._confirmColorTableSpecies(built, "", True) and not dialogs)

print("all passed")
