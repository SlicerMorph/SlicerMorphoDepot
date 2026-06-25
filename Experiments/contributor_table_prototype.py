"""MorphoDepot — contributor data entry via Slicer table nodes (PROTOTYPE / NON-PRODUCTION).

NOT production code. The shipped implementation lives in `MorphoDepot/MorphoDepotLib/contributors.py`;
this file is a kept exploration only. The hardcoded `REPO` below is a throwaway example.

Investigates using Slicer's native table functionality (vtkMRMLTableNode + qMRMLTableView,
the built-in Tables module) as the curator-facing data-entry UI for dataset credit — instead of
a hand-edited YAML/Markdown file.

Why this fits:
  * The curator edits a spreadsheet-like GRID inside Slicer (double-click a cell), never raw syntax.
  * "Author?" is a vtkBitArray column -> renders as a real CHECKBOX.
  * The "GitHub" column is the auto key -> locked read-only (setFirstColumnLocked).
  * Insert/Delete-row + copy/paste come free from qMRMLTableView / the Tables module.
  * Persists as TSV via the node's storage node -> forgiving, round-trips, nothing fragile to break.

Run inside Slicer's Python console:
    exec(open('.../Experiments/contributor_table_prototype.py').read())
After it runs, `report` holds the credit resolved FROM the table (what would feed Zenodo/README).
"""

import slicer
import vtk

ORG = "MorphoDepot"
REPO = "MorphoDepot/Ariopsis-felis-cranium"
ISSUES_URL = "https://github.com/%s/issues/" % REPO


# ---- tear down any previous demo run ----
for pattern in ("MD Contributors*", "MD Contributions*"):
    for node in list(slicer.util.getNodes(pattern).values()):
        slicer.mrmlScene.RemoveNode(node)


def _str_col(name, values):
    a = vtk.vtkStringArray()
    a.SetName(name)
    for v in values:
        a.InsertNextValue(v)
    return a


def _bit_col(name, values):
    a = vtk.vtkBitArray()
    a.SetName(name)
    for v in values:
        a.InsertNextValue(1 if v else 0)
    return a


# ===================== People (identity) table — CURATOR-EDITED =====================
# Auto-seeded when a contribution is accepted: GitHub handle ALWAYS; Name/ORCID auto ONLY for
# org members (verified at ORCID onboarding). The rows below simulate that seeded state:
#   members  -> pre-filled (auto)
#   non-members -> the curator fills Name/ORCID by hand (some done, one still blank/unresolved)
#   GitHub, name, ORCID, author?
people = [
    ("evasquez",      "Vasquez, Elena M.", "0000-0002-1825-0097", True),   # member (auto) - curator
    ("mlindgren",     "Lindgren, Marcus",  "0000-0001-7654-3210", True),   # member (auto) - promoted
    ("tbecker",       "Becker, Tomas",     "0000-0003-2233-1980", False),  # non-member - curator filled
    ("pnair-fishlab", "Nair, Priya",       "",                    False),  # non-member - ORCID missing
    ("jwright99",     "",                  "",                    False),  # non-member - UNRESOLVED
]
peopleNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", "MD Contributors (People)")
peopleNode.AddColumn(_str_col("GitHub", [p[0] for p in people]))
peopleNode.AddColumn(_str_col("Name (Family, Given)", [p[1] for p in people]))
peopleNode.AddColumn(_str_col("ORCID", [p[2] for p in people]))
peopleNode.AddColumn(_bit_col("Author?", [p[3] for p in people]))

# ===================== Contributions (AUTO, read-only): issue -> contributor =====================
# Derived from merged issue-PRs; the curator does NOT edit this. One row per (issue, contributor);
# a person repeats across issues. Number links to the real issue.
contribs = [
    ("5",  "jwright99",     "v2"),
    ("8",  "tbecker",       "v1"),
    ("11", "pnair-fishlab", "v2"),
    ("12", "tbecker",       "v2"),
]
contribNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", "MD Contributions (auto)")
contribNode.AddColumn(_str_col("Issue", ["#" + c[0] for c in contribs]))
contribNode.AddColumn(_str_col("Contributor", ["@" + c[1] for c in contribs]))
contribNode.AddColumn(_str_col("Release", [c[2] for c in contribs]))

# ===================== Show the editable People table in the main window =====================
slicer.util.selectModule("Tables")
try:
    slicer.modules.tables.widgetRepresentation().setCurrentTableNode(peopleNode)
except Exception as e:  # pragma: no cover - UI hookup is best-effort
    print("setCurrentTableNode:", e)

# Large central table view, with the auto GitHub key column locked read-only.
layoutManager = slicer.app.layoutManager()
try:
    layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpTableView)
    tableView = layoutManager.tableWidget(0).tableView()
    tableView.setMRMLTableNode(peopleNode)
    tableView.setFirstColumnLocked(True)   # GitHub handle is auto -> not editable
except Exception as e:  # pragma: no cover
    print("layout:", e)

slicer.app.processEvents()


# ===================== Read the table back -> resolved credit (feeds Zenodo/README) =====================
def read_people(node):
    table = node.GetTable()
    rows = []
    for r in range(table.GetNumberOfRows()):
        rows.append(dict(
            github=table.GetValueByName(r, "GitHub").ToString(),
            name=table.GetValueByName(r, "Name (Family, Given)").ToString(),
            orcid=table.GetValueByName(r, "ORCID").ToString(),
            author=bool(table.GetValueByName(r, "Author?").ToInt()),
        ))
    return rows


ppl = read_people(peopleNode)
authors = [p for p in ppl if p["author"]]
contributors = [p for p in ppl if not p["author"]]
unresolved = [p["github"] for p in ppl if not p["name"]]

out = ["CREDIT RESOLVED FROM THE TABLE (what would feed Zenodo / the README):", ""]
out.append("Authors (cited):")
for a in authors:
    tail = " <%s>" % a["orcid"] if a["orcid"] else ("  <-- NO NAME: blocks citation" if not a["name"] else "")
    out.append("  - %s%s" % (a["name"] or "@" + a["github"], tail))
out.append("Contributors (acknowledged):")
for c in contributors:
    disp = c["name"] if c["name"] else "@%s  (name not provided -> credited by handle)" % c["github"]
    out.append("  - %s" % disp)
out.append("")
out.append("Unresolved rows the curator must complete: %s" % (", ".join("@" + u for u in unresolved) or "none"))
report = "\n".join(out)
print(report)
