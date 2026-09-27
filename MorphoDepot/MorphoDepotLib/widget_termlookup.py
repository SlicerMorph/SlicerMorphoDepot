"""Term-lookup web widget dialog and color-table import helper.

Opens https://morphodepot.github.io/term-lookup/ (or the URL in the
``MorphoDepot/termLookupUrl`` setting) inside a Slicer web widget, then reads
the finished table with evalJS and loads it straight into the scene.

Falls back to the system browser when ``qSlicerWebWidget`` is not present in
the running build, and explains the manual-load path.
"""
import json
import logging
import os
import shutil
import tempfile
import urllib.parse

import qt
import slicer
from slicer.i18n import tr as _


TERM_LOOKUP_URL = "https://morphodepot.github.io/term-lookup/"
_URL_SETTING = "MorphoDepot/termLookupUrl"


def _baseUrl():
    override = slicer.util.settingsValue(_URL_SETTING, "")
    if override:
        return override.rstrip("/") + "/"
    return TERM_LOOKUP_URL


def buildTermLookupUrl(species="", terms=(), tableNameSuggestion="", nonBio=False):
    """Return the full URL to open for the given accession context.

    Parameters
    ----------
    species : str
        Scientific name of the specimen (e.g. "Mus musculus").
    terms : iterable of str
        Segment names to pre-populate the term rows.
    tableNameSuggestion : str
        Suggested file-safe name for the table (``name=`` query parameter).
    nonBio : bool
        When True, ``nonbio=1`` replaces the ``species=`` parameter.
    """
    params = ["embedded=1"]
    if nonBio:
        params.append("nonbio=1")
    elif species:
        params.append("species=" + urllib.parse.quote(species, safe=""))
    termList = [t for t in terms if t]
    if termList:
        params.append("terms=" + urllib.parse.quote("|".join(termList), safe="|"))
    if tableNameSuggestion:
        params.append("name=" + urllib.parse.quote(tableNameSuggestion, safe=""))
    return _baseUrl() + "?" + "&".join(params)


def openTermLookupDialog(parent, species="", terms=(), tableNameSuggestion="", nonBio=False):
    """Open the term-lookup tool and return the loaded color table node, or None.

    Opens the page in the Slicer built-in browser when ``qSlicerWebWidget`` is
    available; falls back to the system browser with a manual-load explanation.

    Parameters match :func:`buildTermLookupUrl`.

    Returns
    -------
    vtkMRMLColorTableNode or None
        The loaded node when the user clicks *Use in Slicer*, or ``None`` when
        the dialog is cancelled, the fallback browser path is used, or loading
        the CSV fails.
    """
    url = buildTermLookupUrl(
        species=species, terms=terms,
        tableNameSuggestion=tableNameSuggestion, nonBio=nonBio)

    if not hasattr(slicer, "qSlicerWebWidget"):
        slicer.util.infoDisplay(
            _("The built-in browser is not available in this Slicer build.\n\n"
              "The term-lookup page will open in your system browser. "
              "When done, download the CSV and load it with the color table "
              "selector's file option."),
            windowTitle=_("Build color table"))
        qt.QDesktopServices.openUrl(qt.QUrl(url))
        return None

    return _runEmbeddedDialog(parent, url)


def _runEmbeddedDialog(parent, url):
    """Open the embedded browser dialog; return the loaded color node or None."""
    dialog = qt.QDialog(parent or slicer.util.mainWindow())
    dialog.setWindowTitle(_("Build Color Table"))
    dialog.setMinimumWidth(960)
    dialog.setMinimumHeight(720)
    layout = qt.QVBoxLayout(dialog)
    layout.setSpacing(6)

    webWidget = slicer.qSlicerWebWidget()
    # Keep the term-lookup page inside Slicer while "View in OLS" and similar
    # links open in the system browser via handleExternalUrlWithDesktopService.
    try:
        webWidget.handleExternalUrlWithDesktopService = True
        webWidget.internalHosts = ["morphodepot.github.io"]
    except Exception:
        pass
    layout.addWidget(webWidget, 1)

    hintLabel = qt.QLabel(
        _("Review the terms, then click <b>Use in Slicer</b> to load the table."))
    hintLabel.setWordWrap(True)
    layout.addWidget(hintLabel)

    useButton = qt.QPushButton(_("Use in Slicer"))
    useButton.enabled = False
    useButton.toolTip = _("Load the finished color table into the Slicer scene.")
    cancelButton = qt.QPushButton(_("Cancel"))
    btnRow = qt.QHBoxLayout()
    btnRow.addStretch(1)
    btnRow.addWidget(useButton)
    btnRow.addWidget(cancelButton)
    layout.addLayout(btnRow)

    cancelButton.clicked.connect(dialog.reject)
    resultHolder = {"node": None}

    def _onEvalResult(js, result):
        try:
            data = json.loads(result)
        except Exception as exc:
            logging.warning(f"TermLookupExport: invalid JSON — {exc}  raw={result!r}")
            slicer.util.warningDisplay(
                _("Could not read the color table from the page. Please try again."),
                windowTitle=_("Build color table"))
            return
        if not data.get("ready"):
            slicer.util.warningDisplay(
                _("The table is not ready yet.\n\n"
                  "Run the lookup and make sure the table name is filled in, "
                  "then click Use in Slicer again."),
                windowTitle=_("Build color table"))
            return
        csvContent = data.get("csv") or ""
        tableName = (data.get("name") or "terminology_color_table").strip()
        provenanceJson = data.get("provenance")

        tmpDir = tempfile.mkdtemp()
        try:
            csvPath = os.path.join(tmpDir, f"{tableName}.csv")
            with open(csvPath, "w", encoding="utf-8") as fh:
                fh.write(csvContent)
            node = slicer.util.loadColorTable(csvPath)
        except Exception as exc:
            logging.warning(f"Could not load color table from term-lookup CSV: {exc}")
            slicer.util.warningDisplay(
                _("Could not load the color table. The CSV may be malformed."),
                windowTitle=_("Build color table"))
            return
        finally:
            shutil.rmtree(tmpDir, ignore_errors=True)

        if not node:
            slicer.util.warningDisplay(
                _("Slicer could not parse the color table."),
                windowTitle=_("Build color table"))
            return

        node.SetName(tableName)
        if provenanceJson is not None:
            try:
                raw = (json.dumps(provenanceJson)
                       if not isinstance(provenanceJson, str)
                       else provenanceJson)
                node.SetAttribute("MorphoDepot.terminologyProvenanceJson", raw)
            except Exception:
                pass

        resultHolder["node"] = node
        dialog.accept()

    def _onUseClicked():
        webWidget.evalJS(
            "JSON.stringify(typeof window.TermLookupExport === 'function'"
            " ? window.TermLookupExport() : {ready: false})")

    # evalResult signal: Slicer 5.x emits evalResult(QString js, QString result).
    # Older builds may omit the first argument; try both signatures.
    try:
        webWidget.connect("evalResult(QString,QString)", _onEvalResult)
    except Exception:
        try:
            webWidget.connect("evalResult(QString)",
                              lambda r: _onEvalResult("", r))
        except Exception as exc:
            logging.warning(f"Could not connect evalResult signal: {exc}")

    useButton.clicked.connect(_onUseClicked)

    try:
        webWidget.connect("loadFinished(bool)",
                          lambda ok: setattr(useButton, "enabled", True))
    except Exception:
        useButton.enabled = True  # can't detect load — enable immediately

    webWidget.url = qt.QUrl(url)
    dialog.exec_()
    return resultHolder["node"]
