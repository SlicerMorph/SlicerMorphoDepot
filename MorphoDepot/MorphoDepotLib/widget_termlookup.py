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

# The one expression this dialog evaluates in the page. qSlicerWebWidget also calls evalJS itself (it
# sets document.webkitHidden whenever the widget is shown or hidden) and emits evalResult for those
# too, so results are only handled when they answer this exact expression.
_EXPORT_JS = ("JSON.stringify(typeof window.TermLookupExport === 'function'"
              " ? window.TermLookupExport() : {ready: false})")

# Node attributes set on an imported table: the provenance JSON (written next to the CSV at staging and
# release) and the species the terms were matched for (checked against the Accession Form at staging).
PROVENANCE_ATTRIBUTE = "MorphoDepot.terminologyProvenanceJson"
SPECIES_ATTRIBUTE = "MorphoDepot.terminologySpecies"
NON_BIOLOGICAL = "(non-biological)"


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


def _removePythonBridge(webWidget):
    """Remove Slicer's 'slicerPython' object from this widget's web channel.

    qSlicerWebWidget registers it on every page so JavaScript can ask to run Python in Slicer (after an
    "Allow Python execution?" prompt that users can set to never ask again). The term-lookup page never
    uses it, so this window should not offer it. Best effort; the outcome is logged either way.
    """
    try:
        channel = webWidget.webView().page().webChannel()
        registered = channel.registeredObjects()
        proxy = registered.get("slicerPython") if hasattr(registered, "get") else None
        if proxy is None:
            logging.info("Term lookup: no slicerPython object registered on this page.")
            return
        channel.deregisterObject(proxy)
        logging.info("Term lookup: removed the slicerPython bridge from the term-lookup window.")
    except Exception as exc:
        logging.warning(f"Term lookup: could not remove the slicerPython bridge ({exc}).")


def _runEmbeddedDialog(parent, url):
    """Open the embedded browser dialog; return the loaded color node or None."""
    dialog = qt.QDialog(parent or slicer.util.mainWindow())
    dialog.setWindowTitle(_("Build Color Table"))
    dialog.setMinimumWidth(960)
    dialog.setMinimumHeight(720)
    layout = qt.QVBoxLayout(dialog)
    layout.setSpacing(6)

    webWidget = slicer.qSlicerWebWidget()
    _removePythonBridge(webWidget)  # before any page loads
    # Keep the term-lookup page inside Slicer while "View in OLS" and similar
    # links open in the system browser via handleExternalUrlWithDesktopService.
    try:
        webWidget.handleExternalUrlWithDesktopService = True
        webWidget.internalHosts = [qt.QUrl(url).host()]
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
        if js != _EXPORT_JS:
            return  # the widget's own evalJS calls (e.g. document.webkitHidden), not our request
        try:
            data = json.loads(result)
            if not isinstance(data, dict):
                raise ValueError(f"expected an object, got {type(data).__name__}")
        except Exception as exc:
            logging.warning(f"TermLookupExport: unreadable result ({exc}); raw={result!r}")
            slicer.util.warningDisplay(
                _("Could not read the color table from the page. Please try again."),
                windowTitle=_("Build color table"))
            return
        if not data.get("ready"):
            reason = data.get("reason") or _("Run the lookup and fill in the table name.")
            slicer.util.warningDisplay(
                _("The table is not ready yet.") + "\n\n" + reason + "\n\n"
                + _("Then click Use in Slicer again."),
                windowTitle=_("Build color table"))
            return
        csvContent = data.get("csv") or ""
        tableName = (data.get("name") or "terminology_color_table").strip()
        provenanceJson = data.get("provenance")
        builtFor = NON_BIOLOGICAL if data.get("nonBiological") else (data.get("species") or "")

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
                raw = (json.dumps(provenanceJson, indent=2)
                       if not isinstance(provenanceJson, str)
                       else provenanceJson)
                node.SetAttribute(PROVENANCE_ATTRIBUTE, raw)
            except Exception:
                pass
        if builtFor:
            node.SetAttribute(SPECIES_ATTRIBUTE, builtFor)

        resultHolder["node"] = node
        dialog.accept()

    def _onUseClicked():
        webWidget.evalJS(_EXPORT_JS)

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

    webWidget.url = url  # the property is a QString
    dialog.exec_()
    return resultHolder["node"]
