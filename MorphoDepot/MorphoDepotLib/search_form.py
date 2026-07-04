"""MorphoDepotSearchForm (split from MorphoDepot.py)."""
import os
import re
import logging
import qt
import ctk
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from MorphoDepotLib.forms import (FormBaseQuestion, FormRadioQuestion, FormCheckBoxesQuestion,
    FormTextQuestion, FormComboBoxQuestion, FormSpeciesQuestion)
from MorphoDepotLib.accession_form import MorphoDepotAccessionForm


class MorphoDepotSearchForm():
    """Customized interface to specify MorphoDepot searches"""

    questionsToIgnore = ['iDigBioURL', 'species', 'redistributionAcknowledgement', "githubRepoName", "repoType", "otherSubjectDescription"]

    # Use shorter labels for the search form to allow for a narrower UI
    shortLabels = {
        "specimenSource": "Specimen Source:",
        "iDigBioAccessioned": "In iDigBio:",
        "modality": "Modality:",
        "contrastEnhancement": "Contrast Enhanced:",
        "imageContents": "Image Contents:",
        "subjectType": "Subject Type:",
        "biologicalSex": "Sex:",
        "developmentalStage": "Stage:",
        "anatomicalAreas": "Anatomical Areas:",
    }

    # The "Repository" tier filter is OWNER-based, NOT the self-declared accession repoType:
    # Archival = repos owned by the MorphoDepot org (the gated, reviewed home); Personal = everything
    # else (personal accounts, other orgs).  Options are fixed here and matched against each repo's
    # owner in MorphoDepotLogic.search() -- so a personal repo that *claimed* "Archival" in its
    # accession is correctly classified as Personal.
    TIER_OPTIONS = ["Archival", "Personal"]

    def __init__(self, updateCallback=lambda : None):
        self.updateCallback = updateCallback
        self.form = qt.QWidget()
        layout = qt.QVBoxLayout()
        self.form.setLayout(layout)
        self.scrollArea = qt.QScrollArea()
        self.scrollArea.setWidget(self.form)
        self.scrollArea.setWidgetResizable(True)
        self.topWidget = self.scrollArea
        self.searchFormLayout = qt.QFormLayout()
        self.topWidget.setLayout(self.searchFormLayout)
        self.searchBox = ctk.ctkSearchBox()
        self.searchFormLayout.addRow(self.searchBox)
        self.searchBox.textChanged.connect(self.updateCallback)
        self.searchBox.setPlaceholderText("Fetch repository data to search...")

        # Repository tier filter (OWNER-based; see TIER_OPTIONS) -- added separately to control its
        # default.  Default to Archival so the many personal / pre-org repos are hidden unless the
        # user opts into Personal.
        self.tierComboBox = ctk.ctkCheckableComboBox()
        self.searchFormLayout.addRow("Repository:", self.tierComboBox)
        self.tierComboBox.setToolTip(
            "Archival = repositories in the MorphoDepot organization (curated, reviewed). "
            "Personal = personal-account repositories.")
        for option in MorphoDepotSearchForm.TIER_OPTIONS:
            self.tierComboBox.addItem(option)
        model = self.tierComboBox.checkableModel()
        self.tierComboBox.setCheckState(model.index(0, 0), qt.Qt.Checked)  # Default to Archival
        self.tierComboBox.checkedIndexesChanged.connect(self.updateCallback)

        self.comboBoxesByQuestion = {}
        questions = MorphoDepotAccessionForm.formQuestions
        for question, questionData in questions.items():
            if question not in MorphoDepotSearchForm.questionsToIgnore:
                label = MorphoDepotSearchForm.shortLabels.get(question, question)
                comboBox = ctk.ctkCheckableComboBox()
                self.searchFormLayout.addRow(label, comboBox)
                for option in questionData[1]:
                    comboBox.addItem(option)
                model = comboBox.checkableModel()
                if question == "subjectType":
                    # Default to "Biological specimen" only
                    comboBox.setCheckState(model.index(0, 0), qt.Qt.Checked)
                else:
                    for row in range(model.rowCount()):
                        index = model.index(row,0)
                        comboBox.setCheckState(index, qt.Qt.Checked)
                comboBox.checkedIndexesChanged.connect(self.updateCallback)
                self.comboBoxesByQuestion[question] = comboBox

    def criteria(self):
        criteria = {"freeText": self.searchBox.text}

        # Repository tier (owner-based) -- matched against each repo's owner in MorphoDepotLogic.search().
        criteria["tier"] = []
        model = self.tierComboBox.checkableModel()
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            if self.tierComboBox.checkState(index) == qt.Qt.Checked:
                criteria["tier"].append(MorphoDepotSearchForm.TIER_OPTIONS[row])

        questions = MorphoDepotAccessionForm.formQuestions
        for question, questionData in questions.items():
            if question not in MorphoDepotSearchForm.questionsToIgnore:
                comboBox = self.comboBoxesByQuestion[question]
                model = comboBox.checkableModel()
                criteria[question] = []
                for row in range(model.rowCount()):
                    index = model.index(row,0)
                    if comboBox.checkState(index) == qt.Qt.Checked:
                        criteria[question].append(questionData[1][row])
        return criteria




#
# MorphoDepotLogic
#
