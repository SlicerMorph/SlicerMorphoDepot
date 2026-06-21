"""MorphoDepotAccessionForm (split from MorphoDepot.py)."""
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


class MorphoDepotAccessionForm():
    """Customized interface to collect data about MorphoDepot accessions"""

    sectionTitles = {
        0: "Subject Type",
        1: "Acquisition type",
        2: "Accessioned specimen",
        3: "Species information",
        4: "Image data description",
        "4a": "Subject Description",
        5: "Partial specimen",
        6: "Licensing",
        7: "Github"
    }

    formQuestions = {
        # each question is a tuple of question, answer options, and tooltip
        # This info is pure data, but is closely coupled to the GUI and validation code below for usability

        # section 4a
        "otherSubjectDescription" : (
            "Please describe the subject of the data.",
            "",
            "Provide a description for this non-biological subject."
        ),
        # section 1
        "subjectType" : (
            "What is the subject type?",
            ["Biological specimen", "Other"],
            "Select the type of subject for this data."
        ),



        "specimenSource" : (
            "Is your data from a commercially acquired organism or from an accessioned specimen (i.e., from a natural history collection)?",
           ["Non-accessioned", "Accessioned specimen"],
           ""
        ),

        # section 2
        "iDigBioAccessioned" : (
            "Is your specimen's species in the iDigBio database?",
            ["Yes", "No"],
            ""
        ),
        "iDigBioURL" : (
            "Enter URL from iDigBio:",
            "",
            "Go to iDigBio portal, search for the specimen, click the link and paste the URL below (it should look something like this: https://www.idigbio.org/portal/records/b328320d-268e-4bfc-ae70-1c00f0891f89)"
        ),

        # section 3
        "species" : (
            "What is your specimen's species?",
            "",
            "Enter a valid genus and species for your specimen and use the 'Check species' button to confirm.  If unsure, use the GBIF web page to search"
        ),
        "biologicalSex" : (
            "What is your specimen's sex?",
            ["Male", "Female", "Unknown"],
            ""
        ),
        "developmentalStage" : (
            "What is your specimen's developmental stage?",
            ["Prenatal (fetus, embryo)", "Juvenile (neonatal to subadult)", "Adult"],
            ""
        ),

        # section 4
        "modality" : (
            "What is the modality of the acquisition?",
            ["Micro CT (or synchrotron)", "Medical CT", "MRI", "Lightsheet microscopy", "3D confocal microscopy", "Surface model (photogrammetry, structured light, or laser scanning)"],
            ""
        ),
        "contrastEnhancement" : (
            "Is there contrast enhancement treatment applied to the specimen?",
            ["Yes", "No"],
            ""
        ),
        "imageContents" : (
            "What is in the image?",
            ["Whole specimen", "Partial specimen"],
            ""
        ),

        # section 5
        "anatomicalAreas" : (
            "What anatomical area(s) is/are present in the scan?",
            ["Head and neck (e.g., cranium, mandible, proximal vertebral colum)", "Pectoral girdle", "Forelimb", "Trunk (e.g. body cavity, torso, spine, ribs)", "Pelvic girdle", "Hind limg", "Tail", "Other"],
            ""
        ),

        # section 6
        "redistributionAcknowledgement" : (
            "Acknowledgement:",
            ["I have the right to allow redistribution of this data."],
            ""
        ),
        "license" : (
            "Choose a license:",
            ["CC BY 4.0 (requires attribution, allows commercial usage)", "CC BY-NC 4.0 (requires attribution, non-commercial usage only)"],
            ""
        ),

        # section 7
        "githubRepoName" : (
            "What should the repository in your github account called? This needs to be unique value for your account.",
            "",
            "Name should be fairly short and contain only letters, numbers, and the dash, underscore, or dot characters."
        ),
        "repoType" : (
            "What is the intended lifespan of this repository?",
            ["Archival (intended for long-term maintenance)", "Short-term (e.g. repositories for classroom exercises, that are not meant to be maintained for long-term)"],
            ""
        ),
    }

    def __init__(self, workflowMode=False, validationCallback=None):
        """based on this form: https://docs.google.com/forms/d/1HbSL2lmslmeAggim4qlxjcyLy6KhQWcNPisrURA2Udo/edit"""
        self.workflowMode = workflowMode
        self.validationCallback = validationCallback
        sectionKeys = [0, 1, 2, 3, 4, "4a", 5, 6, 7]
        self.form = qt.QWidget()
        layout = qt.QVBoxLayout()
        self.form.setLayout(layout)
        if not self.workflowMode:
            self.scrollArea = qt.QScrollArea()
            self.scrollArea.setWidget(self.form)
            self.scrollArea.setWidgetResizable(True)
            self.topWidget = self.scrollArea
        else:
            self.topWidget = self.form
        self.sectionWidgets = {}
        self.sectionSections = {}
        for sectionKey in sectionKeys:
            sectionWidget = qt.QWidget()
            sectionLayout = qt.QVBoxLayout()
            sectionWidget.setLayout(sectionLayout)
            sectionTitle = f"Section {sectionKey}: {MorphoDepotAccessionForm.sectionTitles[sectionKey]}"
            sectionLayout.addWidget(qt.QLabel(sectionTitle))
            sectionSection = qt.QWidget()
            sectionSectionLayout = qt.QVBoxLayout()
            sectionSection.setLayout(sectionSectionLayout)
            self.sectionSections[sectionKey] = sectionSection

            if self.workflowMode:
                bottomRow = qt.QWidget()
                bottomRowLayout = qt.QHBoxLayout()
                bottomRow.setLayout(bottomRowLayout)
                prev = qt.QPushButton("Previous")
                next = qt.QPushButton("Next")
                bottomRowLayout.addWidget(prev)
                bottomRowLayout.addWidget(next)
                sectionLayout.addWidget(bottomRow)
                currentIndex = sectionKeys.index(sectionKey)
                if currentIndex > 0:
                    prev.connect("clicked()", lambda prevIndex=currentIndex-1: self.showSection(sectionKeys[prevIndex]))
                else:
                    prev.enabled = False
                if currentIndex < len(sectionKeys) - 1:
                    next.connect("clicked()", lambda nextIndex=currentIndex+1: self.showSection(sectionKeys[nextIndex]))
                else:
                    next.enabled = False

            self.sectionWidgets[sectionKey] = sectionWidget
            self.form.layout().addWidget(sectionWidget)

        form = MorphoDepotAccessionForm.formQuestions
        self.questions = {}

        # section 0
        layout = self.sectionWidgets[0].layout()
        q,a,t = form["subjectType"]
        self.questions["subjectType"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["subjectType"].questionBox)

        # section 1
        layout = self.sectionWidgets[1].layout()
        q,a,t = form["specimenSource"]
        self.questions["specimenSource"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["specimenSource"].questionBox)

        # section 2
        layout = self.sectionWidgets[2].layout()
        q,a,t = form["iDigBioAccessioned"]
        self.questions["iDigBioAccessioned"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["iDigBioAccessioned"].questionBox)
        self.gotoiDigBioButton = qt.QPushButton("Open iDigBio")
        self.gotoiDigBioButton.connect("clicked()", lambda : qt.QDesktopServices.openUrl(qt.QUrl("https://iDigBio.org")))
        layout.addWidget(self.gotoiDigBioButton)
        q,a,t = form["iDigBioURL"]
        self.questions["iDigBioURL"] = FormTextQuestion(q, self.validateForm)
        self.questions["iDigBioURL"].questionBox.toolTip = t
        layout.addWidget(self.questions["iDigBioURL"].questionBox)

        # section 3
        layout = self.sectionWidgets[3].layout()
        q,a,t = form["species"]
        self.questions["species"] = FormSpeciesQuestion(q, self.validateForm)
        self.questions["species"].questionBox.toolTip = t
        layout.addWidget(self.questions["species"].questionBox)
        self.gotoGBIFButton = qt.QPushButton("Open GBIF")
        self.gotoGBIFButton.connect("clicked()", lambda : qt.QDesktopServices.openUrl(qt.QUrl("https://gbif.org")))
        layout.addWidget(self.gotoGBIFButton)
        q,a,t = form["biologicalSex"]
        self.questions["biologicalSex"] = FormRadioQuestion(q, a,  self.validateForm)
        layout.addWidget(self.questions["biologicalSex"].questionBox)
        q,a,t = form["developmentalStage"]
        self.questions["developmentalStage"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["developmentalStage"].questionBox)

        # section 4
        layout = self.sectionWidgets[4].layout()
        q,a,t = form["modality"]
        self.questions["modality"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["modality"].questionBox)
        q,a,t = form["contrastEnhancement"] # "Is there contrast enhancement treatment applied to the specimen (iodine, phosphotungstenic acid, gadolinium, casting agents, etc)?"
        self.questions["contrastEnhancement"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["contrastEnhancement"].questionBox)
        q,a,t = form["imageContents"]
        self.questions["imageContents"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["imageContents"].questionBox)

        # section 4a
        layout = self.sectionWidgets["4a"].layout()
        q,a,t = form["otherSubjectDescription"]
        self.questions["otherSubjectDescription"] = FormTextQuestion(q, self.validateForm)
        layout.addWidget(self.questions["otherSubjectDescription"].questionBox)

        # section 5
        layout = self.sectionWidgets[5].layout()
        q,a,t = form["anatomicalAreas"]
        self.questions["anatomicalAreas"] = FormCheckBoxesQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["anatomicalAreas"].questionBox)

        # section 6
        layout = self.sectionWidgets[6].layout()
        q,a,t = form["redistributionAcknowledgement"]
        self.questions["redistributionAcknowledgement"] = FormCheckBoxesQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["redistributionAcknowledgement"].questionBox)
        q,a,t = form["license"]
        self.questions["license"] = FormRadioQuestion(q, a, self.validateForm)
        self.questions["license"].optionButtons[a[0]].checked=True
        layout.addWidget(self.questions["license"].questionBox)

        # section 7
        layout = self.sectionWidgets[7].layout()
        # Note: the repository destination (personal account vs. organization) is chosen later,
        # at the Go-live gate in the Create tab — not here. Every repo is first staged privately
        # on the creator's personal account. See MorphoDepotWidget.populateOwnerSelector().
        q,a,t = form["githubRepoName"]
        self.questions["githubRepoName"] = FormTextQuestion(q, self.validateForm)
        self.questions["githubRepoName"].questionBox.toolTip = t
        layout.addWidget(self.questions["githubRepoName"].questionBox)
        # F4: auto-suggest a descriptive, metadata-derived repo name (editable). The status line is
        # filled by the widget's availability check; the link points at the naming guidelines.
        self.userEditedRepoName = False
        self._lastSuggestedName = ""
        self.repoNameStatus = qt.QLabel("")
        self.repoNameStatus.setWordWrap(True)
        self.questions["githubRepoName"].questionLayout.addWidget(self.repoNameStatus)
        namingGuidelines = qt.QLabel(
            '<a href="https://github.com/MorphoDepot/MorphoDepot/wiki/Repository-naming">'
            'MorphoDepot repository-naming guidelines</a>')
        namingGuidelines.setOpenExternalLinks(True)
        self.questions["githubRepoName"].questionLayout.addWidget(namingGuidelines)
        q,a,t = form["repoType"]
        self.questions["repoType"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["repoType"].questionBox)

        # NOTE: the contact email is intentionally NOT collected here.  It belongs to publishing,
        # not accession metadata, so it is gathered in the widget's Go-live section and submitted
        # only when the repo is published (see MorphoDepotWidget.goLiveEmail / _submitContactForm).

        if self.workflowMode:
            self.showSection(0)

        self.validateForm()

    def showSection(self, section):
        if self.workflowMode:
            for sectionWidget in self.sectionWidgets.values():
                sectionWidget.hide()
            self.sectionWidgets[section].show()

    @staticmethod
    def _slug(text):
        """A GitHub-safe slug: lowercase, runs of non-alphanumerics collapsed to single dashes."""
        import re
        return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")

    def suggestedRepoName(self):
        """A descriptive default repo name from the accession metadata: ``genus-species[-modality]
        [-contents]`` (e.g. ``mus-musculus-microct-whole``).  Deliberately longer than bare
        genus-species so two scans of the same species are less likely to collide and the name is
        self-describing.  Falls back to the free-text subject description for non-biological subjects.
        Returns "" when there is nothing to base a name on yet."""
        modalitySlug = {
            "Micro CT (or synchrotron)": "microct",
            "Medical CT": "medicalct",
            "MRI": "mri",
            "Lightsheet microscopy": "lightsheet",
            "3D confocal microscopy": "confocal",
            "Surface model (photogrammetry, structured light, or laser scanning)": "surface",
        }
        contentsSlug = {"Whole specimen": "whole", "Partial specimen": "partial"}
        base = self._slug(self.questions["species"].answer())
        if not base:
            base = self._slug(self.questions["otherSubjectDescription"].answer())
        if not base:
            return ""
        parts = [base]
        modality = modalitySlug.get(self.questions["modality"].answer())
        if modality:
            parts.append(modality)
        contents = contentsSlug.get(self.questions["imageContents"].answer())
        if contents:
            parts.append(contents)
        return "-".join(parts)

    def _applySuggestedRepoName(self):
        """Prefill the GitHub repo-name field with suggestedRepoName(), refreshing it as more
        metadata is entered -- but only until the user types their own value, after which their
        entry is never overwritten.  Robust to Qt signal ordering: any field text we did not place
        there ourselves is treated as the user's and locks the field."""
        question = self.questions.get("githubRepoName")
        if question is None:
            return
        field = question.answerText
        current = field.text
        if current and current != getattr(self, "_lastSuggestedName", ""):
            self.userEditedRepoName = True
        if getattr(self, "userEditedRepoName", False):
            return
        name = self.suggestedRepoName()
        if name and current != name:
            field.blockSignals(True)   # programmatic fill: no validateForm re-entry, not "user-edited"
            field.text = name
            field.blockSignals(False)
            self._lastSuggestedName = name

    def validateForm(self, arguments=None):

        # first, update the visibility of dependent sections
        isBiological = (self.questions["subjectType"].answer() == "Biological specimen")

        self.sectionWidgets[1].setVisible(isBiological)
        self.sectionWidgets[2].setVisible(isBiological)
        self.sectionWidgets[3].setVisible(isBiological)
        self.sectionWidgets["4a"].setVisible(not isBiological)
        # Also hide some questions in section 4 for non-biological
        self.questions["contrastEnhancement"].questionBox.setVisible(isBiological)
        self.questions["imageContents"].questionBox.setVisible(isBiological)

        if isBiological:
            if self.questions["specimenSource"].answer() == "Non-accessioned":
                self.sectionWidgets[2].hide()
            else:
                self.sectionWidgets[2].show()
                if self.questions["iDigBioAccessioned"].answer() == "Yes":
                    self.questions["iDigBioURL"].questionBox.show()
                    self.gotoiDigBioButton.show()
                else:
                    self.questions["iDigBioURL"].questionBox.hide()
                    self.gotoiDigBioButton.hide()

            if self.questions["imageContents"].answer() == "Partial specimen":
                self.sectionWidgets[5].show()
            else:
                self.sectionWidgets[5].hide()
        else: # Not biological
            self.sectionWidgets[2].hide()
            self.sectionWidgets[3].hide()
            self.sectionWidgets[5].hide()

        # then check if required elements have been filled out
        valid = True

        if self.questions["subjectType"].answer() == "":
            valid = False

        if isBiological:
            if self.questions["specimenSource"].answer() == "":
                valid = False
            if self.questions["specimenSource"].answer() == "Accessioned specimen":
                if self.questions["iDigBioAccessioned"].answer() == "Yes":
                    if not self.questions["iDigBioURL"].answer().startswith("https://portal.idigbio.org/portal/records"):
                        valid = False

            # Section 3 is always required for biological
            valid = valid and self.questions["species"].answer() != ""
            valid = valid and (len(self.questions["species"].answer().split()) == 2)
            valid = valid and self.questions["biologicalSex"].answer() != ""
            valid = valid and self.questions["developmentalStage"].answer() != ""

            if self.questions["imageContents"].answer() == "Partial specimen":
                valid = valid and self.questions["anatomicalAreas"].answer() != []
        else: # Not biological
            valid = valid and self.questions["otherSubjectDescription"].answer() != ""

        valid = valid and self.questions["modality"].answer() != ""
        if isBiological:
            valid = valid and self.questions["contrastEnhancement"].answer() != ""
            valid = valid and self.questions["imageContents"].answer() != ""
        valid = valid and self.questions["redistributionAcknowledgement"].answer() != []
        valid = valid and self.questions["license"].answer() != ""
        self._applySuggestedRepoName()   # F4: prefill a metadata-derived name (until the user edits it)
        valid = valid and self.questions["githubRepoName"].answer() != ""
        valid = valid and self.questions["repoType"].answer() != ""
        # Reject "." / ".." as the repo name (review S2): such a name flows into a local
        # os.path.join + shutil.rmtree and would otherwise target the whole working dir / its parent.
        repoNameRegex = r"^(?:([a-zA-Z\d]+(?:-[a-zA-Z\d]+)*)/)?((?!\.\.?$)[\w.-]+)$"
        valid = valid and (re.match(repoNameRegex, self.questions["githubRepoName"].answer()) != None)
        # The contact email is validated separately at Go-live (see _updatePublishEnabled), not here.
        self.validationCallback(valid)

    def accessionData(self):
        data = {}
        for key in MorphoDepotAccessionForm.formQuestions.keys():
            data[key] = (self.questions[key].questionLabel.text, self.questions[key].answer())
        return data

    def setAccessionData(self, data):
        """Pre-fill the questionnaire from a stored accessionData dict (each value is a
        (label, answer) pair, as written to MorphoDepotAccession.json).  Used when resuming a
        staged repo so the curator can review/correct the metadata before publishing."""
        for key, question in self.questions.items():
            if key not in data:
                continue
            value = data[key]
            answer = value[1] if isinstance(value, (list, tuple)) and len(value) > 1 else value
            try:
                question.setAnswer(answer)
            except Exception as e:
                logging.warning(f"Could not pre-fill form field '{key}': {e}")
        self.validateForm()
