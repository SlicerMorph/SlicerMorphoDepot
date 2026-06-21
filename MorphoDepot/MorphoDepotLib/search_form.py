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

        # Add repoType filter separately to control default
        self.repoTypeComboBox = ctk.ctkCheckableComboBox()
        self.searchFormLayout.addRow("Repository Type:", self.repoTypeComboBox)
        repoTypeQuestionData = MorphoDepotAccessionForm.formQuestions["repoType"]
        for option in repoTypeQuestionData[1]:
            self.repoTypeComboBox.addItem(option)
        model = self.repoTypeComboBox.checkableModel()
        self.repoTypeComboBox.setCheckState(model.index(0, 0), qt.Qt.Checked) # Default to Archival
        self.repoTypeComboBox.checkedIndexesChanged.connect(self.updateCallback)

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

        # Handle repoType separately
        repoTypeQuestionData = MorphoDepotAccessionForm.formQuestions["repoType"]
        criteria["repoType"] = []
        model = self.repoTypeComboBox.checkableModel()
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            if self.repoTypeComboBox.checkState(index) == qt.Qt.Checked:
                criteria["repoType"].append(repoTypeQuestionData[1][row])

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
