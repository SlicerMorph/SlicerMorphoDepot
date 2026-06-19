"""Contributor credit for MorphoDepot archival datasets.

The committed source of truth is ``CONTRIBUTORS.json`` at the repo root (see ``docs/org-design.md``
Sec.9.6). This module is the data + credit layer that the release flow, the README/DOI generator, and
the Zenodo mint all build on.

Design:
  * The JSON / credit-resolution core is **Slicer-free** (only ``json``/``os``) so it can be unit-tested
    in plain python3.
  * The ``vtkMRMLTableNode`` glue (the curator's editing grid) is in the "Slicer glue" section and
    imports ``slicer``/``vtk`` lazily, so importing this module never requires Slicer.

The ``people`` list is the single record of *who* is credited (author flag per person); ``contributions``
is the auto, issue-keyed provenance (one row per merged ``issue-N`` PR). A person with no ``name`` is
credited by ``@handle``; only an ``author`` (cited) requires a real name.
"""
from __future__ import annotations

import json
import os
import urllib.request

SCHEMA = "morphodepot-contributors/1"
ORCID_PUBLIC_API = "https://pub.orcid.org/v3.0"  # authoritative given/family split, by the iD
DOC_URL = "https://morphodepot.org/docs/contributors"  # placeholder — credit/contributors documentation

# People-grid columns (the editable curator surface). GitHub is the locked auto key.
COL_GITHUB = "GitHub"
COL_NAME = "Name (Family, Given)"
COL_ORCID = "ORCID"
COL_AFFIL = "Affiliation"
COL_AUTHOR = "Author?"
PEOPLE_COLUMNS = [COL_GITHUB, COL_NAME, COL_ORCID, COL_AFFIL, COL_AUTHOR]

# Contributions-grid columns (auto, read-only).
CONTRIB_COLUMNS = ["Issue", "Contributor", "Release"]


# ======================================================================================
# core: schema + IO (Slicer-free)
# ======================================================================================

def new_record(repo: str) -> dict:
    """An empty contributor record for ``repo`` (e.g. ``MorphoDepot/Name``)."""
    return {"schema": SCHEMA, "repo": repo, "people": [], "contributions": []}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("people", [])
    data.setdefault("contributions", [])
    return data


