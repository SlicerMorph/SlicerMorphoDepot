"""Reusable accession/search form-question widgets (split from MorphoDepot.py)."""
import os
import re
import logging
import qt
import ctk
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate


class FormBaseQuestion():
    def __init__(self, question):
        self.questionBox = qt.QWidget()
        self.questionLayout = qt.QVBoxLayout()
        self.questionBox.setLayout(self.questionLayout)
        self.questionLabel = qt.QLabel(question)
        self.questionLabel.setWordWrap(True)
        self.questionLayout.addWidget(self.questionLabel)

    def answer(self):
        # To be implemented by subclasses
        return None


class FormRadioQuestion(FormBaseQuestion):
    def __init__(self, question, options, validator):
        super().__init__(question)
        self.optionButtons = {}
        for option in options:
            self.optionButtons[option] = qt.QRadioButton(option)
            self.optionButtons[option].connect("clicked()", validator)
            self.questionLayout.addWidget(self.optionButtons[option])

    def answer(self):
        for option,button in self.optionButtons.items():
            if button.checked:
                return option
        return ""

    def setAnswer(self, value):
        for option, button in self.optionButtons.items():
            button.checked = (option == value)


class FormCheckBoxesQuestion(FormBaseQuestion):
    def __init__(self, question, options, validator):
        super().__init__(question)
        self.optionButtons = {}
        for option in options:
            self.optionButtons[option] = qt.QCheckBox(option)
            self.optionButtons[option].connect("clicked()", validator)
            self.questionLayout.addWidget(self.optionButtons[option])

    def answer(self):
        answers = []
        for option,button in self.optionButtons.items():
            if button.checked:
                answers.append(option)
        return answers

    def setAnswer(self, values):
        values = values or []
        for option, button in self.optionButtons.items():
            button.checked = (option in values)


class FormTextQuestion(FormBaseQuestion):
    def __init__(self, question, validator):
        super().__init__(question)
        self.answerText = qt.QLineEdit()
        self.answerText.connect("textChanged(QString)", validator)
        self.questionLayout.addWidget(self.answerText)

    def answer(self):
        return self.answerText.text

    def setAnswer(self, value):
        self.answerText.text = value if value is not None else ""


class FormComboBoxQuestion(FormBaseQuestion):
    """A dropdown question whose options are populated dynamically at runtime.

    Each option carries a display string and an underlying value; answer() returns
    the value of the current selection.  The values are tracked in a parallel Python
    list (rather than Qt item data) so retrieval does not depend on QVariant round-trips.
    Used for the Create tab's repository destination selector (personal account vs.
    organizations)."""
    def __init__(self, question, validator=None):
        super().__init__(question)
        self.comboBox = qt.QComboBox()
        self.optionValues = []
        if validator:
            self.comboBox.connect("currentIndexChanged(int)", lambda _index: validator())
        self.questionLayout.addWidget(self.comboBox)

    def setOptions(self, options):
        """Replace the dropdown contents.

        options: list of (displayText, value) tuples.  The previous selection is
        preserved by value when it is still present after repopulating."""
        previous = self.answer()
        self.comboBox.blockSignals(True)
        self.comboBox.clear()
        self.optionValues = []
        for displayText, value in options:
            self.comboBox.addItem(displayText)
            self.optionValues.append(value)
        if previous and previous in self.optionValues:
            self.comboBox.currentIndex = self.optionValues.index(previous)
        self.comboBox.blockSignals(False)

    def answer(self):
        index = self.comboBox.currentIndex
        if index < 0 or index >= len(self.optionValues):
            return ""
        return self.optionValues[index]