def dumps(data: dict) -> str:
    """Canonical serialization: sorted keys + 2-space indent → stable per-release git diffs.

    Sorting affects dict *keys* only; the order of the ``people`` list (which sets citation/author
    order) is preserved as given.
    """
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def save(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(dumps(data))


# ======================================================================================
# identity helpers (Slicer-free): format a member's name as Zenodo's "Family, Given"
# ======================================================================================

def orcid_name(orcid: str):
    """Authoritative ``(given, family)`` from the public ORCID record — best-effort (None, None)."""
    try:
        req = urllib.request.Request(f"{ORCID_PUBLIC_API}/{orcid}/person",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            nm = json.load(r).get("name") or {}
        return ((nm.get("given-names") or {}).get("value"),
                (nm.get("family-name") or {}).get("value"))
    except Exception:
        return None, None


def zenodo_name(display_name: str, given: str | None, family: str | None) -> str:
    """Format as ``Family, Given``: prefer the ORCID split, else split the display name."""
    if family and given:
        return f"{family}, {given}"
    if family:
        return family
    parts = (display_name or "").split()
    if len(parts) >= 2:
        return f"{parts[-1]}, {' '.join(parts[:-1])}"
    return display_name or "Unknown"


# ======================================================================================
# core: mutation
# ======================================================================================

def _person_key(person: dict):
    """Stable identity for a people row: the GitHub login if present, else the name."""
    gh = (person.get("github") or "").strip().lower()
    if gh:
        return ("github", gh)
    return ("name", (person.get("name") or "").strip().lower())


def find_person(data: dict, *, github: str | None = None, name: str | None = None) -> dict | None:
    target = _person_key({"github": github, "name": name})
    if target == ("name", ""):
        return None
    for person in data["people"]:
        if _person_key(person) == target:
            return person
    return None


def ensure_person(data: dict, *, github: str | None = None, github_id=None, name: str | None = None,
                  orcid: str | None = None, affiliation: str | None = None,
                  author: bool | None = None, source: str | None = None) -> dict:
    """Get-or-create a people row, filling any newly-supplied fields without clobbering existing ones."""
    person = find_person(data, github=github, name=name)
    if person is None:
        person = {"github": github, "github_id": github_id, "name": name, "orcid": orcid,
                  "affiliation": affiliation, "author": bool(author), "source": source}
        data["people"].append(person)
        return person
    if github_id is not None and not person.get("github_id"):
        person["github_id"] = github_id
    for field, value in (("name", name), ("orcid", orcid), ("affiliation", affiliation),
                         ("source", source)):
        if value and not person.get(field):
            person[field] = value
    if author is not None:
        person["author"] = bool(author)
    return person


def add_contribution(data: dict, *, issue: int, by: str, url: str | None = None,
                     release: str | None = None) -> bool:
    """Append an ``(issue, contributor)`` provenance row (deduped) and ensure a people row exists.

    Returns True if a new contribution row was added. ``by`` is the PR author's GitHub login.
    """
    issue = int(issue)
    by_norm = (by or "").strip().lower()
    for c in data["contributions"]:
        if int(c.get("issue", -1)) == issue and (c.get("by") or "").strip().lower() == by_norm:
            return False  # already recorded
    if url is None and data.get("repo"):
        url = f"https://github.com/{data['repo']}/issues/{issue}"
    data["contributions"].append({"issue": issue, "by": by, "url": url, "release": release})
    ensure_person(data, github=by, source="non-member")
    return True


def stamp_release(data: dict, release: str) -> int:
    """Assign ``release`` to every contribution still pending (``release`` is None). Returns count."""
    n = 0
    for c in data["contributions"]:
        if not c.get("release"):
            c["release"] = release
            n += 1
    return n


# ======================================================================================
# core: credit resolution (what feeds the README + Zenodo)
# ======================================================================================

def _display(person: dict) -> str:
    return person.get("name") or ("@" + person["github"] if person.get("github") else "(unknown)")


def issues_for(data: dict, person: dict):
    gh = (person.get("github") or "").strip().lower()
    if not gh:
        return []
    return sorted({int(c["issue"]) for c in data["contributions"]
                   if (c.get("by") or "").strip().lower() == gh})


def resolve(data: dict) -> dict:
    """Resolve the record into authors / contributors / unresolved, for display and validation.

    - authors: people with ``author`` true (a missing name is flagged — it blocks citation).
    - contributors: everyone else, each with the issue numbers they contributed.
    - unresolved: people with no name (credited by handle until the curator fills it in).
    """
    authors, contributors, unresolved = [], [], []
    for person in data["people"]:
        entry = {"display": _display(person), "github": person.get("github"),
                 "orcid": person.get("orcid"), "name": person.get("name"),
                 "issues": issues_for(data, person)}
        if person.get("author"):
            entry["name_missing"] = not person.get("name")
            authors.append(entry)
        else:
            contributors.append(entry)
        if not person.get("name") and not person.get("curator"):
            unresolved.append(person.get("github") or "(unnamed offline row)")
    return {"authors": authors, "contributors": contributors, "unresolved": unresolved}


def zenodo_metadata(data: dict) -> dict:
    """Map the record to Zenodo ``creators`` / ``contributors`` (names already 'Family, Given')."""
    def person_obj(person):
        obj = {"name": person.get("name") or "@" + (person.get("github") or "unknown")}
        if person.get("orcid"):
            obj["orcid"] = person["orcid"]
        if person.get("affiliation"):
            obj["affiliation"] = person["affiliation"]
        return obj

    creators = [person_obj(p) for p in data["people"] if p.get("author")]
    contributors = []
    for p in data["people"]:
        if not p.get("author"):
            obj = person_obj(p)
            obj["type"] = "DataCollector"
            contributors.append(obj)
    return {"creators": creators, "contributors": contributors}


def validation_issues(data: dict) -> list:
    """Hard problems that should block a release. Empty list == the gate auto-satisfies.

    The curator (``curator: true``) is exempt: their name is filled authoritatively by the App from the
    onboarding record, so a blank name there is expected and must not block. Only a *promoted* co-author
    the curator named by hand must have a real name."""
    problems = []
    for p in data["people"]:
        if p.get("author") and not p.get("name") and not p.get("curator"):
            problems.append(f"author '{p.get('github') or '?'}' has no real name (required to cite)")
    return problems


def render_markdown(data: dict) -> str:
    """Render the credit section (Authors + Contributors tables) as Markdown, from the record.

    Slicer-free; used by the release README generator (and shareable elsewhere). Returns "" if there
    is nobody to credit. Each contributor is credited by the issue number(s) they segmented, linked.
    """
    res = resolve(data)
    if not res["authors"] and not res["contributors"]:
        return ""
    url_for = {}
    for c in data.get("contributions", []):
        url_for[((c.get("by") or "").strip().lower(), int(c["issue"]))] = c.get("url")

    lines = ["## Contributors", ""]
    if res["authors"]:
        lines += ["**Authors**", "", "| Name | ORCID |", "|---|---|"]
        for a in res["authors"]:
            lines.append(f"| {a['display']} | {a.get('orcid') or '-'} |")
        lines.append("")
    if res["contributors"]:
        lines += ["**Contributors** — credited by the issue(s) they segmented", "",
                  "| Name | Issues |", "|---|---|"]
        for c in res["contributors"]:
            gh = (c.get("github") or "").strip().lower()
            links = []
            for n in c["issues"]:
                u = url_for.get((gh, n))
                links.append(f"[#{n}]({u})" if u else f"#{n}")
            lines.append(f"| {c['display']} | {', '.join(links) or '-'} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ======================================================================================
# Slicer glue: the editable people grid (vtkMRMLTableNode) — imports slicer/vtk lazily
# ======================================================================================

def _str_col(name, values):
    import vtk
    col = vtk.vtkStringArray()
    col.SetName(name)
    for v in values:
        col.InsertNextValue("" if v is None else str(v))
    return col


def _bit_col(name, values):
    import vtk
    col = vtk.vtkBitArray()
    col.SetName(name)
    for v in values:
        col.InsertNextValue(1 if v else 0)
    return col


def to_people_table(data: dict, nodeName: str = "MorphoDepot Contributors", people=None):
    """Build a vtkMRMLTableNode holding the editable People grid. Returns the node.

    ``people`` defaults to ``data['people']``; pass a subset to exclude rows from the grid (e.g. the
    curator, whose authoritative info is filled by the App and must not be hand-typed)."""
    import slicer
    people = data["people"] if people is None else people
    node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", nodeName)
    node.AddColumn(_str_col(COL_GITHUB, [p.get("github") for p in people]))
    node.AddColumn(_str_col(COL_NAME, [p.get("name") for p in people]))
    node.AddColumn(_str_col(COL_ORCID, [p.get("orcid") for p in people]))
    node.AddColumn(_str_col(COL_AFFIL, [p.get("affiliation") for p in people]))
    node.AddColumn(_bit_col(COL_AUTHOR, [p.get("author") for p in people]))
    return node


def from_people_table(node, existing=None) -> list:
    """Read the edited People grid into a LIST of people dicts (matching rows by GitHub, else name).

    Auto fields not shown in the grid (github_id, source) are preserved by matching to ``existing``;
    rows the curator added by hand become new offline entries. Returns the list (caller assembles
    ``data['people']``, e.g. re-prepending the curator)."""
    existingByKey = {_person_key(p): p for p in (existing or [])}
    table = node.GetTable()
    rebuilt = []
    for r in range(table.GetNumberOfRows()):
        def cell(col):
            return table.GetValueByName(r, col).ToString().strip()

        github = cell(COL_GITHUB) or None
        name = cell(COL_NAME) or None
        person = dict(existingByKey.get(_person_key({"github": github, "name": name}), {}))
        person["github"] = github
        person["name"] = name
        person["orcid"] = cell(COL_ORCID) or None
        person["affiliation"] = cell(COL_AFFIL) or None
        person["author"] = bool(table.GetValueByName(r, COL_AUTHOR).ToInt())
        person.setdefault("source", "offline" if not github else "non-member")
        rebuilt.append(person)
    return rebuilt


def to_contributions_table(data: dict, nodeName: str = "MorphoDepot Contributions"):
    """Build a read-only vtkMRMLTableNode of the auto issue→contributor provenance."""
    import slicer
    node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", nodeName)
    contribs = sorted(data["contributions"], key=lambda c: (str(c.get("release") or "~"), int(c["issue"])))
    node.AddColumn(_str_col("Issue", ["#%s" % c["issue"] for c in contribs]))
    node.AddColumn(_str_col("Contributor", ["@%s" % c["by"] for c in contribs]))
    node.AddColumn(_str_col("Release", [c.get("release") or "(pending)" for c in contribs]))
    return node


def make_contributor_panel(data: dict, title: str = "Contributors & credit",
                           show_header: bool = True, show_contributions: bool = True):
    """Build a self-contained Qt widget for curating credit — the reusable surface for both the
    Create-tab baseline-credit panel and the Release-tab gate (org-design Sec.9.7).

    Returns a lightweight controller (``types.SimpleNamespace``) — PythonQt forbids attaching
    attributes to a raw QWidget, so the controller holds them instead:
      - ``widget`` — the ``qt.QWidget`` to embed in a tab/dialog
      - ``data`` / ``peopleNode`` / ``contribNode`` — the backing record + table nodes
      - ``syncFromTable()`` — read the grid edits back into ``data`` and refresh the status line
      - ``saveTo(path)`` — sync, then write canonical ``CONTRIBUTORS.json``
      - ``isReady()`` — True when the release gate may proceed (no author missing a name)
      - ``statusLabel`` — the live status QLabel

    This is a factory (not a QWidget subclass) so importing this module never requires Qt/Slicer.
    """
    import types

    import qt
    import slicer

    widget = qt.QWidget()
    layout = qt.QVBoxLayout(widget)
    if show_header:
        header = qt.QLabel(
            "<b>%s</b><br>Fill Name/ORCID for non-members; tick <i>Author?</i> for any individual you "
            "want to acknowledge as a co-author. The GitHub column is read-only (filled automatically). "
            "&nbsp;<a href=\"%s\">Documentation</a>" % (title, DOC_URL))
        header.setWordWrap(True)
        header.setOpenExternalLinks(True)
        layout.addWidget(header)

    # The curator (always a member; info filled authoritatively by the App from the onboarding record)
    # is shown READ-ONLY and kept OUT of the editable grid — so it can't be hand-typed, which would slip
    # a wrong-order name past the App's blank-only enrichment.  The grid is for OTHER contributors.
    curatorPerson = next((p for p in data["people"] if p.get("curator")), None)
    editablePeople = [p for p in data["people"] if not p.get("curator")]
    if curatorPerson is not None:
        who = curatorPerson.get("name") or "@" + (curatorPerson.get("github") or "you")
        curatorLabel = qt.QLabel("<b>Lead author (you):</b> %s &nbsp;&mdash; filled in automatically." % who)
        curatorLabel.setWordWrap(True)
        layout.addWidget(curatorLabel)

    peopleNode = to_people_table(data, people=editablePeople)
    peopleView = slicer.qMRMLTableView()
    peopleView.setMRMLScene(slicer.mrmlScene)
    peopleView.setMRMLTableNode(peopleNode)
    peopleView.setFirstColumnLocked(True)  # GitHub is the auto key
    layout.addWidget(peopleView)

    buttonRow = qt.QHBoxLayout()
    addButton = qt.QPushButton("Add offline contributor")
    removeButton = qt.QPushButton("Remove selected row")
    addButton.connect("clicked()", peopleView.insertRow)
    removeButton.connect("clicked()", peopleView.deleteRow)
    buttonRow.addWidget(addButton)
    buttonRow.addWidget(removeButton)
    buttonRow.addStretch()
    layout.addLayout(buttonRow)

    contribNode = None
    if show_contributions:
        contribNode = to_contributions_table(data)
        contribView = slicer.qMRMLTableView()
        contribView.setMRMLScene(slicer.mrmlScene)
        contribView.setMRMLTableNode(contribNode)
        contribView.setEnabled(False)  # auto provenance, read-only
        layout.addWidget(qt.QLabel("Contributions (issue -> contributor, recorded automatically):"))
        layout.addWidget(contribView)

    statusLabel = qt.QLabel()
    layout.addWidget(statusLabel)

    def syncFromTable():
        edited = from_people_table(peopleNode, existing=editablePeople)
        data["people"] = ([curatorPerson] if curatorPerson is not None else []) + edited
        problems = validation_issues(data)
        unresolved = resolve(data)["unresolved"]
        # Only ever surface real PROBLEMS or factual notes — never claim completeness ("ready"),
        # because only the curator knows whether everyone has been added.
        if problems:
            statusLabel.setText("<b style='color:#b00000'>A cited author needs a real name: %s</b>"
                                % "; ".join(problems))
        elif unresolved:
            statusLabel.setText("<span style='color:#a06000'>No name yet for %s &mdash; they will be "
                                "credited by GitHub handle.</span>"
                                % ", ".join("@" + u for u in unresolved))
        else:
            statusLabel.setText("")

    def saveTo(path):
        syncFromTable()
        save(path, data)

    def isReady():
        syncFromTable()
        return not validation_issues(data)

    # Live status: refresh as the curator edits cells (the grid writes edits back to the table node,
    # which fires ModifiedEvent) so the "ready / credited by handle" line is never stale.
    import vtk
    peopleNode.AddObserver(vtk.vtkCommand.ModifiedEvent, lambda caller, event: syncFromTable())

    controller = types.SimpleNamespace(
        widget=widget, data=data, peopleNode=peopleNode, contribNode=contribNode,
        peopleView=peopleView, statusLabel=statusLabel,
        syncFromTable=syncFromTable, saveTo=saveTo, isReady=isReady)
    syncFromTable()
    return controller