class FormSpeciesQuestion(FormTextQuestion):
    def __init__(self, question, validator):
        super().__init__(question, validator)
        # Browse the GBIF backbone taxonomy and pick a name; clicking a result fills the field
        # and shows its lineage in speciesInfo below.
        self.searchButton = qt.QPushButton("Search taxon in GBIF")
        self.searchButton.setIcon(qt.QIcon(qt.QPixmap(":/Icons/Search.png")))
        self.searchButton.setToolTip("Browse the GBIF backbone taxonomy and pick a species name to fill the field.")
        self.searchButton.connect("clicked()", self.onSearchSpecies)
        self.questionLayout.addWidget(self.searchButton)
        self.speciesInfo = qt.QLabel()
        self.questionLayout.addWidget(self.speciesInfo)
        self.searchDialog = None

    @staticmethod
    def _normalizeGbifResult(result):
        """Flatten a GBIF name match into the flat keys this form displays.

        The search dialog uses name_suggest(), which returns a *flat* dict with
        matchType/rank/canonicalName/kingdom/... at the top level.  GBIF's newer *nested*
        match format ({'usage': {...}, 'classification': [...], 'diagnostics': {...}}) is
        also accepted so the lineage display is robust to pygbif version differences."""
        if not isinstance(result, dict):
            return {}
        # The new nested format always carries one of these keys; the flat format never does.
        isNested = any(key in result for key in ("diagnostics", "usage", "classification"))
        if not isNested:
            return result
        flat = {}
        flat["matchType"] = (result.get("diagnostics") or {}).get("matchType", "NONE")
        usage = result.get("usage") or {}
        flat["canonicalName"] = usage.get("canonicalName") or usage.get("name")
        flat["rank"] = usage.get("rank")
        for entry in result.get("classification") or []:
            rank = (entry.get("rank") or "").lower()
            if rank:
                flat[rank] = entry.get("name")  # kingdom, phylum, class, order, family, genus, species
        # GBIF's classification normally includes the species entry, but if a response omits it,
        # fall back to the matched usage name so the species row is never left blank.
        if not flat.get("species") and (flat.get("rank") or "").upper() == "SPECIES":
            flat["species"] = flat["canonicalName"]
        return flat

    def _setSpeciesInfoLabel(self, result):
        result = self._normalizeGbifResult(result)
        requiredKeys = ['matchType', 'rank', 'canonicalName', 'kingdom', 'phylum', 'class', 'order', 'family', 'genus', 'species']
        for key in requiredKeys:
            if key not in result or result[key] is None:
                result[key] = "missing"
        if result['matchType'] == "NONE":
            labelText = "No match"
        elif result['rank'] != "SPECIES":
            labelText = f"Not a species ({result['canonicalName']} is rank {result['rank']})"
        else:
            labelText = f"Kingdom: {result['kingdom']}, Phylum: {result['phylum']}, Class: {result['class']},\nOrder: {result['order']}, Family: {result['family']}, Genus: {result['genus']}, Species: {result['species']}"
        self.speciesInfo.text = labelText


    def onSearchSpecies(self):
        if self.searchDialog is None:
            self.searchDialog = qt.QDialog()
            self.searchDialog.setWindowTitle("Search for species")
            self.searchDialogLayout = qt.QVBoxLayout()
            self.searchDialog.setLayout(self.searchDialogLayout)
            self.searchEntry = qt.QLineEdit()
            self.searchEntry.connect("textChanged(QString)", self.onSearchTextChanged)
            self.searchDialogLayout.addWidget(self.searchEntry)
            self.searchResults = qt.QListWidget()
            self.searchResults.connect("itemClicked(QListWidgetItem*)", self.onSearchResultClicked)
            self.searchDialogLayout.addWidget(self.searchResults)
            self.searchDialog.setModal(True)
            mainWindow = slicer.util.mainWindow()
            self.searchDialog.move(mainWindow.geometry.center() - self.searchDialog.rect.center())
        self.searchEntry.text = self.answerText.text
        self.searchDialog.show()

    def onSearchTextChanged(self, text):
        import pygbif
        self.searchResults.clear()
        if len(text) < 3:
            return
        try:
            results = pygbif.species.name_suggest(q=text, rank="species")
        except Exception as e:
            slicer.util.errorDisplay(f"Error searching for species: {e}")
            return
        for result in results:
            if result['rank'] == "SPECIES":
                item = qt.QListWidgetItem(f"{result['canonicalName']} ({result['kingdom']})")
                item.setData(qt.Qt.UserRole, result)
                self.searchResults.addItem(item)

    def onSearchResultClicked(self, item):
        result = item.data(qt.Qt.UserRole)
        self.answerText.text = result['canonicalName']
        self.searchDialog.hide()
        self._setSpeciesInfoLabel(result)

    def answer(self):
        return self.answerText.text
