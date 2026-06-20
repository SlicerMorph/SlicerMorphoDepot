from contextlib import contextmanager
from typing import Annotated, Optional
import csv
import datetime
import fnmatch
import git
import glob
import json
import locale
import logging
import math
import os
import platform
import random
import re
import requests
import shutil
import subprocess
import sys
import time
import traceback

import ctk
import qt
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import (
    parameterNodeWrapper,
    WithinRange,
)

#
# MorphoDepot
#

from MorphoDepotLib.forms import (FormBaseQuestion, FormRadioQuestion, FormCheckBoxesQuestion,
    FormTextQuestion, FormComboBoxQuestion, FormSpeciesQuestion)
from MorphoDepotLib.accession_form import MorphoDepotAccessionForm
from MorphoDepotLib.search_form import MorphoDepotSearchForm
from MorphoDepotLib.screenshot_dialog import ScreenshotReviewDialog
from MorphoDepotLib.logic_deps import DepsMixin
from MorphoDepotLib.logic_github import GitHubMixin
from MorphoDepotLib.logic_controlplane import ControlPlaneMixin
from MorphoDepotLib.logic_objectstore import ObjectStoreMixin
from MorphoDepotLib.logic_repoclerk import RepoClerkMixin
from MorphoDepotLib.logic_repo import RepoMixin
from MorphoDepotLib.logic_contribute import ContributeMixin
from MorphoDepotLib.logic_release import ReleaseMixin
from MorphoDepotLib.logic_accession import AccessionMixin
from MorphoDepotLib.logic_search import SearchMixin


class MorphoDepot(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("MorphoDepot")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "SlicerMorph")]
        self.parent.dependencies = []
        self.parent.contributors = ["Steve Pieper (Isomics, Inc.)"]
        self.parent.helpText = _("""
This module is the client side of the MorphoDepot collaborative segmentation tool.
""")
        self.parent.acknowledgementText = _("""
This was developed as part of the MorhpoCloud project funded by the NSF
Advances in Biological Informatics (1759883) and NSF/DBI Cyberinfrastructure (2301405) grants.
This file was originally developed by Jean-Christophe Fillion-Robin, Kitware Inc., Andras Lasso, PerkLab,
and Steve Pieper, Isomics, Inc. and was partially funded by NIH grant 3P41RR013218-12S1.
""")


#
# MorphoDepotWidget
#


class EnableModuleMixin:
    """A superclass to check that everything is correct before enabling the module.  """

    def __init__(self):
        pass

    def offerPythonInstallation(self):
        msg = "Extra python packages (idigbio and pygbif) are required."
        msg += "\nClick OK to install them for MorphoDepot."
        install = slicer.util.confirmOkCancelDisplay(msg)
        if install:
            logic = MorphoDepotLogic(progressMethod=MorphoDepotWidget.progressMethod)
            logic.installPythonDependencies()
            msg = "Python package installation complete"
            slicer.util.messageBox(msg)
        return logic.checkPythonDependencies()

    def checkModuleEnabled(self):
        """Module is only enabled if all of the dependencies are available,
        possibly after the user has accepted installation and it worked as expected
        """
        # check Slicer version
        if not self.logic.slicerVersionCheck():
            msg = "This version of Slicer is not supported. Use a newer Preview or a Release after 5.8."
            slicer.util.messageBox(msg)
            return False

        # check Python dependecies
        if not self.logic.checkPythonDependencies():
            if not self.offerPythonInstallation():
                return False

        # check git dependencies
        if not self.logic.checkGitDependencies():
            msgBox = qt.QMessageBox()
            msgBox.setWindowTitle("MorphoDepot Dependencies")
            msgBox.setText("The git and gh command line tools must be installed and configured.")
            informativeText = "Be sure that you have logged into Github with 'gh auth login' and then restart Slicer.\n"
            informativeText += "Click 'Open Documentation' for platform-specific instructions."
            msgBox.setInformativeText(informativeText)
            msgBox.setIcon(qt.QMessageBox.Warning)
            openDocsButton = msgBox.addButton("Open Documentation", qt.QMessageBox.ActionRole)
            msgBox.addButton(qt.QMessageBox.Ok)
            msgBox.exec_()
            if msgBox.clickedButton() == openDocsButton:
                qt.QDesktopServices.openUrl(qt.QUrl("https://github.com/MorphoCloud/SlicerMorphoDepot?tab=readme-ov-file#prerequisites-for-morphodepot"))
            return False

        # check local directory
        repoDirectory = self.logic.localRepositoryDirectory()
        if not os.path.exists(repoDirectory):
            msgBox = qt.QMessageBox()
            msgBox.setWindowTitle("MorphoDepot Repository Directory")
            msgBox.setText("The local directory must exist and be writable.")
            informativeText = f"Could not create or access the directory:\n{repoDirectory}\n\nGo into the configure tab and set a valid local repository directory."
            msgBox.setInformativeText(informativeText)
            msgBox.setIcon(qt.QMessageBox.Warning)
            msgBox.exec_()
            return False

        return True


class MorphoDepotWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, EnableModuleMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)  # needed for parameter node observation
        self.logic = None
        self.issuesByItem = {}
        self.prsByItem = {}
        self.segmentNamesByID = {}
        self.hidePRDrafts = True
        self.searchResultsByItem = {}
        self.testingMode = False
        self.screenshots = [] # list of dicts with 'path' and 'caption'

        # development config:
        ## adminUI still placeholder; releaseUI re-enabled for release-management work (see #119)
        self.includeReleaseUI = True
        self.includeAdminUI = False

    def progressMethod(self, message=None):
        message = message if message else self
        logging.info(message)
        slicer.util.showStatusMessage(message)
        slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)

    def setupLogic(self):
        self.logic = MorphoDepotLogic(progressMethod=self.progressMethod)

    def setup(self) -> None:
        """Called when the user opens the module the first time and the widget is initialized."""
        ScriptedLoadableModuleWidget.setup(self)

        # Load widget from .ui file (created by Qt Designer).
        # Additional widgets can be instantiated manually and added to self.layout.
        self.tabWidget = qt.QTabWidget()
        self.layout.addWidget(self.tabWidget)

        uiWidget = slicer.util.loadUI(os.path.normpath(self.resourcePath("UI/MorphoDepotConfigure.ui")))
        uiWidget.setMRMLScene(slicer.mrmlScene)
        self.configureTabIndex = self.tabWidget.addTab(uiWidget, "Configure")
        self.configureUI = slicer.util.childWidgetVariables(uiWidget)

        uiWidget = slicer.util.loadUI(os.path.normpath(self.resourcePath("UI/MorphoDepotSearch.ui")))
        uiWidget.setMRMLScene(slicer.mrmlScene)
        self.tabWidget.addTab(uiWidget, "Search")
        self.searchUI = slicer.util.childWidgetVariables(uiWidget)

        uiWidget = slicer.util.loadUI(os.path.normpath(self.resourcePath("UI/MorphoDepotAnnotate.ui")))
        uiWidget.setMRMLScene(slicer.mrmlScene)
        self.tabWidget.addTab(uiWidget, "Annotate")
        self.annotateUI = slicer.util.childWidgetVariables(uiWidget)

        uiWidget = slicer.util.loadUI(os.path.normpath(self.resourcePath("UI/MorphoDepotReview.ui")))
        uiWidget.setMRMLScene(slicer.mrmlScene)
        self.tabWidget.addTab(uiWidget, "Review")
        self.reviewUI = slicer.util.childWidgetVariables(uiWidget)

        uiWidget = slicer.util.loadUI(os.path.normpath(self.resourcePath("UI/MorphoDepotCreate.ui")))
        uiWidget.setMRMLScene(slicer.mrmlScene)
        self.createTabIndex = self.tabWidget.addTab(uiWidget, "Create")
        self.createUI = slicer.util.childWidgetVariables(uiWidget)

        uiWidget = slicer.util.loadUI(os.path.normpath(self.resourcePath("UI/MorphoDepotRelease.ui")))
        uiWidget.setMRMLScene(slicer.mrmlScene)
        if self.includeReleaseUI:
            self.tabWidget.addTab(uiWidget, "Release")
        self.releaseUI = slicer.util.childWidgetVariables(uiWidget)

        self.adminTab = qt.QScrollArea()
        if self.includeAdminUI:
            self.tabWidget.addTab(self.adminTab, "Admin")
        self.adminTabIndex = self.tabWidget.indexOf(self.adminTab)
        self.adminUI = {} # for future use

        # restore last tab index
        tabIndex = slicer.util.settingsValue("MorphoDepot/tabIndex", 0, converter=int)
        self.tabWidget.currentIndex = tabIndex

        # Set scene in MRML widgets. Make sure that in Qt designer the top-level qMRMLWidget's
        # "mrmlSceneChanged(vtkMRMLScene*)" signal in is connected to each MRML widget's.
        # "setMRMLScene(vtkMRMLScene*)" slot.
        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.setupLogic()

        # Configure
        # only allow picking directories (bitwise AND NOT file filter bit)
        self.configureUI.repoDirectory.filters = self.configureUI.repoDirectory.filters & ~self.configureUI.repoDirectory.Files
        repoDir = os.path.normpath(self.logic.localRepositoryDirectory())
        self.configureUI.repoDirectory.currentPath = repoDir
        self.configureUI.repoDirectory.toolTip = "Be sure to use a real local directory, not an iCloud or OneDrive online location"
        self.configureUI.gitPath.currentPath = os.path.normpath(self.logic.gitExecutablePath) if self.logic.gitExecutablePath else ""
        self.configureUI.gitPath.toolTip = "Restart Slicer after setting new path"
        self.configureUI.ghPath.currentPath = os.path.normpath(self.logic.ghExecutablePath) if self.logic.ghExecutablePath else ""
        self.configureUI.ghPath.toolTip = "Restart Slicer after setting new path"
        self.annotateUI.forkManagementCollapsibleButton.enabled = False

        # Add reload button
        self.configureUI.reloadButton = qt.QPushButton("Apply Changes")
        self.configureUI.reloadButton.toolTip = "Reload the MorphoDepot module to apply changes."
        self.configureUI.configureCollapsibleButton.layout().addWidget(self.configureUI.reloadButton)


        # Add git user name and email fields
        self.configureUI.gitConfigLayout = qt.QFormLayout()
        self.configureUI.userNameLineEdit = qt.QLineEdit()
        self.configureUI.userNameStatusLabel = qt.QLabel("Required for commits")
        self.configureUI.userEmailLineEdit = qt.QLineEdit()
        self.configureUI.userEmailStatusLabel = qt.QLabel("Required for commits")

        self.configureUI.gitConfigLayout.addRow("User Name:", self.configureUI.userNameLineEdit)
        self.configureUI.gitConfigLayout.addRow("", self.configureUI.userNameStatusLabel)
        self.configureUI.gitConfigLayout.addRow("User Email:", self.configureUI.userEmailLineEdit)
        self.configureUI.gitConfigLayout.addRow("", self.configureUI.userEmailStatusLabel)

        # Assuming configureCollapsibleButton has a QVBoxLayout from the .ui file
        # We insert the form layout before other widgets like the admin checkbox for better organization
        if self.configureUI.configureCollapsibleButton.layout():
            self.configureUI.configureCollapsibleButton.layout().insertLayout(0, self.configureUI.gitConfigLayout)

        self.updateGitConfigInfo()

        self.configureUI.adminModeCheckBox = qt.QCheckBox("Administrator mode")
        adminMode = slicer.util.settingsValue("MorphoDepot/adminMode", False, converter=slicer.util.toBool)
        self.configureUI.adminModeCheckBox.checked = adminMode
        if self.includeAdminUI:
            self.configureUI.configureCollapsibleButton.layout().addWidget(self.configureUI.adminModeCheckBox)

        # Testing
        self.configureUI.testingCollapsibleButton = ctk.ctkCollapsibleButton()
        self.configureUI.testingCollapsibleButton.text = "Testing"
        self.configureUI.testingCollapsibleButton.collapsed = True
        self.configureUI.configureCollapsibleButton.layout().addWidget(self.configureUI.testingCollapsibleButton)
        self.configureUI.testingCollapsibleButton.visible = slicer.util.settingsValue("Developer/DeveloperMode", False, converter=slicer.util.toBool)

        testingLayout = qt.QFormLayout(self.configureUI.testingCollapsibleButton)

        self.configureUI.creatorUser = qt.QLineEdit()
        self.configureUI.creatorUser.text = slicer.util.settingsValue("MorphoDepot/testingCreatorUser", "")
        self.configureUI.creatorUser.toolTip = "GitHub user account for creating repositories in tests. Must be logged in via 'gh auth login'."
        testingLayout.addRow("Creator:", self.configureUI.creatorUser)

        self.configureUI.annotatorUser = qt.QLineEdit()
        self.configureUI.annotatorUser.text = slicer.util.settingsValue("MorphoDepot/testingAnnotatorUser", "")
        self.configureUI.annotatorUser.toolTip = "GitHub user account for annotating in tests. Must be logged in via 'gh auth login'."
        testingLayout.addRow("Annotator:", self.configureUI.annotatorUser)

        # Connections for testing widgets
        self.configureUI.creatorUser.editingFinished.connect(
            lambda: qt.QSettings().setValue("MorphoDepot/testingCreatorUser", self.configureUI.creatorUser.text)
        )
        self.configureUI.annotatorUser.editingFinished.connect(
            lambda: qt.QSettings().setValue("MorphoDepot/testingAnnotatorUser", self.configureUI.annotatorUser.text)
        )

        # Create
        self.createUI.inputSelector = slicer.qMRMLNodeComboBox()
        self.createUI.inputSelector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.createUI.inputSelector.setMRMLScene(slicer.mrmlScene)
        self.createUI.inputSelector.showChildNodeTypes = False
        self.createUI.inputSelector.addEnabled = False
        self.createUI.inputSelector.removeEnabled = False
        self.createUI.inputSelector.noneDisplay = "Select a source volume (required)"
        self.createUI.inputSelector.toolTip = "Pick the source volume for the repository."

        self.createUI.colorSelector = slicer.qMRMLColorTableComboBox()
        self.createUI.colorSelector.setMRMLScene(slicer.mrmlScene)
        self.createUI.colorSelector.noneDisplay = "Select a color table (required)"

        self.createUI.segmentationSelector = slicer.qMRMLNodeComboBox()
        self.createUI.segmentationSelector.nodeTypes = ["vtkMRMLSegmentationNode"]
        self.createUI.segmentationSelector.setMRMLScene(slicer.mrmlScene)
        self.createUI.segmentationSelector.noneEnabled = True
        self.createUI.segmentationSelector.noneDisplay = "Select a baseline segmentation (optional)"
        self.createUI.segmentationSelector.toolTip = "Pick an baseline segmentation (optional)."

        formLayout = self.createUI.inputsCollapsibleButton.layout()
        formLayout.addRow("Source volume:", self.createUI.inputSelector)
        formLayout.addRow("Color table:", self.createUI.colorSelector)
        formLayout.addRow("Baseline segmentation:", self.createUI.segmentationSelector)

        self.createUI.accessionLayout = qt.QVBoxLayout()
        self.createUI.accessionCollapsibleButton.setLayout(self.createUI.accessionLayout)
        self.createUI.createRepository.enabled = False
        # "Create" now stages the repo privately on the personal account; going public (and
        # transferring to an org) happens later at the Go-live gate below.
        self.createUI.createRepository.text = "Create (stage privately)"
        self.createUI.accessionForm = MorphoDepotAccessionForm(validationCallback=self._onAccessionFormValidated)
        self.createUI.accessionLayout.addWidget(self.createUI.accessionForm.topWidget)
        # The destination dropdown is filled lazily the first time the Create tab is shown
        # (see populateOwnerSelector / onCurrentTabChanged).
        self.ownerSelectorPopulated = False
        # Auto-assign workflow availability is also resolved lazily on first Create-tab entry.
        self._workflowScopeChecked = False
        self._hasWorkflowScope = False
        self._stagedNameWithOwner = None
        self._stagedReposListPopulated = False
        self._stagedReposByItem = {}
        # Set when the form was pre-filled by resuming an existing staged repo, so Publish
        # applies edits (saveStagedRepoEdits) rather than treating the form as a new repo.
        self._resumedForEdit = False
        self._resumedOriginalAccession = {}
        # The color/baseline nodes auto-loaded on resume (and a content signature of the
        # baseline), so Save/Publish can tell "kept unchanged" from "replaced", and can detect
        # (and refuse) an in-place edit of the loaded segmentation.
        self._resumedColorNode = None
        self._resumedBaselineNode = None
        self._resumedBaselineSignature = ""

        # Add a developer mode button to fill the form with test data
        self.createUI.fillFormForTestingButton = qt.QPushButton("Fill Form for Testing")
        self.createUI.fillFormForTestingButton.toolTip = "Fills the accession form with data for testing. Only visible in developer mode."
        # The button is added to the layout of the accessionCollapsibleButton, before the form itself.
        self.createUI.accessionCollapsibleButton.layout().addWidget(self.createUI.fillFormForTestingButton)
        self.createUI.fillFormForTestingButton.visible = slicer.util.settingsValue("Developer/DeveloperMode", False, converter=slicer.util.toBool)

        # Move the accession form to be after the test button
        self.createUI.accessionCollapsibleButton.layout().addWidget(self.createUI.accessionForm.topWidget)

        # Screenshots for Create tab
        self.createUI.screenshotsCollapsibleButton = ctk.ctkCollapsibleButton()
        self.createUI.screenshotsCollapsibleButton.text = "Screenshots"
        self.createUI.screenshotsCollapsibleButton.collapsed = False
        self.createUI.screenshotsLayout = qt.QFormLayout(self.createUI.screenshotsCollapsibleButton)
        screenshotsButtonsLayout = qt.QHBoxLayout()
        self.createUI.takeScreenshotButton = qt.QPushButton("Take Screenshot")
        self.createUI.reviewScreenshotsButton = qt.QPushButton("Review Screenshots")
        screenshotsButtonsLayout.addWidget(self.createUI.takeScreenshotButton)
        screenshotsButtonsLayout.addWidget(self.createUI.reviewScreenshotsButton)
        self.createUI.screenshotsLayout.addRow(screenshotsButtonsLayout)
        self.createUI.screenshotCountLabel = qt.QLabel("0 screenshots taken")
        self.createUI.screenshotsLayout.addRow(self.createUI.screenshotCountLabel)
        self.createUI.verticalLayout.insertWidget(1, self.createUI.screenshotsCollapsibleButton)

        # === REGION 1: Unpublished staged repositories (top of the Create tab) ===
        # A live list (queried from GitHub by the `morphodepot-staging` topic) of repos staged
        # privately but not yet published — the recovery path after a crash / close / different
        # computer (no client state).  Double-click to load for editing; right-click for actions.
        self.createUI.stagedReposCollapsible = ctk.ctkCollapsibleButton()
        self.createUI.stagedReposCollapsible.text = "Existing yet to be published (staged only) Repos"
        self.createUI.stagedReposCollapsible.collapsed = False
        stagedReposLayout = qt.QVBoxLayout(self.createUI.stagedReposCollapsible)
        stagedReposLayout.setContentsMargins(4, 4, 4, 4)
        stagedReposLayout.setSpacing(4)
        self.createUI.stagedReposList = qt.QListWidget()
        self.createUI.stagedReposList.setToolTip(
            "Repositories you staged privately but have not yet published. "
            "Double-click one to load it for editing, or right-click for more actions. "
            "Read live from GitHub, so it works from any computer.")
        # Keep the list compact: cap its height and stop QListWidget's default Expanding
        # vertical policy from claiming all the spare space in the tab layout.
        self.createUI.stagedReposList.setFixedHeight(72)
        self.createUI.stagedReposList.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.createUI.stagedReposList.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        stagedReposLayout.addWidget(self.createUI.stagedReposList)
        self.createUI.refreshStagedReposButton = qt.QPushButton("Refresh")
        self.createUI.refreshStagedReposButton.toolTip = "Re-query GitHub for unpublished staged repositories."
        stagedReposLayout.addWidget(self.createUI.refreshStagedReposButton)
        self.createUI.verticalLayout.insertWidget(0, self.createUI.stagedReposCollapsible)

        # === REGION 2 boundary: divider + dynamic header for the create/edit form ===
        self.createUI.createSectionDivider = qt.QFrame()
        self.createUI.createSectionDivider.setFrameShape(qt.QFrame.HLine)
        self.createUI.createSectionDivider.setFrameShadow(qt.QFrame.Sunken)
        self.createUI.createSectionDivider.setLineWidth(2)
        self.createUI.createSectionHeader = qt.QLabel("Create a new repository")
        headerFont = self.createUI.createSectionHeader.font
        headerFont.setBold(True)
        headerFont.setPointSize(headerFont.pointSize() + 3)
        self.createUI.createSectionHeader.setFont(headerFont)
        self.createUI.createSectionHeader.setAlignment(qt.Qt.AlignCenter)
        self.createUI.verticalLayout.insertWidget(1, self.createUI.createSectionHeader)
        self.createUI.verticalLayout.insertWidget(1, self.createUI.createSectionDivider)

        # REGION 2 action: "Update Repository (staged)" sits beside the form's "Create (stage
        # privately)" button (shown only when editing a reopened repo).  Both are form actions
        # and belong with the form — NOT in the Go-live section.
        self.createUI.saveEditsButton = qt.QPushButton("Update Repository (staged)")
        self.createUI.saveEditsButton.toolTip = (
            "Save your edits to the staged repository without publishing it. You can keep "
            "editing and updating, then Publish when ready.")
        self.createUI.saveEditsButton.visible = False
        createButtonIndex = self.createUI.verticalLayout.indexOf(self.createUI.createRepository)
        self.createUI.verticalLayout.insertWidget(createButtonIndex + 1, self.createUI.saveEditsButton)

        # On by default (opt-out), for ALL repo types: bake a GitHub Actions workflow into the new
        # repo that auto-assigns each new issue back to its creator.  Requires the gh token's
        # `workflow` scope (checked lazily on Create-tab entry); without it the box is unchecked +
        # disabled with a hint.  The checkbox sits next to a "?" button that opens a fuller
        # explanation, since the label alone doesn't convey what the option actually does.
        self.createUI.autoAssignCheckBox = qt.QCheckBox(
            "Set the GitHub workflow to auto-assign new issues to their creators")
        self.createUI.autoAssignCheckBox.checked = True
        self.createUI.autoAssignCheckBox.enabled = False
        self.createUI.autoAssignCheckBox.toolTip = (
            "Adds a small GitHub Actions workflow so that when someone opens an issue on this "
            "repository, it is automatically assigned to them (no manual step). "
            "Requires your GitHub login to have the 'workflow' scope. "
            "Click the '?' for details.")
        self.createUI.autoAssignHelpButton = qt.QToolButton()
        self.createUI.autoAssignHelpButton.text = "?"
        self.createUI.autoAssignHelpButton.toolTip = "What does this do?"
        self.createUI.autoAssignHelpButton.clicked.connect(self.onAutoAssignHelp)
        autoAssignLayout = qt.QHBoxLayout()
        autoAssignLayout.addWidget(self.createUI.autoAssignCheckBox)
        autoAssignLayout.addWidget(self.createUI.autoAssignHelpButton)
        autoAssignLayout.addStretch(1)
        self.createUI.verticalLayout.insertLayout(
            self.createUI.verticalLayout.indexOf(self.createUI.createRepository),
            autoAssignLayout)

        # === Q0: repository TYPE is the FIRST choice (org-design Sec.1.0/9.7).  It determines where
        # the repo lives (archival -> MorphoDepot org, members-only, DOI; short-term -> personal
        # account, no DOI) and the org-only behaviors below.  This selector sits at the very top of the
        # create form and drives the (now hidden) duplicate repoType question in the accession form. ===
        self.createUI.repoTypeGroup = qt.QGroupBox("Repository type")
        repoTypeGroupLayout = qt.QVBoxLayout(self.createUI.repoTypeGroup)
        self.createUI.archivalRadio = qt.QRadioButton(
            "Archival - maintained and citable; created in the MorphoDepot organization "
            "(members only) and gets a DOI")
        self.createUI.shortTermRadio = qt.QRadioButton(
            "Short-term - disposable / classroom; created on your own account, no DOI")
        repoTypeGroupLayout.addWidget(self.createUI.archivalRadio)
        repoTypeGroupLayout.addWidget(self.createUI.shortTermRadio)
        headerIndex = self.createUI.verticalLayout.indexOf(self.createUI.createSectionHeader)
        self.createUI.verticalLayout.insertWidget(headerIndex + 1, self.createUI.repoTypeGroup)
        self.createUI.archivalRadio.toggled.connect(self._onRepoTypeChanged)
        self.createUI.shortTermRadio.toggled.connect(self._onRepoTypeChanged)
        # Hide the duplicate repoType question buried in the accession form; this selector drives it.
        try:
            self.createUI.accessionForm.questions["repoType"].questionBox.hide()
        except Exception:
            pass

        # === REGION 3: Make Repository Public (shown only once a repo is staged) ===
        # Separated from the form above by a divider + bold centered header (the SAME treatment
        # as the form header), then a bounded box holding ONLY publishing controls: the contact
        # email (required), the publish destination, and Publish / Discard.
        self.createUI.goLiveDivider = qt.QFrame()
        self.createUI.goLiveDivider.setFrameShape(qt.QFrame.HLine)
        self.createUI.goLiveDivider.setFrameShadow(qt.QFrame.Sunken)
        self.createUI.goLiveDivider.setLineWidth(2)
        self.createUI.goLiveHeader = qt.QLabel("Make Repository Public (Publish)")
        goLiveHeaderFont = self.createUI.goLiveHeader.font
        goLiveHeaderFont.setBold(True)
        goLiveHeaderFont.setPointSize(goLiveHeaderFont.pointSize() + 3)
        self.createUI.goLiveHeader.setFont(goLiveHeaderFont)
        self.createUI.goLiveHeader.setAlignment(qt.Qt.AlignCenter)
        self.createUI.goLiveGroup = qt.QGroupBox("")
        goLiveLayout = qt.QVBoxLayout(self.createUI.goLiveGroup)
        self.createUI.stagingStatusLabel = qt.QLabel("")
        self.createUI.stagingStatusLabel.setWordWrap(True)
        goLiveLayout.addWidget(self.createUI.stagingStatusLabel)
        # Contact email — required to publish, submitted to the MorphoDepot contact list at
        # publish time (never for staged-only repos).
        emailTooltip = ("This is the email address we will use to contact you about MorphoDepot "
                        "updates. If you prefer a different one, you can update it. It is added to "
                        "the MorphoDepot contact list, submitted only when you publish, and is not "
                        "stored in the repository.")
        self.createUI.goLiveEmailLabel = qt.QLabel("Contact email (required to publish):")
        self.createUI.goLiveEmailLabel.toolTip = emailTooltip
        goLiveLayout.addWidget(self.createUI.goLiveEmailLabel)
        self.createUI.goLiveEmail = qt.QLineEdit()
        self.createUI.goLiveEmail.toolTip = emailTooltip
        goLiveLayout.addWidget(self.createUI.goLiveEmail)
        # Publish destination (personal account or an organization); hidden unless the user is
        # in at least one organization (populateOwnerSelector decides).
        self.createUI.destinationQuestion = FormComboBoxQuestion("Publish destination:")
        self.createUI.destinationQuestion.questionBox.toolTip = (
            "When you click 'Make Public', the repository is made public under this owner. "
            "Choose your personal account or an organization you belong to.")
        self.createUI.destinationQuestion.questionBox.setVisible(False)
        self.createUI.destinationPersonalLogin = ""
        goLiveLayout.addWidget(self.createUI.destinationQuestion.questionBox)
        goLiveButtonsLayout = qt.QHBoxLayout()
        self.createUI.publishButton = qt.QPushButton("Make Public")
        self.createUI.publishButton.enabled = False
        self.createUI.discardButton = qt.QPushButton("Discard")
        self.createUI.discardButton.enabled = False
        goLiveButtonsLayout.addWidget(self.createUI.publishButton)
        goLiveButtonsLayout.addWidget(self.createUI.discardButton)
        goLiveLayout.addLayout(goLiveButtonsLayout)
        self.createUI.verticalLayout.addWidget(self.createUI.goLiveDivider)
        self.createUI.verticalLayout.addWidget(self.createUI.goLiveHeader)
        self.createUI.verticalLayout.addWidget(self.createUI.goLiveGroup)
        self._setGoLiveZoneVisible(False)

        self.updateScreenshotCount()
        # Annotate
        self.annotateUI.commitButton.enabled = False
        self.annotateUI.reviewButton.enabled = False

        # Review
        self.reviewUI.prCollapsibleButton.enabled = False

        # Release
        self.releaseUI.releasesCollapsibleButton.enabled = False

        # New-release inputs: required baseline segmentation and color table go into the form
        # layout next to the existing source-volume row. Screenshot widgets sit below in the
        # vertical layout, between the form and the release-comments block.
        self.releaseUI.newBaselineSelector = slicer.qMRMLNodeComboBox()
        self.releaseUI.newBaselineSelector.nodeTypes = ["vtkMRMLSegmentationNode"]
        self.releaseUI.newBaselineSelector.setMRMLScene(slicer.mrmlScene)
        self.releaseUI.newBaselineSelector.noneEnabled = True
        self.releaseUI.newBaselineSelector.noneDisplay = "Select a baseline segmentation (required)"
        self.releaseUI.newBaselineSelector.toolTip = "Pick the segmentation to ship as the baseline for this release."
        self.releaseUI.newBaselineSelector.setCurrentNode(None)
        self.releaseUI.newReleaseFormLayout.addRow("New baseline segmentation:", self.releaseUI.newBaselineSelector)

        self.releaseUI.newColorSelector = slicer.qMRMLColorTableComboBox()
        self.releaseUI.newColorSelector.setMRMLScene(slicer.mrmlScene)
        self.releaseUI.newColorSelector.noneDisplay = "Select a color table (required)"
        self.releaseUI.newColorSelector.setCurrentNode(None)
        self.releaseUI.newReleaseFormLayout.addRow("Color table:", self.releaseUI.newColorSelector)

        releaseScreenshotButtonsLayout = qt.QHBoxLayout()
        self.releaseUI.takeScreenshotButton = qt.QPushButton("Take Screenshot")
        self.releaseUI.reviewScreenshotsButton = qt.QPushButton("Review Screenshots")
        self.releaseUI.reviewScreenshotsButton.enabled = False
        releaseScreenshotButtonsLayout.addWidget(self.releaseUI.takeScreenshotButton)
        releaseScreenshotButtonsLayout.addWidget(self.releaseUI.reviewScreenshotsButton)
        self.releaseUI.screenshotCountLabel = qt.QLabel("0 screenshots taken")
        # Insert above the existing release-comments label (newReleaseLayout indices: 0=form,
        # 1=releaseCommentsLabel, 2=releaseCommentsEdit, 3=makeReleaseButton).
        self.releaseUI.newReleaseLayout.insertLayout(1, releaseScreenshotButtonsLayout)
        self.releaseUI.newReleaseLayout.insertWidget(2, self.releaseUI.screenshotCountLabel)

        # Pre-release announcement section (added dynamically; sits between repo list and Releases)
        self.releaseUI.announcementCollapsibleButton = ctk.ctkCollapsibleButton()
        self.releaseUI.announcementCollapsibleButton.text = "Pre-release Announcement"
        self.releaseUI.announcementCollapsibleButton.enabled = False
        self.releaseUI.announcementCollapsibleButton.collapsed = True
        announcementLayout = qt.QFormLayout(self.releaseUI.announcementCollapsibleButton)

        self.releaseUI.announcementCounts = qt.QLabel("Targets: (load a repository)")
        announcementLayout.addRow(self.releaseUI.announcementCounts)

        # Ambient state: shows whether a pre-release announcement already exists for the loaded
        # repo (filled by updateAnnouncementState on load). The collapsible header also reflects
        # it, so it is visible even when this section is collapsed.
        self.releaseUI.announcementStateLabel = qt.QLabel("")
        self.releaseUI.announcementStateLabel.setWordWrap(True)
        announcementLayout.addRow(self.releaseUI.announcementStateLabel)

        self.releaseUI.announcementDeadline = qt.QDateEdit()
        self.releaseUI.announcementDeadline.calendarPopup = True
        self.releaseUI.announcementDeadline.displayFormat = "yyyy-MM-dd"
        self.releaseUI.announcementDeadline.date = qt.QDate.currentDate().addDays(14)
        announcementLayout.addRow("Deadline:", self.releaseUI.announcementDeadline)

        self.releaseUI.announcementMessageEdit = qt.QPlainTextEdit()
        self.releaseUI.announcementMessageEdit.placeholderText = "Message body. {deadline} will be replaced with the date above."
        self.releaseUI.announcementMessageEdit.plainText = (
            "The repository owner is planning to cut a new release on {deadline}.\n"
            "Please complete and submit your work before that date so it can be incorporated.\n"
            "Contributions not submitted by release time will not be included; "
            "after the release you will need to sync to the new baseline before resuming."
        )
        announcementLayout.addRow("Message:", self.releaseUI.announcementMessageEdit)

        self.releaseUI.announceButton = qt.QPushButton("Notify contributors")
        announcementLayout.addRow(self.releaseUI.announceButton)

        # Insert before the existing Releases collapsible (refresh=0, repos=1, [insert here]=2, releases=3)
        self.releaseUI.verticalLayout.insertWidget(2, self.releaseUI.announcementCollapsibleButton)

        # Search
        self.searchUI.searchForm = MorphoDepotSearchForm(updateCallback=self.doSearch)
        self.searchUI.searchCollapsibleButton.layout().addWidget(self.searchUI.searchForm.topWidget)
        self.searchUI.searchForm.topWidget.enabled = False
        self.searchUI.searchCollapsibleButton.collapsed = True
        self.searchUI.resultsTable = qt.QTableView()
        self.searchUI.resultsTable.setContextMenuPolicy(qt.Qt.CustomContextMenu)
        self.searchUI.resultsTable.customContextMenuRequested.connect(self.onSearchResultsContextMenu)
        self.searchUI.resultsModel = qt.QStandardItemModel()
        self.searchUI.resultsTable.setModel(self.searchUI.resultsModel)
        self.searchUI.resultsTable.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.searchUI.resultsTable.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.searchUI.resultsTable.setSortingEnabled(True)
        self.searchUI.refreshButton.text = "Load Searchable Repository Data"
        self.searchUI.resultsCollapsibleButton.layout().addWidget(self.searchUI.resultsTable)

        self.searchUI.resultsButtonsLayout = qt.QHBoxLayout()
        self.searchUI.saveSearchResultsButton = qt.QPushButton("Save...")
        self.searchUI.saveSearchResultsButton.enabled = False
        self.searchUI.resultsButtonsLayout.addStretch(1)
        self.searchUI.resultsButtonsLayout.addWidget(self.searchUI.saveSearchResultsButton)
        self.searchUI.resultsCollapsibleButton.layout().addLayout(self.searchUI.resultsButtonsLayout)


        # Connections
        self.tabWidget.currentChanged.connect(self.onCurrentTabChanged)
        self.configureUI.repoDirectory.comboBox().connect("currentTextChanged(QString)", self.onRepoDirectoryChanged)
        self.configureUI.gitPath.comboBox().connect("currentTextChanged(QString)", self.onGitPathChanged)
        self.configureUI.adminModeCheckBox.stateChanged.connect(self.onAdminModeChanged)
        self.configureUI.userNameLineEdit.textChanged.connect(self.onUserNameChanged)
        self.configureUI.userEmailLineEdit.textChanged.connect(self.onUserEmailChanged)
        self.configureUI.ghPath.comboBox().connect("currentTextChanged(QString)", self.onGhPathChanged)
        self.configureUI.reloadButton.clicked.connect(self.onReload)
        self.createUI.createRepository.clicked.connect(self.onCreateRepository)
        self.createUI.saveEditsButton.clicked.connect(self.onSaveEdits)
        self.createUI.publishButton.clicked.connect(self.onPublish)
        self.createUI.discardButton.clicked.connect(self.onDiscard)
        self.createUI.stagedReposList.itemDoubleClicked.connect(self.onStagedRepoActivated)
        self.createUI.stagedReposList.customContextMenuRequested.connect(self.onStagedReposContextMenu)
        self.createUI.refreshStagedReposButton.clicked.connect(lambda: self.refreshStagedReposList(force=True))
        self.createUI.goLiveEmail.textChanged.connect(self._updatePublishEnabled)
        self.createUI.openRepository.clicked.connect(self.onOpenRepository)
        self.createUI.clearForm.clicked.connect(self.onClearForm)
        self.createUI.fillFormForTestingButton.clicked.connect(self.onFillFormForTesting)
        self.createUI.reviewScreenshotsButton.clicked.connect(self.onReviewScreenshots)
        self.createUI.takeScreenshotButton.clicked.connect(self.onTakeScreenshot)
        self.annotateUI.issueList.itemDoubleClicked.connect(self.onIssueDoubleClicked)
        self.annotateUI.prList.itemSelectionChanged.connect(self.onPRSelectionChanged)
        self.annotateUI.messageTitle.textChanged.connect(self.onCommitMessageChanged)
        self.annotateUI.commitButton.clicked.connect(self.onCommit)
        self.annotateUI.reviewButton.clicked.connect(self.onRequestReview)
        self.annotateUI.refreshButton.connect("clicked(bool)", self.onRefresh)
        self.annotateUI.openPRPageButton.clicked.connect(self.onOpenPRPageButtonClicked)
        self.reviewUI.refreshButton.connect("clicked(bool)", self.onReviewRefresh)
        self.reviewUI.prList.itemDoubleClicked.connect(self.onPRDoubleClicked)
        self.reviewUI.hideDraftsCheckBox.stateChanged.connect(self.onHideDraftsChanged)
        self.reviewUI.requestChangesButton.clicked.connect(self.onRequestChanges)
        self.reviewUI.approveButton.clicked.connect(self.onApprove)
        self.releaseUI.refreshButton.clicked.connect(self.onRefreshReleaseTab)
        self.releaseUI.repoList.itemDoubleClicked.connect(self.onReleaseRepoDoubleClicked)
        self.releaseUI.makeReleaseButton.clicked.connect(self.onMakeRelease)
        self.releaseUI.openReleasePageButton.clicked.connect(self.onOpenReleasePage)
        self.releaseUI.announceButton.clicked.connect(self.onAnnounceUpcomingRelease)
        self.releaseUI.newBaselineSelector.connect("currentNodeChanged(vtkMRMLNode*)", lambda _: self.updateMakeReleaseEnabled())
        self.releaseUI.newColorSelector.connect("currentNodeChanged(vtkMRMLNode*)", lambda _: self.updateMakeReleaseEnabled())
        self.releaseUI.takeScreenshotButton.clicked.connect(self.onTakeScreenshot)
        self.releaseUI.reviewScreenshotsButton.clicked.connect(self.onReviewScreenshots)
        self.searchUI.resultsTable.doubleClicked.connect(self.onSearchResultsDoubleClicked)
        self.searchUI.refreshButton.clicked.connect(self.onRefreshSearch)
        self.searchUI.saveSearchResultsButton.clicked.connect(self.onSaveSearchResults)

        # Add hidden RepoClerk status labels below each refresh button
        for ui, btnName in [
            (self.annotateUI, "refreshButton"),
            (self.reviewUI, "refreshButton"),
            (self.searchUI, "refreshButton"),
        ]:
            btn = getattr(ui, btnName)
            label = qt.QLabel("")
            label.hide()
            layout = btn.parent().layout()
            layout.insertWidget(layout.indexOf(btn) + 1, label)
            ui.repoClerkStatusLabel = label

        # set initial visibility of admin tab
        self.onAdminModeChanged(self.configureUI.adminModeCheckBox.checkState())
        self.reviewUI.hideDraftsCheckBox.checked = self.hidePRDrafts

        self.updateRefreshButtonLabels()

        # The currentChanged signal is connected after the tab index is restored above, so
        # populate the destination dropdown here if the Create tab is the active one at launch.
        if self.tabWidget.currentIndex == self.createTabIndex:
            self.populateOwnerSelector()
            self.refreshStagedReposList()

    def cleanup(self) -> None:
        """Called when the application closes and the module widget is destroyed."""
        self.removeObservers()

    def onReload(self):
        """Reload this module AND its ``MorphoDepotLib`` submodules. Slicer's stock reload
        re-execs only ``MorphoDepot.py``, leaving ``MorphoDepotLib.*`` cached (stale), so edits
        to the split-out mixins/clients would not take effect without reloading them first."""
        import importlib
        for name in [n for n in list(sys.modules)
                     if n == "MorphoDepotLib" or n.startswith("MorphoDepotLib.")]:
            try:
                importlib.reload(sys.modules[name])
            except Exception as e:
                logging.warning(f"Could not reload {name}: {e}")
        ScriptedLoadableModuleWidget.onReload(self)

    def enter(self):
        """Disable everything except the configUI"""
        moduleEnabled = self.checkModuleEnabled()
        for tabIndex in range(self.tabWidget.count):
            if tabIndex != self.configureTabIndex:
                self.tabWidget.setTabEnabled(tabIndex, moduleEnabled)
            else:
                self.tabWidget.setTabEnabled(tabIndex, True)

    def refreshStagedReposList(self, force=False):
        """Populate the 'Unpublished staged repositories' list on the Create tab by querying
        GitHub for repos carrying the `morphodepot-staging` topic (the source of truth).  The
        query is lazy: it runs the first time the Create tab is shown and after stage/publish/
        discard, or whenever `force` is set (the Refresh button)."""
        listWidget = getattr(self.createUI, "stagedReposList", None)
        if listWidget is None:
            return
        if self._stagedReposListPopulated and not force:
            return
        self._stagedReposByItem = {}
        listWidget.clear()
        try:
            repos = self.logic.listStagedRepos()
            self._stagedReposListPopulated = True
        except Exception as e:
            logging.warning(f"Could not list staged repositories: {e}")
            return
        if not repos:
            placeholder = qt.QListWidgetItem("(none — repos you stage privately appear here)")
            placeholder.setFlags(qt.Qt.NoItemFlags)
            listWidget.addItem(placeholder)
            return
        for repo in repos:
            item = qt.QListWidgetItem(repo.get("summary") or repo.get("nameWithOwner", ""))
            listWidget.addItem(item)
            self._stagedReposByItem[item] = repo

    def onStagedReposContextMenu(self, point):
        """Right-click menu on an unpublished staged repo: open it on GitHub, or load it to
        edit (same as double-click)."""
        listWidget = self.createUI.stagedReposList
        item = listWidget.itemAt(point)
        if item is None:
            return
        repo = self._stagedReposByItem.get(item)
        if not repo:
            return
        menu = qt.QMenu(listWidget)
        openAction = menu.addAction("Open the private repo in browser")
        openAction.connect("triggered()", lambda r=repo: self._openStagedRepoInBrowser(r))
        editAction = menu.addAction("Load the repo to edit")
        editAction.connect("triggered()", lambda i=item: self.onStagedRepoActivated(i))
        menu.exec_(listWidget.mapToGlobal(point))

    def _openStagedRepoInBrowser(self, repo):
        nameWithOwner = repo.get("nameWithOwner")
        if nameWithOwner:
            qt.QDesktopServices.openUrl(qt.QUrl(f"https://github.com/{nameWithOwner}"))

    def onStagedRepoActivated(self, item):
        """Resume the repo double-clicked in the unpublished list: clone it, pre-fill the
        questionnaire from its committed metadata so the curator can review/correct it, and
        restore the Publish / Discard gate.  Edits are applied at Publish (saveStagedRepoEdits)."""
        repo = self._stagedReposByItem.get(item)
        if not repo:
            return
        try:
            with slicer.util.tryWithErrorDisplay(_("Trouble resuming staged repository"), waitCursor=True):
                slicer.util.showStatusMessage("Resuming staged repository (cloning)...")
                accession = self.logic.resumeStagedRepo(repo)

                # Pre-fill the form from the repo's accession data and remember it (for the
                # volume-derived fields the form does not carry, e.g. scan dimensions/spacing).
                self._resumedOriginalAccession = accession or {}
                self._resumedForEdit = True
                if accession:
                    self.createUI.accessionForm.setAccessionData(accession)
                # The repo already exists; its name cannot be changed by editing the field.
                self.createUI.accessionForm.questions["githubRepoName"].answerText.readOnly = True

                # Load the submitted data into the scene so Subject Data looks exactly as
                # submitted: source volume (from the v1 release asset, shown read-only), and the
                # color table and baseline segmentation committed in the repo (both editable).
                nameWithOwner = self.logic.stagingContext.get("personalNameWithOwner")
                self._loadResumedSubjectData(self.logic.stagingContext.get("repoDir"), nameWithOwner)
        except Exception:
            slicer.util.showStatusMessage("")
            return
        slicer.util.showStatusMessage("")

        self._enterGoLiveState(repo.get("nameWithOwner"))

    def _loadResumedSubjectData(self, repoDir, nameWithOwner):
        """On resume, load the submitted data into the scene and fill the Subject Data
        selectors: the source volume (downloaded from the private repo's v1 release asset, then
        shown read-only — it cannot be changed), and the color table and baseline segmentation
        committed in the repo (both editable)."""
        self._resumedColorNode = None
        self._resumedBaselineNode = None
        if not repoDir or not os.path.exists(repoDir):
            return

        # Source volume: download it (from its source_volume URL for new repos — the JS2 object
        # store — or the legacy private v1 release asset otherwise) into a cache dir OUTSIDE the
        # git working tree (so it is never committed), load it, and show it read-only.
        sourceVolumeName = ""
        pointerPath = os.path.join(repoDir, "source_volume")
        if os.path.exists(pointerPath):
            try:
                with open(pointerPath) as fp:
                    base = os.path.basename(fp.read().strip())
                sourceVolumeName = base[:-5] if base.endswith(".nrrd") else base
            except Exception:
                pass
        self.createUI.inputSelector.setCurrentNode(None)
        self.createUI.inputSelector.enabled = False
        self.createUI.inputSelector.noneDisplay = (
            f"{sourceVolumeName} (existing source volume — not editable)" if sourceVolumeName
            else "(existing source volume — not editable)")
        if sourceVolumeName and nameWithOwner:
            try:
                cacheDir = os.path.join(self.logic.localRepositoryDirectory(), "MorphoDepotCaches",
                                        "Volumes", nameWithOwner.replace("/", "-"))
                os.makedirs(cacheDir, exist_ok=True)
                nrrdPath = os.path.join(cacheDir, f"{sourceVolumeName}.nrrd")
                if not os.path.exists(nrrdPath):
                    slicer.util.showStatusMessage("Downloading source volume...")
                    volumeRef = open(pointerPath).read().strip()
                    if volumeRef.startswith("http"):
                        # New repos: source_volume is an absolute, public object-store URL.
                        volumeURL = self.logic.resolveVolumeURL(volumeRef, nameWithOwner)
                        slicer.util.downloadFile(volumeURL, nrrdPath)
                    else:
                        # Legacy staged repo: volume is a private v1 release asset (needs gh auth).
                        self.logic.gh(f"release download v1 --repo {nameWithOwner} "
                                      f"--pattern {sourceVolumeName}.nrrd --dir {cacheDir} --clobber")
                volumeNode = slicer.util.loadVolume(nrrdPath)
                self.createUI.inputSelector.setCurrentNode(volumeNode)
            except Exception as e:
                logging.warning(f"Could not load staged source volume: {e}")

        # Color table: load the committed CSV and select it.
        try:
            csvFiles = [f for f in os.listdir(repoDir) if f.endswith(".csv")]
            if csvFiles:
                self._resumedColorNode = slicer.util.loadColorTable(os.path.join(repoDir, csvFiles[0]))
                self.createUI.colorSelector.setCurrentNode(self._resumedColorNode)
        except Exception as e:
            logging.warning(f"Could not load staged color table: {e}")

        # Baseline segmentation: load it if the repo has one.
        baselinePath = os.path.join(repoDir, "baseline.seg.nrrd")
        if os.path.exists(baselinePath):
            try:
                self._resumedBaselineNode = slicer.util.loadSegmentation(baselinePath)
                self.createUI.segmentationSelector.setCurrentNode(self._resumedBaselineNode)
            except Exception as e:
                logging.warning(f"Could not load staged baseline segmentation: {e}")

        # Snapshot the baseline's content signature so a later Save can detect (and refuse) an
        # in-place edit of the loaded segmentation.
        self._resumedBaselineSignature = self._segmentationContentSignature(self._resumedBaselineNode)

        # Screenshots: load the committed set into the screenshot list so Take/Review work on
        # the full set and edits (add/remove/recaption) are saved.  Copy the PNGs to a cache
        # dir OUTSIDE the git tree so their paths survive the repo being rewritten on save.
        self.screenshots = []
        captionsPath = os.path.join(repoDir, "screenshots", "captions.json")
        if os.path.exists(captionsPath):
            try:
                with open(captionsPath) as fp:
                    captions = json.load(fp)
                shotCacheDir = os.path.join(self.logic.localRepositoryDirectory(), "MorphoDepotCaches",
                                            "StagedScreenshots", (nameWithOwner or "").replace("/", "-"))
                if os.path.exists(shotCacheDir):
                    shutil.rmtree(shotCacheDir, ignore_errors=True)
                os.makedirs(shotCacheDir, exist_ok=True)
                for name in sorted(captions.keys()):
                    src = os.path.join(repoDir, "screenshots", name)
                    if os.path.exists(src):
                        dst = os.path.join(shotCacheDir, name)
                        shutil.copy(src, dst)
                        self.screenshots.append({"path": dst, "caption": captions[name]})
            except Exception as e:
                logging.warning(f"Could not load staged screenshots: {e}")
        self.updateScreenshotCount()

    def _baselineMatchesCommittedFile(self, node, committedPath):
        """M6 no-change check (org-design M6), done CHEAPLY: save the candidate baseline to a temp
        COMPRESSED .seg.nrrd and compare its SIZE to the committed baseline.seg.nrrd.  A compressed
        segmentation is small (KB-MB), so this is file I/O on a small file -- the previous approach
        resampled every segment onto the full source-volume voxel grid and SHA-256'd it, which pegged
        every core for minutes and froze the UI.  A single changed voxel changes the compressed size,
        and size is immune to gzip-header timestamp non-determinism.  Returns True only when the sizes
        match exactly (no new work -> not a release); ANY save/read failure returns False so a tooling
        hiccup never blocks a real release.  (A different-content / same-compressed-size collision is
        astronomically unlikely; add a decompressed-voxel tiebreak on a size match if ever needed.)"""
        import tempfile
        if not committedPath or not os.path.exists(committedPath):
            return False
        # Segment count: a reliable signal that adding/removing segments ALWAYS changes -- the
        # compressed size alone can miss it, and Slicer's modified flag can miss copy/merge edits.
        try:
            committedCount = self._committedSegmentCount(committedPath)
            if committedCount is not None and node is not None \
                    and node.GetSegmentation().GetNumberOfSegments() != committedCount:
                return False  # different number of segments -> definitely changed
        except Exception as e:
            logging.warning(f"Baseline segment-count check failed: {e}")
        tmpDir = tempfile.mkdtemp()
        try:
            tmpPath = os.path.join(tmpDir, "candidate.seg.nrrd")
            if not slicer.util.saveNode(node, tmpPath, {'useCompression': True}):
                return False
            return os.path.getsize(tmpPath) == os.path.getsize(committedPath)
        except Exception as e:
            logging.warning(f"Baseline file comparison failed; treating as changed: {e}")
            return False
        finally:
            shutil.rmtree(tmpDir, ignore_errors=True)

    def _committedSegmentCount(self, path):
        """Number of segments in a committed .seg.nrrd, parsed from its (uncompressed) NRRD text
        header (Segment{N}_* custom fields).  Returns None if it cannot be determined."""
        try:
            import re
            # Read until the NRRD header terminator (\n\n), not a fixed slice: a segmentation with many
            # DICOM-derived per-segment tag strings can push the header past a fixed cap, which would
            # undercount segments and let the no-change guard silently pass. Cap at 8 MB as a backstop.
            header = b""
            with open(path, "rb") as f:
                while b"\n\n" not in header and len(header) < 8 * 1024 * 1024:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    header += chunk
            header = header.split(b"\n\n", 1)[0]
            indices = set(re.findall(rb"Segment(\d+)_", header))
            return len(indices) if indices else None
        except Exception as e:
            logging.warning(f"Could not read committed segment count from {path}: {e}")
            return None

    def _colorTableMatchesCommitted(self, colorNode):
        """True if the picked color table is byte-identical to the repository's committed color file
        (so a release would not actually change it -- only the node name differs).  Saves the picked
        node to a temp file of the same extension and compares bytes.  Returns False when there is no
        committed color file or on any error, so it never falsely blocks a real color change."""
        import tempfile
        if colorNode is None or not (self.logic and self.logic.localRepo):
            return False
        repoDir = self.logic.localRepo.working_dir
        committed = glob.glob(f"{repoDir}/*.csv") or glob.glob(f"{repoDir}/*.ctbl")
        if not committed:
            return False
        if len(committed) > 1:
            # Compare against the same file prepareReleaseSnapshot writes (first match); warn so a
            # stray extra color file can't silently make the comparison pick the wrong one.
            logging.warning(f"Multiple committed color files {committed}; comparing against {committed[0]}")
        committedPath = committed[0]
        tmpDir = tempfile.mkdtemp()
        try:
            tmpPath = os.path.join(tmpDir, "candidate" + os.path.splitext(committedPath)[1])
            if not slicer.util.saveNode(colorNode, tmpPath):
                return False
            with open(tmpPath, "rb") as a, open(committedPath, "rb") as b:
                return a.read() == b.read()
        except Exception as e:
            logging.warning(f"Color-table comparison failed; assuming changed: {e}")
            return False
        finally:
            shutil.rmtree(tmpDir, ignore_errors=True)

    def _segmentationContentSignature(self, segNode, referenceVolume=None):
        """A content signature of a segmentation (segment ids + binary-labelmap voxels), used to
        detect whether the loaded baseline was edited in the scene (publish-edit path only; the
        release no-change check now compares compressed file sizes instead).  Falls back to a
        modified-time string if no reference geometry is available.  NOTE: when a referenceVolume is
        present this resamples each segment onto it (can be slow for a large volume) -- the same
        pre-existing cost the release path moved away from; left as-is here since this path is not
        currently exercised, and a file-size approach should be applied here too if it ever is."""
        if segNode is None:
            return ""
        if referenceVolume is None:
            referenceVolume = self.createUI.inputSelector.currentNode()
        try:
            import numpy as np
            import hashlib
            seg = segNode.GetSegmentation()
            h = hashlib.sha256()
            for i in range(seg.GetNumberOfSegments()):
                segId = seg.GetNthSegmentID(i)
                h.update(segId.encode("utf-8"))
                arr = slicer.util.arrayFromSegmentBinaryLabelmap(segNode, segId, referenceVolume)
                if arr is not None:
                    h.update(str(arr.shape).encode("utf-8"))
                    h.update(np.ascontiguousarray(arr).tobytes())
            return h.hexdigest()
        except Exception as e:
            logging.warning(f"Could not compute segmentation content signature: {e}")
            try:
                return f"mtime:{segNode.GetSegmentation().GetMTime()}"
            except Exception:
                return f"mtime:{segNode.GetMTime()}"

    def _baselineWasEditedInScene(self, node):
        """True if the loaded baseline was edited in the scene since it was read from disk.

        Two-stage check.  Stage 1 (fast gate): Slicer's storable-node GetModifiedSinceRead()
        — the exact flag the Save Data dialog uses; vtkMRMLSegmentationNode bumps its
        StorableModifiedTime on segment edits.  If it is false there is no storable change at
        all, so the baseline is unedited.  Stage 2 (confirm): when the flag is true it may also
        have been tripped by generating a closed-surface representation for the 3D view (also a
        RepresentationModified event), so we confirm against a content signature of the source
        binary labelmap — which a display-only representation change does NOT alter — to avoid
        a false 'modified' on a repo the curator merely viewed."""
        try:
            if not node.GetModifiedSinceRead():
                return False
        except Exception as e:
            logging.warning(f"GetModifiedSinceRead unavailable, using content hash only: {e}")
        return self._segmentationContentSignature(node) != self._resumedBaselineSignature

    def _gatherStagedEdits(self):
        """Assemble the edits to persist: the questionnaire (always), plus a REPLACEMENT color
        table or baseline segmentation only when a *different* node has been selected.  The
        accession form is the only thing meant to be edited on the reopen flow — the color
        table and baseline are replaced by providing a finished file, not tweaked in place.
        scanDimensions/scanSpacing carry over from the committed file (volume is not editable).

        Returns (editedData, newColorTable, newSegmentation), or None to ABORT because the
        loaded baseline was modified in the scene (the user is warned to reload the original
        instead of editing here)."""
        # Compare nodes by ID, not Python identity: Slicer's qMRMLNodeComboBox.currentNode()
        # returns a fresh PythonQt wrapper each call, so `is` is unreliable.
        loadedBaselineID = self._resumedBaselineNode.GetID() if self._resumedBaselineNode else None
        currentBaseline = self.createUI.segmentationSelector.currentNode()
        newSegmentation = None
        if currentBaseline is not None and currentBaseline.GetID() == loadedBaselineID:
            # Same node we loaded: allowed only if untouched — editing here is not supported.
            if self._baselineWasEditedInScene(currentBaseline):
                slicer.util.warningDisplay(
                    "The baseline segmentation appears to have been modified in this scene.\n\n"
                    "Editing the segmentation here is not supported.  Reopen the repository to "
                    "reload the original from disk, or prepare the finished segmentation "
                    "separately and load it as a new node, then try again.",
                    windowTitle="Segmentation modified")
                return None
        elif currentBaseline is not None:
            # A different, deliberately-provided node: use it as the replacement.
            newSegmentation = currentBaseline

        editedData = self.createUI.accessionForm.accessionData()
        for preservedKey in ("scanDimensions", "scanSpacing"):
            if preservedKey in self._resumedOriginalAccession:
                editedData[preservedKey] = self._resumedOriginalAccession[preservedKey]

        loadedColorID = self._resumedColorNode.GetID() if self._resumedColorNode else None
        currentColor = self.createUI.colorSelector.currentNode()
        newColorTable = currentColor if (currentColor is not None and currentColor.GetID() != loadedColorID) else None
        return editedData, newColorTable, newSegmentation

    def _rebaselineResumedNodes(self):
        """After a save, treat the now-current color/baseline as the new baseline so a
        subsequent save with no further change is correctly a no-op."""
        self._resumedColorNode = self.createUI.colorSelector.currentNode()
        self._resumedBaselineNode = self.createUI.segmentationSelector.currentNode()
        self._resumedBaselineSignature = self._segmentationContentSignature(self._resumedBaselineNode)

    def _exitResumeMode(self):
        """Undo the resume-mode UI state so the Subject Data section works normally again for
        creating a new repository."""
        self._resumedForEdit = False
        self._resumedColorNode = None
        self._resumedBaselineNode = None
        self._resumedBaselineSignature = ""
        self.createUI.inputSelector.enabled = True
        self.createUI.inputSelector.noneDisplay = "Select a source volume (required)"
        self.createUI.accessionForm.questions["githubRepoName"].answerText.readOnly = False
        self._updateCreateSectionHeader()

    def _updateCreateSectionHeader(self):
        """The section header below the staged-repos list reflects what the form is doing:
        creating a new repository, or editing a reopened staged one."""
        editing = bool(self._resumedForEdit and self._stagedNameWithOwner)
        if editing:
            self.createUI.createSectionHeader.text = f"Editing staged repository: {self._stagedNameWithOwner}"
        else:
            self.createUI.createSectionHeader.text = "Create a new repository"
        # The repository TYPE is the irreversible Q0 decision (org-design Sec.1.0): archival<->short-term
        # cannot change once staged (it would mean moving between the org and a personal account).  So
        # the selector is editable only while CREATING; when editing a reopened staged repo it is hidden
        # (to change type, discard and start over).  Reflect the staged repo's fixed type on it anyway,
        # so accessionData['repoType'] stays correct for the re-save.
        if hasattr(self.createUI, "repoTypeGroup"):
            self.createUI.repoTypeGroup.visible = not editing
            if editing:
                owner = self._stagedNameWithOwner.split("/")[0]
                try:
                    orgs = (self.logic.morphoDepotOrg, self.logic.morphoDepotTestingOrg)
                except Exception:
                    orgs = ("MorphoDepot", "MorphoDepotTesting")
                if owner in orgs:
                    self.createUI.archivalRadio.checked = True
                else:
                    self.createUI.shortTermRadio.checked = True

    def onCurrentTabChanged(self,index):
        qt.QSettings().setValue("MorphoDepot/tabIndex", index)
        self.updateRefreshButtonLabels()
        if index == self.createTabIndex:
            if not self.ownerSelectorPopulated:
                self.populateOwnerSelector()
            self._refreshAutoAssignAvailability()
            self._refreshArchivalAvailability()
            self.refreshStagedReposList()

    def _onRepoTypeChanged(self, _checked=False):
        """Q0 fork (org-design Sec.1.0/9.7): the top-level repository-type selector drives the
        (hidden) accession-form repoType question.  Auto-assign is independent of repo type now
        (on by default / opt-out for ALL types, scope-gated), so this no longer touches it."""
        archival = self.createUI.archivalRadio
        shortTerm = self.createUI.shortTermRadio
        if not archival.checked and not shortTerm.checked:
            return  # neither chosen yet
        isArchival = archival.checked
        # keep accessionData['repoType'] populated by driving the hidden form question
        try:
            label = ("Archival (intended for long-term maintenance)" if isArchival
                     else "Short-term (e.g. repositories for classroom exercises, "
                          "that are not meant to be maintained for long-term)")
            self.createUI.accessionForm.questions["repoType"].optionButtons[label].click()
        except Exception:
            pass

    def _refreshAutoAssignAvailability(self):
        """Check whether the gh token has the `workflow` scope and enable the auto-assign checkbox
        accordingly.  A positive result is cached; a negative/errored result is re-checked on each
        Create-tab entry so granting the scope takes effect without a reload.  Without the scope,
        pushing a `.github/workflows/` file would be rejected, so the option is disabled with a hint
        rather than failing the whole repo creation later."""
        if self._workflowScopeChecked:
            return
        try:
            self._hasWorkflowScope = self.logic.hasWorkflowScope()
        except Exception as e:
            logging.warning(f"Could not check workflow scope: {e}")
            self._hasWorkflowScope = False
        # Cache only a POSITIVE result; if the scope is absent or the probe failed, re-check on the
        # next Create-tab entry so `gh auth refresh -s workflow` takes effect without a module reload.
        self._workflowScopeChecked = bool(self._hasWorkflowScope)
        checkBox = self.createUI.autoAssignCheckBox
        checkBox.enabled = self._hasWorkflowScope
        if not self._hasWorkflowScope:
            checkBox.checked = False
            checkBox.toolTip = (
                "Auto-assign needs the 'workflow' scope on your GitHub login. Enable it by "
                "running:  gh auth refresh -s workflow")

    def _refreshArchivalAvailability(self):
        """Disable the Archival option for users we can CONFIRM are not MorphoDepot org members, so
        they are guided to Short-term instead of filling the whole form only to be rejected at
        submit.  On 'unknown' (control plane unreachable) leave it enabled — the submit-time guard
        and message handle that, so a transient blip never locks out a real member."""
        radio = getattr(self.createUI, "archivalRadio", None)
        if radio is None:
            return
        if getattr(self, "_resumedForEdit", False):
            return  # editing a staged repo: the type is fixed (repoTypeGroup is hidden), and the
                    # radio reflects the repo's type — gating here would wrongly flip an archival
                    # edit to Short-term.
        isNonMember = (self.logic.orgMembershipStatus() == "non_member")
        radio.enabled = not isNonMember
        if isNonMember:
            radio.toolTip = _(
                "Archival repositories are created in the MorphoDepot organization and are limited "
                "to members. Join at join.morphodepot.org, or choose Short-term to create on your "
                "own account.")
            if radio.checked:
                self.createUI.shortTermRadio.checked = True
        else:
            radio.toolTip = ""

    def _showArchivalMembershipRequired(self):
        """Warning-styled dialog (not an error/crash style) explaining the membership requirement,
        with a button that opens the join site — mirrors the checkModuleEnabled dialog pattern."""
        msgBox = qt.QMessageBox()
        msgBox.setIcon(qt.QMessageBox.Warning)
        msgBox.setWindowTitle(_("Membership required"))
        msgBox.setText(_("You are not a member of the MorphoDepot organization, so you cannot "
                         "create an archival repository."))
        msgBox.setInformativeText(_(
            "Archival repositories are long-term, citable, and live in the MorphoDepot organization "
            "(members only). Short-term repositories are created on your own account and are open to "
            "anyone.\n\nTo create this now, set the Repository Type to Short-term. To create archival "
            "datasets, join the organization."))
        joinButton = msgBox.addButton(_("Open join.morphodepot.org"), qt.QMessageBox.ActionRole)
        msgBox.addButton(qt.QMessageBox.Ok)
        msgBox.exec_()
        if msgBox.clickedButton() == joinButton:
            qt.QDesktopServices.openUrl(qt.QUrl("https://join.morphodepot.org"))

    def _onAccessionFormValidated(self, valid):
        """Enable the "Create (stage privately)" button when the accession form is valid — but
        only while no repo is staged (the Go-live gate hidden).  Once a repo is staged or open
        for editing, the gate is visible and Create is locked (Update/Publish are the actions)."""
        # The accession form's first validateForm() fires during setup, before goLiveGroup is
        # built — treat a missing gate as "not staged" (create mode).
        goLiveGroup = getattr(self.createUI, "goLiveGroup", None)
        gateVisible = goLiveGroup is not None and goLiveGroup.visible
        self.createUI.createRepository.enabled = valid and not gateVisible
        # In edit mode the Update button must also reflect form validity — which includes the
        # Section-6 redistribution acknowledgement (validateForm) — so unticking that box disables
        # Update, not just Create.
        saveEdits = getattr(self.createUI, "saveEditsButton", None)
        if saveEdits is not None and getattr(self, "_resumedForEdit", False):
            saveEdits.enabled = valid
        self._scheduleRepoNameAvailabilityCheck()   # F4: advisory availability of the suggested name

    def _scheduleRepoNameAvailabilityCheck(self):
        """Debounce an advisory GitHub availability check for the (possibly auto-suggested) repo
        name -- fired ~0.7s after the user stops changing the form, so it never blocks typing."""
        form = getattr(self.createUI, "accessionForm", None)
        if form is None or not hasattr(form, "repoNameStatus"):
            return
        timer = getattr(self, "_repoNameCheckTimer", None)
        if timer is None:
            timer = qt.QTimer()
            timer.setSingleShot(True)
            timer.setInterval(700)
            timer.connect("timeout()", self._checkSuggestedRepoNameAvailability)
            self._repoNameCheckTimer = timer
        timer.start()

    def _checkSuggestedRepoNameAvailability(self):
        """Tell the user whether the current repo name is free on their account (advisory only --
        the authoritative check happens at create time).  Best-effort: stays silent on any error."""
        form = getattr(self.createUI, "accessionForm", None)
        if form is None or not hasattr(form, "repoNameStatus"):
            return
        name = (form.questions["githubRepoName"].answer() or "").strip()
        if not name:
            form.repoNameStatus.text = ""
            return
        if name == getattr(self, "_lastCheckedRepoName", None):
            return
        self._lastCheckedRepoName = name
        try:
            owner = self.logic.whoami()
        except Exception:
            owner = None
        if not owner:
            return
        try:
            taken = self.logic.repoExists(f"{owner}/{name}")
        except Exception as e:
            logging.warning(f"Repo-name availability check failed: {e}")
            form.repoNameStatus.text = ""
            return
        if taken:
            form.repoNameStatus.text = (
                f"<span style='color:#a4671a;'>‘{name}’ already exists in your account "
                "— edit the name below.</span>")
        else:
            form.repoNameStatus.text = (
                f"<span style='color:#2a7a2a;'>‘{name}’ is available — you can edit "
                "it if you like.</span>")

    def _redistributionAcknowledged(self):
        """True if the Section-6 'I have the right to allow redistribution of this data' box is
        ticked.  Required to stage (validateForm), and enforced again at Update and Publish so the
        attestation cannot be revoked while still proceeding."""
        try:
            return self.createUI.accessionForm.questions["redistributionAcknowledgement"].answer() != []
        except Exception:
            return False

    def _setGoLiveZoneVisible(self, visible):
        """Show/hide the entire 'Make Repository Public' zone — divider + bold header + box —
        as one unit, so the separator never dangles when the publish controls are hidden."""
        self.createUI.goLiveDivider.visible = visible
        self.createUI.goLiveHeader.visible = visible
        self.createUI.goLiveGroup.visible = visible

    def _updatePublishEnabled(self, *args):
        """Publish is enabled only when the Go-live gate is open.  For non-members it ALSO
        requires a syntactically valid contact email (mandatory) — a fake-but-valid address is
        accepted (we validate format, not deliverability) so someone who declines to share can
        still proceed, but only via a deliberate, valid-looking entry.  Org members are already
        on the contact list from ORCID onboarding, so no email is asked of them."""
        if not self.createUI.goLiveGroup.visible:
            self.createUI.publishButton.enabled = False
            return
        if not getattr(self, "_contactEmailNeeded", True):
            self.createUI.publishButton.enabled = True
            return
        emailRegex = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
        email = self.createUI.goLiveEmail.text.strip()
        self.createUI.publishButton.enabled = bool(re.match(emailRegex, email))

    def populateOwnerSelector(self):
        """Fill the Create-tab destination dropdown with the active user's account and orgs.

        Detects the organizations the logged-in GitHub account belongs to; if there are any,
        the destination selector offers a choice between the personal account and each
        organization (consulted at Go-live).  Runs lazily the first time the Create tab is
        opened; transient failures (e.g. gh not yet configured) leave the flag unset so it
        retries next time."""
        destination = getattr(self.createUI, "destinationQuestion", None)
        if destination is None:
            return
        try:
            personal = self.logic.whoami()
        except Exception as e:
            logging.warning(f"Could not determine active GitHub user: {e}")
            personal = ""
        organizations = self.logic.userOrganizations() if personal else []
        options = []
        if personal:
            options.append((f"{personal} (your personal account)", personal))
        for org in organizations:
            options.append((f"{org} (organization)", org))
        destination.setOptions(options)
        destination.questionBox.setVisible(False)  # destination is chosen at create now, not publish
        self.createUI.destinationPersonalLogin = personal
        self.ownerSelectorPopulated = bool(personal)

    def selectedDestination(self):
        """Return the chosen Go-live destination owner login, or '' if none resolved yet."""
        destination = getattr(self.createUI, "destinationQuestion", None)
        return destination.answer() if destination is not None else ""

    def selectedDestinationIsOrganization(self):
        """True when the chosen Go-live destination is an organization, not the personal account."""
        owner = self.selectedDestination()
        return bool(owner) and owner != getattr(self.createUI, "destinationPersonalLogin", "")

    def updateRefreshButtonLabels(self):
        """Suffix the Annotate/Review/Release Refresh GitHub buttons with the active gh user."""
        try:
            user = self.logic.whoami()
            suffix = f" (user: {user})"
        except Exception:
            suffix = ""
        self.annotateUI.refreshButton.text = f"Refresh Github{suffix}"
        self.reviewUI.refreshButton.text = f"Refresh Github{suffix}"
        self.releaseUI.refreshButton.text = f"Refresh Github{suffix}"

    def onAdminModeChanged(self, state):
        isAdmin = (state == qt.Qt.Checked)
        qt.QSettings().setValue("MorphoDepot/adminMode", isAdmin)
        self.tabWidget.setTabVisible(self.adminTabIndex, isAdmin)

    def updateGitConfigInfo(self):
        userNameLabel = self.configureUI.gitConfigLayout.labelForField(self.configureUI.userNameLineEdit)
        userEmailLabel = self.configureUI.gitConfigLayout.labelForField(self.configureUI.userEmailLineEdit)

        gitIsWorking = self.logic.gitExecutablePath and self.logic.checkCommand([self.logic.gitExecutablePath, '--version'])

        self.configureUI.userNameLineEdit.enabled = gitIsWorking
        self.configureUI.userEmailLineEdit.enabled = gitIsWorking
        if userNameLabel: userNameLabel.enabled = gitIsWorking
        if userEmailLabel: userEmailLabel.enabled = gitIsWorking

        if gitIsWorking:
            userName = self.logic.getGitConfig("user.name")
            userEmail = self.logic.getGitConfig("user.email")

            # `gh auth login` configures the credential helper but never sets git's commit
            # identity, so on a fresh instance these are empty.  Seed them from the GitHub
            # profile (gh) instead of forcing the user to type them, writing back to the global
            # git config so commits work.  We only fill *empty* fields — a value the user has
            # already set is never overwritten.  The email comes from the public GitHub profile;
            # when the user keeps it private it is empty, so the field stays blank and the
            # "Required for commits" prompt remains (it is mandatory and must be entered).
            if not userName or not userEmail:
                profile = self.logic.ghUserProfile()
                if not userName:
                    userName = profile["name"] or profile["login"]
                    if userName:
                        self.logic.setGitConfig("user.name", userName)
                if not userEmail and profile["email"]:
                    userEmail = profile["email"]
                    self.logic.setGitConfig("user.email", userEmail)

            self.configureUI.userNameLineEdit.text = userName
            self.configureUI.userNameStatusLabel.visible = not bool(userName)

            self.configureUI.userEmailLineEdit.text = userEmail
            self.configureUI.userEmailStatusLabel.visible = not bool(userEmail)
        else:
            self.configureUI.userNameLineEdit.clear()
            self.configureUI.userEmailLineEdit.clear()
            self.configureUI.userNameStatusLabel.visible = False
            self.configureUI.userEmailStatusLabel.text = "Git must be correctly installed in order to enable configuration"
            self.configureUI.userEmailStatusLabel.visible = True

    def onAutoAssignHelp(self):
        """Explain the auto-assign option in a popup.  Kept as a popup for now; the
        'Open Documentation' button is where a proper docs page will be linked later."""
        msgBox = qt.QMessageBox()
        msgBox.setWindowTitle("Auto-assign new issues to their creators")
        msgBox.setIcon(qt.QMessageBox.Information)
        msgBox.setText("Automatically assign each new issue to the person who opened it.")
        msgBox.setInformativeText(
            "When enabled, MorphoDepot adds a small GitHub Actions workflow to this repository. "
            "From then on, whenever someone opens an issue, GitHub assigns that issue to them "
            "automatically.\n\n"
            "That way you don't have to manually assign every task your students create back to "
            "them — the person who reports or requests something is already the assignee.\n\n"
            "This requires your GitHub login to have the 'workflow' scope. The option is left "
            "disabled until that scope is present.")
        openDocsButton = msgBox.addButton("Open Documentation", qt.QMessageBox.ActionRole)
        msgBox.addButton(qt.QMessageBox.Ok)
        msgBox.exec_()
        if msgBox.clickedButton() == openDocsButton:
            qt.QDesktopServices.openUrl(qt.QUrl(
                "https://github.com/MorphoCloud/SlicerMorphoDepot?tab=readme-ov-file#morphodepot"))

    def onUserNameChanged(self, userName):
        if userName:
            self.logic.setGitConfig("user.name", userName)
        self.configureUI.userNameStatusLabel.visible = not bool(userName)

    def onUserEmailChanged(self, userEmail):
        if userEmail:
            self.logic.setGitConfig("user.email", userEmail)
        self.configureUI.userEmailStatusLabel.visible = not bool(userEmail)

    # Create
    def _collectAccessionInputs(self):
        """Validate the Create-tab selections and assemble accessionData.

        Returns (sourceVolume, colorTable, sourceSegmentation, accessionData) or None if
        anything is missing/invalid or the user cancels the terminology prompt."""
        if self.createUI.inputSelector.currentNode() == None or self.createUI.colorSelector.currentNode() == None:
            slicer.util.errorDisplay("Need to select volume and color table")
            return None

        sourceVolume = self.createUI.inputSelector.currentNode()
        sourceSegmentation = self.createUI.segmentationSelector.currentNode()
        colorTable = self.createUI.colorSelector.currentNode()

        validGithubAsset = r'^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$'
        if re.fullmatch(validGithubAsset, sourceVolume.GetName()) is None:
            slicer.util.errorDisplay("Please rename volume.\n"
                "Only alphanumerics, periods, hyphens and underscores accepted.")
            return None
        if re.fullmatch(validGithubAsset, colorTable.GetName()) is None:
            slicer.util.errorDisplay("Please rename color table.\n"
                "Only alphanumerics, periods, hyphens and underscores accepted.\n"
                "Use the 'All nodes' tab of the Data module to access the color table and right-click to rename.")
            return None

        accessionData = self.createUI.accessionForm.accessionData()
        accessionData['scanDimensions'] = str(sourceVolume.GetImageData().GetDimensions())
        accessionData['scanSpacing'] = str(sourceVolume.GetSpacing())

        if accessionData["repoType"][1] == "Archival (intended for long-term maintenance)":
            for colorIndex in range(1, colorTable.GetNumberOfColors()):
                if colorTable.GetTerminologyAsString(colorIndex) == "~^^~^^~^^~~^^~^^~":
                    slicer.util.errorDisplay(f"Selected Color table is missing terminology for index {colorIndex}, {colorTable.GetColorName(colorIndex)}", windowTitle="Missing Terminology")
                    return None
        else:
            validTerminology = True
            for colorIndex in range(1, colorTable.GetNumberOfColors()):
                if colorTable.GetTerminologyAsString(colorIndex) == "~^^~^^~^^~~^^~^^~":
                    validTerminology = False
                    break
            if not validTerminology:
                ok = slicer.util.confirmOkCancelDisplay("Color table does not have complete terminology.  Click OK to fill with defaults or Cancel to fill manually", windowTitle="Missing Terminology")
                if ok:
                    for colorIndex in range(1, colorTable.GetNumberOfColors()):
                        colorTable.SetTerminology(colorIndex, "SCT", "85756007", "Tissue", "SCT", "85756007", "Tissue")
                else:
                    return None

        return sourceVolume, colorTable, sourceSegmentation, accessionData

    def _submitContactForm(self):
        """Best-effort submission of the creator's contact info to the MorphoDepot contact list.
        Called at PUBLISH time only — never for staged-only repos, so the list never accumulates
        contacts for repositories that are discarded or never go public.  Reads the email from
        the Go-live field and the repo type from the (still-populated) accession form, and runs
        on a background thread so it never blocks the UI."""
        CONTACT_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLScqzoTAIklSg2Dc4sQHMw-_J8PPQUOSBqFrpLnWpLS-tvvVHQ/formResponse"
        CONTACT_FORM_ENTRY_EMAIL       = "entry.2057466047"  # Email Address
        CONTACT_FORM_ENTRY_GH_USER     = "entry.1912463514"  # GitHub Username
        CONTACT_FORM_ENTRY_REPO_NAME   = "entry.683034902"   # Repository Name
        CONTACT_FORM_ENTRY_REPO_TYPE   = "entry.156019116"   # Repository Type
        if not getattr(self, "_contactEmailNeeded", True):
            # Org members already supplied a verified email at ORCID onboarding; nothing collected
            # here, so there is nothing to submit.
            return
        try:
            ghUser = self.logic.gh("api user --jq .login").strip()
        except Exception:
            ghUser = ""
        try:
            repoTypeFull = self.createUI.accessionForm.questions["repoType"].answer()
        except Exception:
            repoTypeFull = ""
        repoTypeShort = "Archival" if repoTypeFull.startswith("Archival") else "Short-term"
        repoName = (self._stagedNameWithOwner or "").split("/")[-1]
        formData = {
            CONTACT_FORM_ENTRY_EMAIL:     self.createUI.goLiveEmail.text.strip(),
            CONTACT_FORM_ENTRY_GH_USER:   ghUser,
            CONTACT_FORM_ENTRY_REPO_NAME: repoName,
            CONTACT_FORM_ENTRY_REPO_TYPE: repoTypeShort,
        }
        import threading
        def _post(url, data):
            try:
                requests.post(url, data=data, timeout=5)
            except Exception:
                pass  # non-critical
        threading.Thread(target=_post, args=(CONTACT_FORM_URL, formData), daemon=True).start()

    def onCreateRepository(self):
        """Stage the repository: build it locally and provision it PRIVATE on the personal
        account.  The Go-live gate then lets the user publish or discard it."""
        # A fresh create is never an edit-resume; restore normal Subject Data behavior.
        self._exitResumeMode()
        inputs = self._collectAccessionInputs()
        if inputs is None:
            return
        sourceVolume, colorTable, sourceSegmentation, accessionData = inputs

        # Q0 fork (org-design Sec.1.0 / 9.7): the repository TYPE determines where it lives — the two
        # never cross.  ARCHIVAL = born in the MorphoDepot org (S3, 10 GB, governed, DOI-minted) and
        # is members-only.  SHORT-TERM = the creator's personal account (release asset, 2 GB,
        # deletable anytime), available to anyone.  There is no separate destination prompt.
        useOrg = False
        testingOwner = None
        repoTypeAnswer = accessionData.get("repoType") or ["", ""]
        isArchival = str(repoTypeAnswer[1]).startswith("Archival")
        if self.testingMode:
            # Developer self-test: provision into the throwaway testing org (release asset, no App/S3).
            testingOwner = self.logic.morphoDepotTestingOrg
        elif isArchival:
            status = self.logic.orgMembershipStatus()
            if status == "non_member":
                self._showArchivalMembershipRequired()
                self.progressMethod("Repository creation aborted: archival requires membership")
                return
            if status == "unknown":
                slicer.util.messageBox(_(
                    "We couldn't verify your MorphoDepot membership right now. This is a temporary "
                    "connection issue, not a problem with your account.\n\nPlease try again in a "
                    "moment, or set the Repository Type to Short-term to create on your own account."),
                    windowTitle=_("Couldn't verify membership"))
                self.progressMethod("Repository creation aborted: membership could not be verified")
                return
            useOrg = True  # confirmed member -> the org
        # else: short-term -> personal account (useOrg stays False)

        if not self.showConfirmationDialog(sourceVolume, colorTable, accessionData, sourceSegmentation, self.screenshots, useOrg=useOrg):
            self.progressMethod("Repository creation aborted")
            return

        # Auto-assign workflow: ON by default for ALL repo types (opt-out via the checkbox).
        # Still scope-gated — without the `workflow` scope the box is unchecked + disabled, so
        # `checked` is already False there and we never try to push a workflow we cannot.
        enableAutoAssign = bool(self.createUI.autoAssignCheckBox.checked
                                and getattr(self, "_hasWorkflowScope", False))

        slicer.util.showStatusMessage("Staging repository (private)...")
        staged = None
        try:
            with slicer.util.tryWithErrorDisplay(_("Trouble creating repository"), waitCursor=True):
                staged = self.logic.createAccessionRepo(sourceVolume, colorTable, accessionData, sourceSegmentation, self.screenshots, useOrg=useOrg, targetOwner=testingOwner, enableAutoAssign=enableAutoAssign)
        except Exception as e:
            slicer.util.showStatusMessage("Cleaning up...")
            repoName = accessionData['githubRepoName'][1].split("/")[-1]
            repoDir = os.path.join(self.logic.localRepositoryDirectory(), repoName)
            if os.path.exists(repoDir):
                shutil.rmtree(repoDir)
            slicer.util.showStatusMessage("")
            return

        slicer.util.showStatusMessage("")
        if not staged:
            return
        self.screenshots = []
        self.updateScreenshotCount()
        self._enterGoLiveState(staged)

    def _enterGoLiveState(self, stagedNameWithOwner):
        """Reveal the Go-live gate after a repo has been staged privately."""
        self._stagedNameWithOwner = stagedNameWithOwner
        self.populateOwnerSelector()
        self.createUI.createRepository.enabled = False
        self._setGoLiveZoneVisible(True)
        # Org members are already on the contact list (verified email captured at ORCID
        # onboarding), so the contact-email field is redundant for them — hide it and drop the
        # publish gate.  Non-members must enter a valid contact email before Publish enables (see
        # _updatePublishEnabled).  Cleared on every entry so it is never carried over.
        self._contactEmailNeeded = not self.logic.userIsOrgMember()
        self.createUI.goLiveEmailLabel.visible = self._contactEmailNeeded
        self.createUI.goLiveEmail.visible = self._contactEmailNeeded
        # Pre-fill with the GitHub public profile email (gh) so non-members don't have to type
        # it; they can still edit it.  Empty when the profile email is private, in which case it
        # remains a mandatory entry gated by _updatePublishEnabled.
        self.createUI.goLiveEmail.text = self.logic.ghUserProfile()["email"] if self._contactEmailNeeded else ""
        self._updatePublishEnabled()
        self.createUI.discardButton.enabled = True
        self.createUI.openRepository.enabled = True
        # "Save changes" only applies when editing a reopened repo; a fresh stage has no edits.
        self.createUI.saveEditsButton.visible = self._resumedForEdit
        self.createUI.saveEditsButton.enabled = self._resumedForEdit
        self._updateCreateSectionHeader()
        if self._resumedForEdit:
            self.createUI.stagingStatusLabel.text = (
                f"Editing staged repo {stagedNameWithOwner} (private, not yet published).\n"
                "Correct any fields, Save changes as many times as you need, then Publish.")
        else:
            self.createUI.stagingStatusLabel.text = (
                f"Staged privately as {stagedNameWithOwner} — not yet public, not discoverable.\n"
                "Review it on GitHub, then choose a destination and Publish, or Discard.")
        # Passive duplicate-volume note (cached from staging — no network here): a shared SHA-256
        # means a byte-identical source volume already lives in another repo.
        stagingCtx = getattr(self.logic, "stagingContext", None) or {}
        dups = [r for r in (stagingCtx.get("duplicateRepos") or []) if r != stagedNameWithOwner]
        if dups:
            preview = ", ".join(dups[:3]) + (f" (+{len(dups) - 3} more)" if len(dups) > 3 else "")
            self.createUI.stagingStatusLabel.text += (
                f"\n⚠ This exact volume already exists in: {preview}. "
                "Discard if this duplicate is unintended.")
        self.refreshStagedReposList(force=True)

    def onSaveEdits(self):
        """Persist edits to the reopened staged repo without publishing, so it can be corrected
        repeatedly before going live."""
        if not self._resumedForEdit:
            return
        if not self._redistributionAcknowledged():
            slicer.util.errorDisplay(
                "Please confirm you have the right to allow redistribution of this data "
                "(Section 6: Licensing) before updating the repository.",
                windowTitle="Redistribution acknowledgement required")
            return
        gathered = self._gatherStagedEdits()
        if gathered is None:
            return  # aborted: the loaded segmentation was edited in place (user was warned)
        editedData, newColorTable, newSegmentation = gathered
        changed = False
        try:
            with slicer.util.tryWithErrorDisplay(_("Trouble updating repository"), waitCursor=True):
                slicer.util.showStatusMessage("Updating staged repository...")
                changed = self.logic.saveStagedRepoEdits(editedData, colorTable=newColorTable,
                                                         sourceSegmentation=newSegmentation,
                                                         screenshots=self.screenshots)
        except Exception as e:
            slicer.util.showStatusMessage("")
            return
        slicer.util.showStatusMessage("")
        self._rebaselineResumedNodes()
        repoName = self._stagedNameWithOwner
        if changed:
            statusText = f"✓ Repository updated: {repoName} (still staged — not yet published)."
            popupText = (f"'{repoName}' was updated.\n\nIt is still staged (private, not "
                         "published). Keep editing and updating, or click Publish when ready.")
        else:
            statusText = "No changes to save — the repository already matches the form."
            popupText = "No changes to save — the repository already matches the form."
        self.createUI.stagingStatusLabel.text = statusText
        self.refreshStagedReposList(force=True)
        if not self.testingMode:
            slicer.util.infoDisplay(popupText, windowTitle="MorphoDepot")

    def _enrichMemberPerson(self, person):
        """Pre-fill a member's name/ORCID/affiliation from their onboarding record so the curator never
        re-types info we already have (every archival creator is a member).  Org owners can read the
        owners-only onboarding-records repo; for others this 404s and we fall back to the GitHub
        profile name.  The App authoritatively re-enriches every member at mint."""
        import MorphoDepotContributors as MDC
        github = person.get("github")
        if not github or person.get("name"):
            return
        text, _ = self._readRepoFileViaApi("MorphoDepot/onboarding-records", f"records/{github}.json")
        if text:
            try:
                rec = json.loads(text)
            except Exception:
                rec = {}
            recName = rec.get("name", github)
            orcid = (rec.get("orcid") or "").strip()
            if "," in recName:
                # Already "Family, Given": use verbatim — must NOT pass through zenodo_name (its
                # whitespace-split fallback would reorder it), and skip the blocking ORCID lookup
                # (10s timeout, called in a loop over all people before the dialog opens).
                person["name"] = recName
            else:
                given, family = MDC.orcid_name(orcid) if orcid else (None, None)
                if given and family:
                    person["name"] = f"{family}, {given}"  # authoritative "Family, Given" from ORCID
                else:
                    # R-9: ORCID unavailable -> use the onboarding name VERBATIM; never heuristically
                    # reorder (it would mangle "Maria de la Cruz" -> "Cruz, Maria de la" permanently).
                    person["name"] = recName
            if orcid and not person.get("orcid"):
                person["orcid"] = orcid
            if rec.get("institution") and not person.get("affiliation"):
                person["affiliation"] = rec["institution"]
            person["source"] = "member"
            return
        try:  # fallback: GitHub profile display name (no ORCID)
            profile = self.logic.ghJSON(["api", f"/users/{github}"])
            if isinstance(profile, dict) and profile.get("name"):
                person["name"] = profile["name"]
        except Exception:
            pass

    def _enrichMembersViaApi(self, data):
        """Resolve every contributor handle to a member identity in ONE App call (the App checks org
        membership and reads the owners-only onboarding records, which a non-owner curator cannot).
        Members get name/ORCID/affiliation and source='member'; true outsiders stay handle-only.
        Falls back to the per-person _enrichMemberPerson (owner-readable records + GitHub profile) if
        the App is unreachable, so the dialog still degrades gracefully offline.  (org-design 9.5/9.6)
        """
        people = data.get("people", [])
        # Resolve EVERY GitHub handle (even already-named rows) so the stable numeric github_id gets
        # filled for all of them (Sec.9.5 C5); names/ORCID are still only filled for blank rows.
        handles = [p["github"] for p in people if p.get("github")]
        resolved = None
        if handles:
            try:
                resolved = self.logic.controlPlaneRequest("contributors/resolve", {"handles": handles})
            except Exception as e:
                logging.warning(f"Could not resolve member identities via the App: {e}")
        for person in people:
            github = person.get("github")
            if not github:
                continue
            info = (resolved or {}).get(github)
            if info is None:
                if not person.get("name"):
                    self._enrichMemberPerson(person)  # App unreachable -> best-effort local
                continue
            if info.get("github_id") and not person.get("github_id"):
                person["github_id"] = info["github_id"]   # stable id, even for already-named rows
            if info.get("is_member"):
                person["source"] = "member"
            if person.get("name"):
                continue  # already named -> keep it (id filled above)
            if info.get("name"):
                person["name"] = info["name"]
                if info.get("orcid") and not person.get("orcid"):
                    person["orcid"] = info["orcid"]
                if info.get("affiliation") and not person.get("affiliation"):
                    person["affiliation"] = info["affiliation"]
            elif not info.get("is_member"):
                self._enrichMemberPerson(person)  # confirmed outsider -> try a profile display name

    def _repoHasBaseline(self, nameWithOwner):
        """True if the staged repo carries a baseline.seg.nrrd (it shipped a baseline -> atlas case).
        A from-scratch archival repo has no baseline file and skips the credit gate at go-live."""
        try:
            info = self.logic.ghJSON(["api", f"/repos/{nameWithOwner}/contents/baseline.seg.nrrd"])
            return isinstance(info, dict) and bool(info.get("sha"))
        except Exception:
            return False

    def _readRepoFileViaApi(self, nameWithOwner, path):
        """Return (text, sha) for a repo file via the GitHub API, or (None, None) if absent."""
        import base64
        try:
            info = self.logic.ghJSON(["api", f"/repos/{nameWithOwner}/contents/{path}"])
            if isinstance(info, dict) and info.get("content"):
                return base64.b64decode(info["content"]).decode(), info.get("sha")
        except Exception:
            pass
        return None, None

    def _curateBaselineCreditViaApi(self, nameWithOwner):
        """Atlas case at go-live: collect/confirm who made the baseline and write CONTRIBUTORS.json to
        the staged repo via the GitHub API (no local clone exists at publish; the repo is private but
        the curator has write).  Returns False to abort the publish.  Loads any existing
        CONTRIBUTORS.json so names are never re-entered (org-design Sec.9.6/9.7)."""
        import base64
        import MorphoDepotContributors as MDC
        text, existingSha = self._readRepoFileViaApi(nameWithOwner, "CONTRIBUTORS.json")
        try:
            data = json.loads(text) if text else MDC.new_record(nameWithOwner)
        except (ValueError, json.JSONDecodeError):
            logging.warning("CONTRIBUTORS.json is not valid JSON; starting from a fresh record")
            data = MDC.new_record(nameWithOwner)
        curatorText, _ = self._readRepoFileViaApi(nameWithOwner, "CURATOR")
        curator = (curatorText or "").strip() or self.logic.whoami()
        MDC.ensure_person(data, github=curator, author=True, source="member")["curator"] = True
        for member in data["people"]:   # pre-fill any rows we already have info for (the curator etc.)
            self._enrichMemberPerson(member)

        panel = MDC.make_contributor_panel(data, title="Who made the baseline segmentation?",
                                           show_header=False, show_contributions=False)
        dialog = qt.QDialog(slicer.util.mainWindow())
        dialog.setWindowTitle("Baseline contributors")
        dialog.setMinimumWidth(560)
        dialog.setMaximumWidth(760)
        dialogLayout = qt.QVBoxLayout(dialog)
        message = qt.QLabel(
            "<b>Who made the baseline segmentation?</b><br><br>"
            "Is there anyone other than yourself you would like to acknowledge for contributing to this "
            "existing segmentation? If you want, you can elevate them to co-author status; otherwise they "
            "are listed as contributors. Your information is automatically added as the lead author to "
            "the citation.<br><br>"
            "When you are done &mdash; or if you don't have anyone to add &mdash; click <b>Done</b>. "
            "&nbsp;<a href=\"%s\">Documentation</a>" % MDC.DOC_URL)
        message.setWordWrap(True)
        message.setOpenExternalLinks(True)
        dialogLayout.addWidget(message)
        dialogLayout.addWidget(panel.widget)
        # NOTE: build buttons with addButton(text, role) — PythonQt does NOT honor the
        # QDialogButtonBox(Ok | Cancel) flags constructor (it yields an empty, button-less box).
        buttonBox = qt.QDialogButtonBox()
        buttonBox.addButton("Done", qt.QDialogButtonBox.AcceptRole)
        buttonBox.addButton("Cancel", qt.QDialogButtonBox.RejectRole)
        buttonBox.accepted.connect(dialog.accept)
        buttonBox.rejected.connect(dialog.reject)
        dialogLayout.addWidget(buttonBox)
        while True:
            if not dialog.exec_():
                return False  # cancelled -> caller blocks the publish
            panel.syncFromTable()
            if panel.isReady():
                break
            slicer.util.warningDisplay("Each cited author needs a real name (Family, Given).",
                                       windowTitle="Author name required")

        content = base64.b64encode(MDC.dumps(panel.data).encode()).decode()
        command = ["api", f"/repos/{nameWithOwner}/contents/CONTRIBUTORS.json", "-X", "PUT",
                   "-f", "message=Add CONTRIBUTORS.json (baseline credit at go-live)",
                   "-f", f"content={content}"]
        if existingSha:
            command += ["-f", f"sha={existingSha}"]
        self.logic.gh(command)
        return True

    def onPublish(self):
        """Take the staged repo live (make it public) where it already lives — the destination
        was fixed at create, so this no longer offers an org/personal choice or any transfer."""
        if not self._redistributionAcknowledged():
            slicer.util.errorDisplay(
                "Please confirm you have the right to allow redistribution of this data "
                "(Section 6: Licensing) before publishing.",
                windowTitle="Redistribution acknowledgement required")
            return
        ctx = getattr(self.logic, "stagingContext", None) or {}
        where = ctx.get("personalNameWithOwner", "the staged repository")
        isOrg = bool(ctx.get("isMember"))
        # Advisory duplicate-volume gate (non-blocking): if this exact volume (identical SHA-256)
        # is already published elsewhere, surface it before the one-way publish.  Cached from
        # staging — no network here; skipped silently when the index was unreachable.
        dups = [r for r in (ctx.get("duplicateRepos") or []) if r != where]
        if dups and not self.testingMode:
            shown = "\n".join(f"  • {r}" for r in dups[:10])
            more = f"\n  …and {len(dups) - 10} more" if len(dups) > 10 else ""
            if not slicer.util.confirmOkCancelDisplay(
                    "This exact source volume (identical checksum) is already in:\n"
                    f"{shown}{more}\n\n"
                    "Publishing will create another public copy of the same data. Publish anyway?",
                    windowTitle="Duplicate volume detected"):
                return
        prompt = (f"Publish {where}?\n\nThis makes it public and discoverable"
                  + (" in the MorphoDepot organization." if isOrg else " on your account."))
        if not (self.testingMode or slicer.util.confirmOkCancelDisplay(prompt, windowTitle="Publish repository")):
            return
        # Gather any pending edits up front so we can abort cleanly (without publishing) if the
        # loaded segmentation was edited in place.
        editsBundle = None
        if self._resumedForEdit:
            editsBundle = self._gatherStagedEdits()
            if editsBundle is None:
                return  # warned; abort publish
        final = None
        try:
            with slicer.util.tryWithErrorDisplay(_("Trouble publishing repository"), waitCursor=True):
                # Apply pending edits first (rewrites main as one clean commit; the source
                # volume is never touched).  No-op if nothing changed since the last save.
                if editsBundle is not None:
                    slicer.util.showStatusMessage("Saving edits...")
                    editedData, newColorTable, newSegmentation = editsBundle
                    self.logic.saveStagedRepoEdits(editedData, colorTable=newColorTable,
                                                   sourceSegmentation=newSegmentation,
                                                   screenshots=self.screenshots)
        except Exception as e:
            slicer.util.showStatusMessage("")
            return

        # Contributor-credit gate (archival + baseline): an archival repo that ships a baseline at
        # go-live (= v1) must declare who made that baseline before it can be made public.  No local
        # clone exists at publish, so this reads/writes CONTRIBUTORS.json on GitHub directly (the repo
        # is private but the curator has write).  Cancelling here blocks the publish.  (org-design 9.6/9.7)
        nameWithOwner = ctx.get("personalNameWithOwner")
        if isOrg and nameWithOwner and self._repoHasBaseline(nameWithOwner):
            try:
                creditOk = self._curateBaselineCreditViaApi(nameWithOwner)
            except Exception as exc:
                slicer.util.showStatusMessage("")
                slicer.util.errorDisplay(
                    f"Could not record the baseline contributors on GitHub:\n{exc}\n\nPublish aborted.",
                    windowTitle="Baseline credit failed")
                return  # writing CONTRIBUTORS.json failed -> do NOT make public
            if not creditOk:
                slicer.util.showStatusMessage("")
                return  # curator cancelled the credit step -> do NOT make public

        try:
            with slicer.util.tryWithErrorDisplay(_("Trouble publishing repository"), waitCursor=True):
                slicer.util.showStatusMessage("Publishing...")
                final = self.logic.publishStagedRepo()
        except Exception as e:
            slicer.util.showStatusMessage("")
            return
        slicer.util.showStatusMessage("")
        if not final:
            return
        # Org publish is routed through the MD-reviewers review gate (org-design 11.3): the App
        # emailed an Approve link and did NOT make the repo public yet.  Report that review was
        # requested and leave the repo staged; do NOT add a contact (the repo isn't public).
        if isinstance(final, dict) and final.get("pending"):
            where = final.get("nameWithOwner")
            to = final.get("reviewSentTo") or "the MorphoDepot reviewers"
            self._exitResumeMode()
            self._stagedNameWithOwner = where
            self.createUI.publishButton.enabled = False
            self.createUI.discardButton.enabled = False
            self.createUI.openRepository.enabled = False
            self.createUI.stagingStatusLabel.text = (
                f"Submitted for review: {where} becomes public once approved.")
            self.refreshStagedReposList(force=True)
            if not self.testingMode:
                slicer.util.infoDisplay(
                    f"'{where}' has been submitted for review.\n\n"
                    f"A request was emailed to {to}. Once a reviewer approves, the repository is "
                    "made public automatically and you'll get an email. Until then it stays "
                    "private and remains in your unpublished list (reopen it there to make "
                    "changes, which will require requesting review again).",
                    windowTitle="Submitted for review")
            slicer.mrmlScene.Clear()
            return
        # Now that the repo is actually public, add the creator to the contact list (best-effort,
        # background).  Done here — never at stage — so discarded/abandoned repos never leak a
        # contact.  Reads _stagedNameWithOwner (the repo name is stable across the transfer).
        self._submitContactForm()
        self._exitResumeMode()
        self._stagedNameWithOwner = final
        self.createUI.publishButton.enabled = False
        self.createUI.discardButton.enabled = False
        self.createUI.openRepository.enabled = True
        self.createUI.stagingStatusLabel.text = f"Published: {final} is now public and discoverable."
        self.refreshStagedReposList(force=True)
        # Publishing fully succeeded: open the now-public repository in the browser, and clear the
        # scene so the (potentially multi-GB) source volume and segmentation do not linger in the
        # session.  The browser is skipped under automated testing.
        if not self.testingMode:
            qt.QDesktopServices.openUrl(qt.QUrl(f"https://github.com/{final}"))
        slicer.mrmlScene.Clear()

    def onDiscard(self):
        """Abandon the staged repo.  Deleting a repo needs the `delete_repo` token scope,
        which we don't request, so instead we open the repo's GitHub Settings page for the
        user to delete it from the web (Danger Zone), and clean up our own side."""
        if not (self.testingMode or slicer.util.confirmOkCancelDisplay(
                "Discard the staged repository?\n\n"
                "Its GitHub Settings page will open so you can delete it from the web "
                "(scroll to the Danger Zone -> 'Delete this repository').  The local copy will "
                "be removed.", windowTitle="Discard repository")):
            return
        settingsURL = None
        try:
            with slicer.util.tryWithErrorDisplay(_("Trouble discarding repository"), waitCursor=True):
                slicer.util.showStatusMessage("Discarding...")
                settingsURL = self.logic.discardStagedRepo()
        except Exception as e:
            slicer.util.showStatusMessage("")
            return
        slicer.util.showStatusMessage("")
        if settingsURL and not self.testingMode:
            qt.QDesktopServices.openUrl(qt.QUrl(settingsURL))
            slicer.util.infoDisplay(
                "Opened the repository's GitHub Settings page.\n\n"
                "Scroll to the bottom (Danger Zone) and click 'Delete this repository' to "
                "remove it from GitHub.",
                windowTitle="Delete on GitHub")
        self._setGoLiveZoneVisible(False)
        self.createUI.stagingStatusLabel.text = ""
        self.createUI.openRepository.enabled = False
        self.refreshStagedReposList(force=True)
        self.onClearForm()

    def showConfirmationDialog(self, sourceVolume, colorTable, accessionData, sourceSegmentation, screenshots, useOrg=False):
        """Shows a confirmation dialog with a summary of the repository to be created."""
        if self.testingMode:
            return True
        dialog = qt.QDialog(slicer.util.mainWindow())
        dialog.setWindowTitle("Confirm Repository Creation")
        layout = qt.QVBoxLayout(dialog)

        # When creating under the org, make the destination unmistakable at the top.
        if useOrg:
            repoName = accessionData['githubRepoName'][1].split("/")[-1]
            headerLabel = qt.QLabel(
                f"<b>This repository will be created in the MorphoDepot organization, as "
                f"<code>{self.logic.morphoDepotOrg}/{repoName}</code>.</b><br>"
                "Cancel if you'd rather create it under your own account.")
            headerLabel.setWordWrap(True)
            headerLabel.setStyleSheet("color:#b35900;")
            layout.addWidget(headerLabel)

        # Summary Text
        summaryText = self.getAccessionSummary(sourceVolume, colorTable, accessionData, sourceSegmentation)
        summaryLabel = qt.QLabel(summaryText)
        summaryLabel.setWordWrap(True)
        layout.addWidget(summaryLabel)

        # Screenshots
        if screenshots:
            screenshotsGroup = qt.QGroupBox("Screenshots")
            screenshotsLayout = qt.QHBoxLayout()
            screenshotsGroup.setLayout(screenshotsLayout)
            scrollArea = qt.QScrollArea()
            scrollArea.setWidgetResizable(True)
            scrollArea.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAsNeeded)
            scrollArea.setVerticalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
            screenshotWidget = qt.QWidget()
            screenshotLayout = qt.QHBoxLayout()
            screenshotWidget.setLayout(screenshotLayout)

            for ss in screenshots:
                pixmap = qt.QPixmap(ss['path'])
                label = qt.QLabel()
                label.setPixmap(pixmap.scaledToHeight(128, qt.Qt.SmoothTransformation))
                if ss['caption']:
                    label.setToolTip(ss['caption'])
                screenshotLayout.addWidget(label)

            scrollArea.setWidget(screenshotWidget)
            screenshotsLayout.addWidget(scrollArea)
            layout.addWidget(screenshotsGroup)

        # Dialog buttons
        buttonBox = qt.QDialogButtonBox(qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel, dialog)
        buttonBox.accepted.connect(dialog.accept)
        buttonBox.rejected.connect(dialog.reject)
        layout.addWidget(buttonBox)

        return dialog.exec_() == qt.QDialog.Accepted

    def getAccessionSummary(self, sourceVolume, colorTable, accessionData, sourceSegmentation):
        """Generates a summary string from the accession data."""
        summary = "<b>Review the details of the repository to be created:</b><br><br>"

        def add_detail(label, value):
            return f"{label}: <i>{value}</i><br>"

        summary += add_detail("GitHub Repository", accessionData['githubRepoName'][1])
        summary += add_detail("Initial state", "Private staging repo on your personal account "
                              "(made public — and transferred to an org if you choose — at Go-live)")
        summary += add_detail("Source Volume", sourceVolume.GetName())
        summary += add_detail("Color Table", colorTable.GetName())
        if sourceSegmentation:
            summary += add_detail("Baseline Segmentation", sourceSegmentation.GetName())

        summary += "<br><b>Specimen Details:</b><br>"
        summary += add_detail("Species", accessionData['species'][1] if 'species' in accessionData and accessionData['species'][1] else "N/A")
        summary += add_detail("Modality", accessionData['modality'][1])
        summary += add_detail("License", accessionData['license'][1])
        summary += add_detail("Repository Type", accessionData['repoType'][1])

        # Calculate and add physical size and volume
        try:
            dims_str = accessionData.get('scanDimensions', '()').strip('()')
            spacing_str = accessionData.get('scanSpacing', '()').strip('()')

            if dims_str and spacing_str:
                dims = [int(d.strip()) for d in dims_str.split(',')]
                spacing = [float(s.strip()) for s in spacing_str.split(',')]

                if len(dims) == 3 and len(spacing) == 3:
                    phys_dims_mm = [d * s for d, s in zip(dims, spacing)]
                    volume_mm3 = phys_dims_mm[0] * phys_dims_mm[1] * phys_dims_mm[2]

                    # Format physical dimensions
                    size_str = f"{phys_dims_mm[0]:.2f} x {phys_dims_mm[1]:.2f} x {phys_dims_mm[2]:.2f} mm"

                    # Format volume
                    if volume_mm3 < 1000:
                        volume_str = f"{volume_mm3:.2f} mm³"
                    elif volume_mm3 < 1_000_000:
                        volume_cm3 = volume_mm3 / 1000
                        volume_str = f"{volume_cm3:.2f} cm³ (cc)"
                    else:
                        volume_l = volume_mm3 / 1_000_000
                        volume_str = f"{volume_l:.2f} L"

                    summary += "<br><b>Calculated Physical Size:</b><br>"
                    summary += add_detail("Dimensions", size_str)
                    # Format raw dimensions and spacing
                    raw_dims_str = " x ".join(map(str, dims))
                    raw_spacing_str = f"{spacing[0]:.3f} x {spacing[1]:.3f} x {spacing[2]:.3f} mm"

                    summary += "<br><b>Image Details:</b><br>"
                    summary += add_detail("Voxel Dimensions", raw_dims_str)
                    summary += add_detail("Voxel Spacing", raw_spacing_str)
                    summary += add_detail("Physical Dimensions", size_str)
                    summary += add_detail("Volume", volume_str)

        except (ValueError, IndexError) as e:
            logging.warning(f"Could not calculate physical size for summary: {e}")
            # Don't show partial or incorrect calculations

        return summary

    def onOpenRepository(self):
        # After staging, the local clone is deleted (so the multi-GB volume does not linger),
        # so nameWithOwner("origin") would be empty.  The staged repo's name is recorded in
        # stagingContext; fall back to the local remote only if a clone is present (reopen/edit).
        ctx = getattr(self.logic, "stagingContext", None) or {}
        nameWithOwner = ctx.get("personalNameWithOwner")
        if not nameWithOwner and self.logic.localRepo:
            nameWithOwner = self.logic.nameWithOwner("origin")
        if not nameWithOwner:
            slicer.util.errorDisplay(_("No staged repository to open yet."))
            return
        qt.QDesktopServices.openUrl(qt.QUrl(f"https://github.com/{nameWithOwner}"))

    def onClearForm(self):
        slicer.util.reloadScriptedModule(self.moduleName)
        self.screenshots = []
        self.updateScreenshotCount()

    def onFillFormForTesting(self):
        """Fills the accession form with default data for testing."""
        if not slicer.util.settingsValue("Developer/DeveloperMode", False, converter=slicer.util.toBool):
            return

        form = self.createUI.accessionForm
        repoName = f"test-repo-{random.randint(1000, 9999)}"
        speciesName = "Testudo exempli"

        form.questions["subjectType"].optionButtons["Biological specimen"].click()
        form.questions["specimenSource"].optionButtons["Non-accessioned"].click()
        form.questions["species"].answerText.text = speciesName
        form.questions["biologicalSex"].optionButtons["Unknown"].click()
        form.questions["developmentalStage"].optionButtons["Adult"].click()
        form.questions["modality"].optionButtons["Micro CT (or synchrotron)"].click()
        form.questions["contrastEnhancement"].optionButtons["No"].click()
        form.questions["imageContents"].optionButtons["Whole specimen"].click()
        form.questions["githubRepoName"].answerText.text = repoName
        form.questions["redistributionAcknowledgement"].optionButtons["I have the right to allow redistribution of this data."].click()
        self.createUI.shortTermRadio.checked = True  # top-level Q0 selector -> drives the hidden repoType question
        # Contact email is no longer part of the accession form; it is entered in the widget's
        # Go-live section and submitted at publish.

    def onTakeScreenshot(self):
        viewport = slicer.app.layoutManager().viewport()
        screenshot = viewport.grab()
        filePath = os.path.join(slicer.util.tempDirectory(), f"morphodepot-screenshot-{len(self.screenshots) + 1}.png")
        screenshot.save(filePath, "PNG")
        self.screenshots.append({'path': filePath, 'caption': ''})
        self.onReviewScreenshots(selectLast=True)

    def onReviewScreenshots(self, checked=False, selectLast=False):
        dialog = ScreenshotReviewDialog(self.screenshots, parent=slicer.util.mainWindow(), selectLast=selectLast)
        if dialog.exec_():
            self.screenshots = dialog.getUpdatedScreenshots()
            self.updateScreenshotCount()

    def saveScreenshotCaptions(self):
        captions = {os.path.basename(ss['path']): ss['caption'] for ss in self.screenshots}
        captionsPath = os.path.join(slicer.util.tempDirectory(), "morphodepot-screenshot-captions.json")
        with open(captionsPath, "w") as f:
            json.dump(captions, f, indent=2)
        slicer.util.showStatusMessage(f"Screenshot captions saved to {captionsPath}", 3000)

    def _waitForRepoClerkUpdate(self, statusLabel):
        """If there are open RepoClerk update-request issues, switch the label to
        'Github update pending...' and wait up to 2 minutes for them to clear.
        The label is assumed to already be visible (showing 'Updating...').
        Returns True if issues cleared (caller should reload data), False otherwise.
        Fails gracefully if the user navigates away or exits Slicer."""
        if not self.logic.hasRepoClerkUpdatePending():
            return False
        MAX_WAIT = 120
        statusLabel.text = "Github update pending..."
        try:
            for _ in range(MAX_WAIT):
                slicer.app.processEvents()
                time.sleep(1)
                if not self.logic.hasRepoClerkUpdatePending():
                    return True
            slicer.util.showStatusMessage(
                "RepoClerk journal update has been running for over 2 minutes — something may be wrong.", 10000)
            return False
        except Exception:
            return False
        finally:
            try:
                statusLabel.hide()
            except Exception:
                pass

    # Annotate
    def onRefresh(self):
        self.annotateUI.repoClerkStatusLabel.text = "Updating..."
        self.annotateUI.repoClerkStatusLabel.show()
        slicer.app.processEvents()
        with slicer.util.tryWithErrorDisplay("Failed to refresh from GitHub", waitCursor=True):
            self.annotateUI.issueList.clear()
            self.annotateUI.prList.clear()
            self.updateIssueList()
            self.updateAnnotatePRList()
        if self._waitForRepoClerkUpdate(self.annotateUI.repoClerkStatusLabel):
            with slicer.util.tryWithErrorDisplay("Failed to refresh from GitHub", waitCursor=True):
                self.annotateUI.issueList.clear()
                self.annotateUI.prList.clear()
                self.updateIssueList()
                self.updateAnnotatePRList()
        self.annotateUI.repoClerkStatusLabel.hide()

    def updateScreenshotCount(self):
        count = len(self.screenshots)
        text = f"{count} screenshot{'s' if count != 1 else ''} taken"
        self.createUI.screenshotCountLabel.text = text
        self.createUI.reviewScreenshotsButton.enabled = count > 0
        if hasattr(self.releaseUI, 'screenshotCountLabel'):
            self.releaseUI.screenshotCountLabel.text = text
            self.releaseUI.reviewScreenshotsButton.enabled = count > 0

    def onCommitMessageChanged(self, text):
        commitEnabled = (text != "")
        self.annotateUI.commitButton.enabled = commitEnabled

    def updateIssueList(self):
        slicer.util.showStatusMessage(f"Updating issues")
        self.annotateUI.issueList.clear()
        self.issuesByItem = {}
        issueList = self.logic.issueList()
        for issue in issueList:
            issueTitle = f"{issue['title']} {issue['repository']['nameWithOwner']}, #{issue['number']}"
            item = qt.QListWidgetItem(issueTitle)
            self.issuesByItem[item] = issue
            self.annotateUI.issueList.addItem(item)
        slicer.util.showStatusMessage(f"{len(issueList)} issues")

    def updateAnnotatePRList(self):
        slicer.util.showStatusMessage(f"Updating PRs")
        self.annotateUI.prList.clear()
        self.prsByItem = {}
        prList = self.logic.prList(role="segmenter")
        for pr in prList:
            prStatus = 'draft' if pr['isDraft'] else 'ready for review'
            prTitle = f"{pr['title']} {pr['issueTitles']} {pr['repository']['nameWithOwner']}: {prStatus}"
            item = qt.QListWidgetItem(prTitle)
            self.prsByItem[item] = pr
            self.annotateUI.prList.addItem(item)
        slicer.util.showStatusMessage(f"{len(prList)} prs")

    def onPRSelectionChanged(self):
        self.annotateUI.openPRPageButton.enabled = False
        self.selectedPR = None
        selectedItems = self.annotateUI.prList.selectedItems()
        if selectedItems:
            item = selectedItems[0]
            # prsByItem is shared between the Annotate and Review tabs; a stale item from the other
            # tab's still-populated list won't be present, so guard the lookup.
            self.selectedPR = self.prsByItem.get(item)
            self.annotateUI.openPRPageButton.enabled = self.selectedPR is not None

    def onOpenPRPageButtonClicked(self):
        """Open the currently selected PR in the browser."""
        if self.selectedPR:
            repoNameWithOwner = self.selectedPR["repository"]["nameWithOwner"]
            prNumber = self.selectedPR["number"]
            prURL = qt.QUrl(f"https://github.com/{repoNameWithOwner}/pull/{prNumber}")
            qt.QDesktopServices.openUrl(prURL)
        else:
            slicer.util.errorDisplay("No PR selected.")

    def onIssueDoubleClicked(self, item):
        slicer.util.showStatusMessage(f"Loading {item.text()}")
        repoDirectory = os.path.normpath(self.configureUI.repoDirectory.currentPath)
        issue = self.issuesByItem[item]
        if self.testingMode or slicer.util.confirmOkCancelDisplay("Close scene and load issue?"):
            with slicer.util.tryWithErrorDisplay("Failed to load issue", waitCursor=True):
                slicer.util.showStatusMessage(f"Loading {item.text()}")
                self.removeObservers()
                self.segmentNamesByID = {}
                self.annotateUI.currentIssueLabel.text = f"Issue: {item.text()}"
                slicer.mrmlScene.Clear()
                try:
                    self.logic.loadIssue(issue, repoDirectory)
                    self.annotateUI.forkManagementCollapsibleButton.enabled = True
                    segmentation = self.logic.segmentationNode.GetSegmentation()
                    segmentationLogic = slicer.modules.segmentations.logic()
                    for segmentID in segmentation.GetSegmentIDs():
                        segment = segmentation.GetSegment(segmentID)
                        segmentationLogic.SetSegmentStatus(segment, segmentationLogic.NotStarted)
                        self.segmentNamesByID[segmentID] = segment.GetName()
                    segmentEvents = [segmentation.SourceRepresentationModified,
                                     segmentation.SegmentModified,
                                     segmentation.SegmentAdded,
                                     segmentation.SegmentRemoved]
                    for event in segmentEvents:
                        self.addObserver(segmentation, event, self.onSegmentationModified)
                    pr = self.logic.issuePR(role="segmenter")
                    if pr:
                        self.annotateUI.reviewButton.enabled = True
                    slicer.util.showStatusMessage(f"Start segmenting {item.text()}")
                except git.exc.NoSuchPathError:
                    slicer.util.errorDisplay("Could not load issue. If it was just created on github please wait a few seconds and try again")

    def onSegmentationModified(self, segmentation, callData):
        """Called when a segment is modified, triggers an update of the commit message."""
        self.updateAutogeneratedCommitMessage()

    def updateAutogeneratedCommitMessage(self):
        """Updates the autogenerated commit title and body based on segmentation changes."""
        segmentationLogic = slicer.modules.segmentations.logic()
        segmentation = self.logic.segmentationNode.GetSegmentation()
        currentSegmentIDs = segmentation.GetSegmentIDs()

        removedSegmentNames = set()
        for segmentID,segmentName in self.segmentNamesByID.items():
            if segmentID not in currentSegmentIDs:
                removedSegmentNames.add(segmentName)

        addedSegmentNames = set()
        modifiedSegmentNames = set()
        for segmentID in currentSegmentIDs:
            segment = segmentation.GetSegment(segmentID)
            if segmentID not in self.segmentNamesByID:
                addedSegmentNames.add(segment.GetName())
            elif segmentationLogic.GetSegmentStatus(segment) != segmentationLogic.NotStarted:
                modifiedSegmentNames.add(segment.GetName())
            segmentName = segment.GetName()
            if segmentName in self.segmentNamesByID.values() and segmentName != self.segmentNamesByID[segmentID]:
                self.segmentNamesByID[segmentID] = segmentName
                modifiedSegmentNames.add(segment.GetName())


        # Update UI
        autogeneratedTitle = f"Edited {self.logic.segmentationNode.GetName()}"
        autogeneratedBody = "Edits:\n"
        if len(modifiedSegmentNames) > 0:
            autogeneratedTitle += f" - {len(modifiedSegmentNames)} modified"
            autogeneratedBody += "Modified segments:\n" + "\n".join(f"- {name}" for name in sorted(list(modifiedSegmentNames)))
        if len(addedSegmentNames) > 0:
            autogeneratedTitle += f" - {len(addedSegmentNames)} added"
            autogeneratedBody += "\nAdded segments:\n" + "\n".join(f"- {name}" for name in sorted(list(addedSegmentNames)))
        if len(removedSegmentNames) > 0:
            autogeneratedTitle += f" - {len(removedSegmentNames)} removed"
            autogeneratedBody += "\nRemoved segments:\n" + "\n".join(f"- {name}" for name in sorted(list(removedSegmentNames)))

        self.annotateUI.messageTitle.text = autogeneratedTitle
        self.annotateUI.autogeneratedCommitText.plainText = f"{autogeneratedBody.strip()}"
        slicer.util.showStatusMessage(f"MorphoDepot commit message updated.")

    def onCommit(self):
        with slicer.util.tryWithErrorDisplay("Failed to commit and push", waitCursor=True):
            slicer.util.showStatusMessage(f"Committing and pushing")
            message = self.annotateUI.messageTitle.text
            if message == "":
                #slicer.util.messageBox("You must provide a commit message (title required, body optional)")
                #return
                message = "message was empty"
            body = self.annotateUI.messageBody.plainText
            if body != "":
                message = f"{message}\n\n{body}"
            autogeneratedText = self.annotateUI.autogeneratedCommitText.plainText
            if autogeneratedText:
                message += f"\n\n{autogeneratedText}"
            if self.logic.commitAndPush(message):
                self.annotateUI.messageTitle.text = ""
                self.annotateUI.messageBody.plainText = ""
                slicer.util.showStatusMessage(f"Commit and push complete")
                self.updateAnnotatePRList()
                self.annotateUI.reviewButton.enabled = True
            else:
                path = os.path.normpath(self.configureUI.repoDirectory.currentPath)
                slicer.util.messageBox(f"Commit failed.\nYour repository conflicts with what's on github. Copy your work from {path} and then delete the local repository folder and restart the issues.")
                slicer.util.showStatusMessage(f"Commit and push failed")

    def onRequestReview(self):
        """Create a checkpoint if need, then mark issue as ready for review"""
        with slicer.util.tryWithErrorDisplay("Failed to request review", waitCursor=True):
            slicer.util.showStatusMessage(f"Marking pull request for review")
            pr = self.logic.issuePR(role="segmenter")
            if not pr:
                self.onCommit()
            prURL = self.logic.requestReview()
            self.updateAnnotatePRList()
            self.annotateUI.messageTitle.text = ""
            self.annotateUI.messageBody.plainText = ""

    def onRepoDirectoryChanged(self):
        logging.info(f"Setting repoDirectory to be {os.path.normpath(self.configureUI.repoDirectory.currentPath)}")
        self.logic.setLocalRepositoryDirectory(os.path.normpath(self.configureUI.repoDirectory.currentPath))

    def onGitPathChanged(self):
        logging.info(f"Setting gitPath to be {os.path.normpath(self.configureUI.gitPath.currentPath)}")
        qt.QSettings().setValue("MorphoDepot/gitPath", os.path.normpath(self.configureUI.gitPath.currentPath))
        self.setupLogic()
        self.enter()

    def onGhPathChanged(self):
        logging.info(f"Setting ghPath to be {os.path.normpath(self.configureUI.ghPath.currentPath)}")
        qt.QSettings().setValue("MorphoDepot/ghPath", os.path.normpath(self.configureUI.ghPath.currentPath))
        self.setupLogic()
        self.enter()

    # Review
    def onReviewRefresh(self):
        self.reviewUI.repoClerkStatusLabel.text = "Updating..."
        self.reviewUI.repoClerkStatusLabel.show()
        slicer.app.processEvents()
        with slicer.util.tryWithErrorDisplay("Failed to update PR list", waitCursor=True):
            self.updateReviewPRList()
        if self._waitForRepoClerkUpdate(self.reviewUI.repoClerkStatusLabel):
            with slicer.util.tryWithErrorDisplay("Failed to update PR list", waitCursor=True):
                self.updateReviewPRList()
        self.reviewUI.repoClerkStatusLabel.hide()

    def updateReviewPRList(self):
        with slicer.util.tryWithErrorDisplay("Failed to update PR list", waitCursor=True):
            slicer.util.showStatusMessage(f"Updating PRs")
            self.reviewUI.prList.clear()
            self.prsByItem = {}
            prList = self.logic.prList(role="reviewer")
            prCount = 0
            for pr in prList:
                if self.hidePRDrafts and pr['isDraft']:
                    continue
                prStatus = 'draft' if pr['isDraft'] else 'ready for review'
                prTitle = f"{pr['title']} {pr['issueTitles']} {pr['repository']['nameWithOwner']}: {prStatus}"
                item = qt.QListWidgetItem(prTitle)
                prCount += 1
                self.prsByItem[item] = pr
                self.reviewUI.prList.addItem(item)
            slicer.util.showStatusMessage(f"{len(prList)} prs")

    def onPRDoubleClicked(self, item):
        repoDirectory = self.logic.localRepositoryDirectory()
        pr = self.prsByItem.get(item)  # shared dict; ignore a stale item from the other tab
        if pr is None:
            return
        if self.testingMode or slicer.util.confirmOkCancelDisplay("Close scene and load PR?"):
            with slicer.util.tryWithErrorDisplay("Failed to load PR", waitCursor=True):
                slicer.util.showStatusMessage(f"Loading {item.text()}")
                self.reviewUI.currentPRLabel.text = f"PR: {item.text()}"
                slicer.mrmlScene.Clear()
                if self.logic.loadPR(pr, repoDirectory):
                    self.reviewUI.prCollapsibleButton.enabled = True
                    slicer.util.showStatusMessage(f"Start reviewing {item.text()}")
                else:
                    slicer.util.showStatusMessage(f"PR load failed")

    def onHideDraftsChanged(self, state):
        self.hidePRDrafts = (state == qt.Qt.Checked)
        self.updateReviewPRList()

    def onRequestChanges(self):
        with slicer.util.tryWithErrorDisplay("Failed to request changes", waitCursor=True):
            slicer.util.showStatusMessage(f"Requesting changes")
            message = self.reviewUI.reviewMessage.plainText
            self.logic.requestChanges(message)
            self.reviewUI.reviewMessage.plainText = ""
            slicer.util.showStatusMessage(f"Changes requested")
            self.updateReviewPRList()

    def onApprove(self):
        with slicer.util.tryWithErrorDisplay("Failed to approve PR", waitCursor=True):
            slicer.util.showStatusMessage(f"Approving")
            prURL = self.logic.approvePR()
            self.reviewUI.reviewMessage.plainText = ""
            self.updateReviewPRList()

    # Release
    def onRefreshReleaseTab(self):
        with slicer.util.tryWithErrorDisplay("Failed to refresh repositories", waitCursor=True):
            slicer.util.showStatusMessage("Fetching owned repositories...")
            self.releaseUI.repoList.clear()
            self.releaseUI.makeReleaseButton.enabled = False
            self.releaseUI.releasesCollapsibleButton.enabled = False
            self.releaseUI.announcementCollapsibleButton.enabled = False
            self.releaseUI.announcementCounts.text = "Targets: (load a repository)"
            self.releaseUI.currentRepoLabel.text = "No repository loaded"
            self.releaseUI.currentVersionLabel.text = "Current version: None"
            self.releaseUI.sourceVolumeLabel.text = ""
            self.releaseUI.openReleasePageButton.enabled = False
            self.reposByItem = {}
            administratedRepos = self.logic.administratedRepoList()
            for repo in administratedRepos:
                issues = repo.get('issues', {}).get('totalCount', 0)
                prs = repo.get('pullRequests', {}).get('totalCount', 0)
                issueLabel = "issue" if issues == 1 else "issues"
                prLabel = "PR" if prs == 1 else "PRs"
                label = f"{repo['nameWithOwner']}  ({issues} open {issueLabel}, {prs} open {prLabel})"
                item = qt.QListWidgetItem(label)
                tooltip = self.repoTooltip(repo)
                if tooltip:
                    item.setToolTip(tooltip)
                self.reposByItem[item] = repo
                self.releaseUI.repoList.addItem(item)
            slicer.util.showStatusMessage(f"Found {len(administratedRepos)} owned repositories.")

    def onReleaseRepoDoubleClicked(self, item):
        repoData = self.reposByItem[item]
        slicer.util.showStatusMessage(f"Loading repository {repoData['nameWithOwner']}...")
        if self.testingMode or slicer.util.confirmOkCancelDisplay("Close scene and load repository?"):
            slicer.mrmlScene.Clear()
            with slicer.util.tryWithErrorDisplay("Failed to load repository", waitCursor=True):
                if self.logic.loadRepoForRelease(repoData):
                    self.releaseUI.currentRepoLabel.text = f"Loaded: {repoData['nameWithOwner']}"
                    self.releaseUI.sourceVolumeLabel.text = getattr(self.logic, 'sourceVolumeName', '') or ""
                    self.releaseUI.releasesCollapsibleButton.enabled = True
                    self.releaseUI.announcementCollapsibleButton.enabled = True
                    # Auto-select the color table (safe to default), but DELIBERATELY do NOT
                    # auto-populate the baseline picker: defaulting it to the repo's existing
                    # baseline made it too easy to release without merging in the new
                    # contribution, silently re-publishing the old baseline (see issue #123).
                    # Force a conscious selection of the new baseline instead.
                    self.releaseUI.newBaselineSelector.setCurrentNode(None)
                    self.releaseUI.newColorSelector.setCurrentNode(getattr(self.logic, 'colorTableNode', None))
                    self.updateAnnouncementCounts(repoData)
                    self.updateAnnouncementState(repoData['nameWithOwner'])
                    self.updateReleaseNotesTemplate(repoData)
                    self.updateCurrentVersionLabel()
                    self.updateMakeReleaseEnabled()
                    slicer.util.showStatusMessage(f"Repository {repoData['nameWithOwner']} loaded.")

    def updateMakeReleaseEnabled(self):
        """The Make Release button requires a loaded repository plus both a baseline
        segmentation and a color table to be selected."""
        hasRepo = self.logic is not None and self.logic.localRepo is not None
        hasSegmentation = self.releaseUI.newBaselineSelector.currentNode() is not None
        hasColorTable = self.releaseUI.newColorSelector.currentNode() is not None
        self.releaseUI.makeReleaseButton.enabled = bool(hasRepo and hasSegmentation and hasColorTable)

    def _resetReleaseInputs(self):
        """After a release is submitted/created, clear the New-release inputs and (via the selector
        change) disable Make Release, so the finished action can't be fired again by mistake.  The
        loaded-repository context (Current release line) is left as-is."""
        self.releaseUI.newBaselineSelector.setCurrentNode(None)
        self.releaseUI.newColorSelector.setCurrentNode(None)
        self.releaseUI.releaseCommentsEdit.plainText = ""
        self.screenshots = []
        self.updateScreenshotCount()
        self.updateMakeReleaseEnabled()  # no segmentation/color selected now -> button disabled

    def updateAnnouncementCounts(self, repoData):
        issues = repoData.get('issues', {}).get('totalCount', 0)
        prs = repoData.get('pullRequests', {}).get('totalCount', 0)
        self.releaseUI.announcementCounts.text = f"Will post to {issues} open issues and {prs} open PRs."

    def updateAnnouncementState(self, nameWithOwner):
        """Show whether a pre-release announcement already exists for the loaded repo, so the
        user sees it on load without clicking anything. Reads live repo state. There is at most
        one (the dedup guard prevents more); 'Notify contributors' becomes 'Replace announcement'
        when one exists, and that edits the existing announcement in place."""
        announcements = self.logic.listReleaseAnnouncements(nameWithOwner)
        if announcements is None:
            return  # the check itself failed (transient gh error) — leave the indicator unchanged
        header = self.releaseUI.announcementCollapsibleButton
        label = self.releaseUI.announcementStateLabel
        button = self.releaseUI.announceButton
        if not announcements:
            header.text = "Pre-release Announcement"
            label.text = ""
            label.setStyleSheet("")
            button.text = "Notify contributors"
            return
        deadline = announcements[0].get("deadline") or "unknown"
        header.text = f"Pre-release Announcement — already announced (deadline {deadline})"
        label.text = (f"There is already a published release announcement (deadline {deadline}). "
                      "To modify it, edit the fields below and click “Replace announcement”.")
        label.setStyleSheet("color: #1a4d7a;")  # informational, not a warning
        button.text = "Replace announcement"
        header.collapsed = False  # auto-expand so it is noticed

    def repoTooltip(self, repo):
        """Build a multiline tooltip listing open issues and PRs for a repo."""
        lines = []
        issueNodes = repo.get('issues', {}).get('nodes', []) or []
        if issueNodes:
            lines.append("Open issues:")
            for issue in issueNodes:
                authorNode = issue.get('author') or {}
                author = authorNode.get('login', '?')
                assignees = ", ".join(
                    f"@{a['login']}" for a in (issue.get('assignees', {}) or {}).get('nodes', []) or []
                )
                line = f"  #{issue['number']}: {issue['title']} (by @{author}"
                if assignees:
                    line += f", assigned: {assignees}"
                line += ")"
                lines.append(line)
        prNodes = repo.get('pullRequests', {}).get('nodes', []) or []
        if prNodes:
            if lines:
                lines.append("")
            lines.append("Open PRs:")
            for pr in prNodes:
                authorNode = pr.get('author') or {}
                author = authorNode.get('login', '?')
                draft = " [draft]" if pr.get('isDraft') else ""
                lines.append(f"  #{pr['number']}: {pr['title']} (by @{author}){draft}")
        return "\n".join(lines)

    def updateReleaseNotesTemplate(self, repoData):
        """Prefill the release-notes editor with two blank lines (for owner prose) and an
        autogenerated change log of issues closed since the last release."""
        nameWithOwner = repoData['nameWithOwner']
        try:
            closedIssues = self.logic.closedIssuesSinceLastRelease(nameWithOwner)
        except Exception as e:
            logging.warning(f"Could not fetch closed-issue change log: {e}")
            closedIssues = []
        lines = ["", ""]  # two blank lines for the owner's prose
        if closedIssues:
            lines.append("## Changes in this release")
            for issue in closedIssues:
                lines.append(f"- #{issue['number']}: {issue['title']}")
        self.releaseUI.releaseCommentsEdit.plainText = "\n".join(lines)

    def updateCurrentVersionLabel(self):
        """Gets releases and updates the version label and open page button."""
        self.releaseUI.openReleasePageButton.enabled = False
        releases = self.logic.getReleases()
        if releases:
            latestRelease = releases[0] # gh cli returns latest first
            self.releaseUI.currentVersionLabel.text = f"Current version: {latestRelease['tagName']}"
            self.releaseUI.openReleasePageButton.enabled = True
        else:
            self.releaseUI.currentVersionLabel.text = "Current version: None"

    def onOpenReleasePage(self):
        """Opens the GitHub releases page for the current repository."""
        if self.logic.localRepo:
            nameWithOwner = self.logic.nameWithOwner("origin")
            releasesURL = qt.QUrl(f"https://github.com/{nameWithOwner}/releases")
            qt.QDesktopServices.openUrl(releasesURL)

    # Search
    def onRefreshSearch(self):
        self.searchUI.repoClerkStatusLabel.text = "Updating..."
        self.searchUI.repoClerkStatusLabel.show()
        slicer.app.processEvents()
        with slicer.util.tryWithErrorDisplay("Failed to refresh search cache", waitCursor=True):
            slicer.util.showStatusMessage("Refreshing search cache...")
            self.logic.refreshSearchCache()
            self.searchUI.searchForm.searchBox.setPlaceholderText("Search...")
            self.searchUI.searchForm.topWidget.enabled = True
            self.searchUI.searchCollapsibleButton.collapsed = False
            self.doSearch()
        if self._waitForRepoClerkUpdate(self.searchUI.repoClerkStatusLabel):
            with slicer.util.tryWithErrorDisplay("Failed to refresh search cache", waitCursor=True):
                slicer.util.showStatusMessage("Reloading search cache with updated journals...")
                self.logic.refreshSearchCache()
                self.doSearch()
        self.searchUI.repoClerkStatusLabel.hide()

    def doSearch(self):
        criteria = self.searchUI.searchForm.criteria()
        results = self.logic.search(criteria)
        self.updateSearchResults(results)

    def repoDataKetToRepoNameAndOwner(self, repoDataKey):
        nameWithOwnerSplit = repoDataKey.split('^')
        owner = nameWithOwnerSplit[0]
        repoName = nameWithOwnerSplit[1]
        return repoName,owner

    def updateSearchResults(self, results):
        slicer.util.showStatusMessage(f"Updating search results")
        self.searchUI.resultsModel.clear()
        self.searchUI.saveSearchResultsButton.enabled = False
        self.searchResultsByItem = {}
        headers = ["Size (GB)", "Repository", "Owner", "Species", "Modality", "Active", "Spacing", "Dimensions"]
        self.searchUI.resultsModel.setHorizontalHeaderLabels(headers)
        for repoDataKey, repoData in results.items():
            repoName,owner = self.repoDataKetToRepoNameAndOwner(repoDataKey)
            species = repoData.get('species', [None, "N/A"])[1]
            modality = repoData.get('modality', [None, "N/A"])[1]

            activeText = "N/A"
            pushedAtStr = repoData.get('pushedAt')
            if pushedAtStr:
                try:
                    pushedAtDate = datetime.date.fromisoformat(pushedAtStr.split("T")[0])
                    today = datetime.date.today()
                    delta = today - pushedAtDate
                    days = delta.days
                    if days < 1:
                        activeText = "Today"
                    elif days < 30:
                        activeText = f"{days} day{'s' if days > 1 else ''} ago"
                    elif days < 365:
                        months = days // 30
                        activeText = f"{months} month{'s' if months > 1 else ''} ago"
                    else:
                        years = days // 365
                        activeText = f"{years} year{'s' if years > 1 else ''} ago"
                except ValueError:
                    activeText = "Invalid Date"

            sizeText = "N/A"
            volumeSize = repoData.get('volumeSize')
            if volumeSize is not None:
                sizeInGB = volumeSize / (1024**3)
                sizeText = f"{sizeInGB:.2f}"

            spacingText = "N/A"
            spacingStr = repoData.get('scanSpacing')
            if spacingStr:
                try:
                    # The string is a tuple representation like "(0.5, 0.5, 0.9)"
                    spacingValues = [float(v) for v in spacingStr.strip("()").split(',')]
                    formattedValues = [f"{v:.3g}" for v in spacingValues]
                    spacingText = ", ".join(formattedValues)
                except (ValueError, IndexError, TypeError):
                    spacingText = "Invalid"

            dimensionsText = "N/A"
            dimensionsStr = repoData.get('scanDimensions')
            if dimensionsStr:
                try:
                    # The string is a tuple representation like "(512, 512, 300)"
                    dims = dimensionsStr.strip("()").split(',')
                    dimensionsText = " x ".join([d.strip() for d in dims])
                except:
                    dimensionsText = "Invalid"

            repoItem = qt.QStandardItem(repoName)
            ownerItem = qt.QStandardItem(owner)
            speciesItem = qt.QStandardItem(species)
            modalityItem = qt.QStandardItem(modality)
            sizeItem = qt.QStandardItem(sizeText)
            activeItem = qt.QStandardItem(activeText)
            spacingItem = qt.QStandardItem(spacingText)
            dimensionsItem = qt.QStandardItem(dimensionsText)

            # Store the full data in the first item of the row
            sizeItem.setData(repoData, qt.Qt.UserRole)
            sizeItem.setData(repoDataKey, qt.Qt.UserRole + 1)
            rowItems = [sizeItem, repoItem, ownerItem, speciesItem, modalityItem, activeItem, spacingItem, dimensionsItem]

            # Create a rich HTML tooltip
            tooltipParts = [f"<b>{repoName}</b> by <b>{owner}</b><br><hr>"]
            tooltipParts.append("<table>")
            tooltipParts.append(f"<tr><td><b>Last Active:</b></td><td>{activeText}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Species:</b></td><td>{species}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Size (GB):</b></td><td>{sizeText}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Modality:</b></td><td>{modality}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Spacing:</b></td><td>{spacingText}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Dimensions:</b></td><td>{dimensionsText}</td></tr>")
            tooltipParts.append("</table>")
            screenshotCount = repoData.get('screenshotCount', 0)

            if screenshotCount > 0 and 'screenshotCaptions' in repoData:
                tooltipParts.append("<hr><b>Screenshots:</b><br>")
                screenshotCacheDir = os.path.join(self.logic.localRepositoryDirectory(), "MorphoDepotCaches", "Screenshots")
                screenshotCaptions = repoData.get('screenshotCaptions') or {}  # `or {}`: key may be null
                if isinstance(screenshotCaptions, list):
                    # Some journals store captions as a list; .items() would crash. Normalize to
                    # {filename: caption} best-effort (defensive — such repos currently carry 0
                    # screenshots, so this branch isn't actually reached today).
                    normalized = {}
                    for n, entry in enumerate(screenshotCaptions, 1):
                        if isinstance(entry, dict):
                            key = entry.get("name") or entry.get("filename") or f"screenshot-{n}.png"
                            normalized[key] = entry.get("caption", "")
                        else:
                            normalized[f"screenshot-{n}.png"] = entry
                    screenshotCaptions = normalized
                # Limit to 5 thumbnails to avoid overly large tooltips
                for i, (filename, caption) in enumerate(screenshotCaptions.items()):
                    if i >= 5:
                        tooltipParts.append(f"<i>...and {screenshotCount - 5} more.</i>")
                        break

                    urlPrefix = "https://raw.githubusercontent.com"
                    imageURL = f"{urlPrefix}/{owner}/{repoName}/main/screenshots/{filename}"
                    localImagePath = os.path.join(screenshotCacheDir, owner, repoName, filename)

                    if not os.path.exists(localImagePath):
                        try:
                            os.makedirs(os.path.dirname(localImagePath), exist_ok=True)
                            slicer.util.downloadFile(imageURL, localImagePath)
                        except Exception as e:
                            logging.warning(f"Could not download screenshot {imageURL}: {e}")

                    if os.path.exists(localImagePath):
                        tooltipParts.append(f'<img src="file:///{localImagePath}" width="128"> ')

            tooltipText = "".join(tooltipParts)

            # Set the same tooltip for all items in the row
            for item in rowItems:
                item.setToolTip(tooltipText)

            self.searchUI.resultsModel.appendRow(rowItems)

        self.searchUI.resultsTable.resizeColumnsToContents()
        self.searchUI.saveSearchResultsButton.enabled = len(results) > 0
        slicer.util.showStatusMessage(f"{len(results.keys())} matching repositories")

    def onSearchResultsContextMenu(self, point):
        index = self.searchUI.resultsTable.indexAt(point)
        if not index.isValid():
            return

        item = self.searchUI.resultsModel.item(index.row(), 0) # data is in column 0
        repoData = item.data(qt.Qt.UserRole)
        repoDataKey = item.data(qt.Qt.UserRole + 1)
        repoName, owner = self.repoDataKetToRepoNameAndOwner(repoDataKey)
        fullRepoName = f"{owner}/{repoName}"

        menu = qt.QMenu()
        openRepoAction = menu.addAction("Open Repository Page")
        previewAction = menu.addAction("Preview in Slicer")

        action = menu.exec_(self.searchUI.resultsTable.mapToGlobal(point))

        if action == openRepoAction:
            qt.QDesktopServices.openUrl(qt.QUrl(f"https://github.com/{fullRepoName}"))
        elif action == previewAction:
            self.previewRepository(fullRepoName)

    def onSearchResultsDoubleClicked(self, index):
        """Handle double-click on search results table to preview repository."""
        if not index.isValid():
            return

        item = self.searchUI.resultsModel.item(index.row(), 0) # data is in column 0
        repoDataKey = item.data(qt.Qt.UserRole + 1)
        repoName, owner = self.repoDataKetToRepoNameAndOwner(repoDataKey)
        fullRepoName = f"{owner}/{repoName}"
        self.previewRepository(fullRepoName)

    def onSaveSearchResults(self):
        """Saves the current content of the search results table to a CSV file."""
        fileName = qt.QFileDialog.getSaveFileName(self.parent, "Save Search Results", "MorphoDepotSearchResult.csv", "CSV Files (*.csv)")
        if not fileName:
            return

        try:
            with open(fileName, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)

                # Write headers
                model = self.searchUI.resultsModel
                headers = []
                for column in range(model.columnCount()):
                    headers.append(model.horizontalHeaderItem(column).text())
                writer.writerow(headers)

                # Write data rows
                for row in range(model.rowCount()):
                    rowData = []
                    for column in range(model.columnCount()):
                        item = model.item(row, column)
                        rowData.append(item.text() if item else "")
                    writer.writerow(rowData)
            slicer.util.showStatusMessage(f"Search results saved to {fileName}", 3000)
        except Exception as e:
            slicer.util.errorDisplay(f"Could not save search results to {fileName}: {e}")

    def onMakeRelease(self):
        if not self.logic.localRepo:
            return
        baselineNode = self.releaseUI.newBaselineSelector.currentNode()
        colorTableNode = self.releaseUI.newColorSelector.currentNode()
        if baselineNode is None or colorTableNode is None:
            return

        nameWithOwner = self.logic.nameWithOwner("origin")
        newTag = self.logic.nextReleaseTag()
        plan = self.logic.releaseSnapshotPlan(newTag, baselineNode, colorTableNode, self.screenshots)
        if plan is None:
            return

        # #123: guard against a release that carries no new segmentation work.  Compare CONTENT
        # directly via _baselineMatchesCommittedFile (segment count + compressed file size).  Do NOT
        # trust Slicer's GetModifiedSinceRead() as an "unchanged" shortcut: it does not reliably
        # register copy/merge-segment edits, so it would short-circuit to "unchanged" and skip the
        # comparison -- which wrongly reported a baseline with many added segments as no new work.
        committedPath = os.path.join(self.logic.localRepo.working_dir, "baseline.seg.nrrd")
        baselineUnchanged = self._baselineMatchesCommittedFile(baselineNode, committedPath)
        if baselineUnchanged:
            if not (self.testingMode or slicer.util.confirmOkCancelDisplay(
                    "The selected baseline is identical to the repository's current baseline, so "
                    "this release would incorporate no new segmentation work.\n\n"
                    "If you meant to publish merged contributions, click Cancel and select (or "
                    "build) the updated segmentation as the new baseline first.\n\n"
                    "Release with the unchanged baseline anyway?",
                    windowTitle="Baseline unchanged")):
                return

        # Color-table no-change feedback -- ONLY when the curator picked a DIFFERENT color node than
        # the one loaded from the repo (so a change was intended) that turns out byte-identical: the
        # case where a "fix" silently didn't take.  Releasing with the existing (loaded) color table
        # unchanged is normal and must NOT warn -- unlike the baseline, the color table need not change.
        loadedColor = getattr(self.logic, 'colorTableNode', None)
        colorIsLoadedNode = (loadedColor is not None and colorTableNode.GetID() == loadedColor.GetID())
        if not colorIsLoadedNode and self._colorTableMatchesCommitted(colorTableNode):
            if not (self.testingMode or slicer.util.confirmOkCancelDisplay(
                    f"The selected color table '{colorTableNode.GetName()}' is byte-identical to the "
                    "repository's committed color table, so the release will NOT change it.\n\n"
                    "If you meant to apply a color or terminology fix, click Cancel, correct the "
                    "color table, and re-select it.\n\n"
                    "Release with the unchanged color table anyway?",
                    windowTitle="Color table unchanged")):
                return

        # #124: non-blocking reminder if no pre-release announcement was made (or its deadline
        # has not passed). Never enforces — every path can proceed.
        if not self._confirmReleaseAnnouncement(nameWithOwner):
            return

        # Contributor credit (archival/org repos only): curate CONTRIBUTORS.json and stage it into the
        # working tree so prepareReleaseSnapshot's `git add --all` commits it in the release commit
        # (the shared-file invariant — all shared files change only at release; org-design Sec.9.6).
        if self._isArchivalRepo(nameWithOwner):
            if not self._curateContributorsForRelease(nameWithOwner):
                return

        prompt = self.buildReleaseConfirmation(plan, baselineNode, colorTableNode)
        if not (self.testingMode or slicer.util.confirmOkCancelDisplay(prompt, windowTitle=f"Make release {newTag}")):
            return

        slicer.util.showStatusMessage(f"Creating release {newTag}...")
        releaseNotes = self.releaseUI.releaseCommentsEdit.plainText
        createdTag = None
        try:
            createdTag = self.logic.createRelease(
                releaseNotes,
                baselineSegmentationNode=baselineNode,
                colorTableNode=colorTableNode,
                screenshots=self.screenshots,
            )
        except Exception as e:
            self.handleReleaseFailure(e)
            return

        # Archival/org release is routed through the App's review gate (org-design Sec.11.3): the App
        # emailed an Approve link and has NOT cut the tag or minted the DOI yet, so the release is not
        # live.  Report that review was requested and skip the post-release cleanup (announcement
        # retire, item close) — those belong to an actually-created release.
        if isinstance(createdTag, dict) and createdTag.get("pending"):
            to = createdTag.get("reviewSentTo") or "the MorphoDepot reviewers"
            tag = createdTag.get("tag")
            self.logic.discardReleaseBackup()
            self.updateCurrentVersionLabel()
            # Retire the pre-release announcement now (its contributions are gathered and the release
            # is submitted) -- best-effort/idempotent.
            try:
                self.logic.clearReleaseAnnouncement(nameWithOwner)
                self.updateAnnouncementState(nameWithOwner)
            except Exception as e:
                logging.warning(f"Could not retire the pre-release announcement: {e}")
            slicer.util.showStatusMessage("Release submitted for review.")
            if not self.testingMode:
                slicer.util.infoDisplay(
                    f"Release {tag} has been submitted for review.\n\n"
                    f"A request was emailed to {to}. Once a reviewer approves, the release is cut and "
                    "its DOI is minted automatically; if changes are requested you'll get an email "
                    "with the details. The release commit is already pushed to the repository.",
                    windowTitle="Release submitted for review")
            # The contributions are merged into the release commit, so offer to close their open
            # issues/PRs now -- the curator's token can close both (the App's token can't close PRs),
            # and they should not linger waiting on approval.
            self.maybeCloseOpenItemsForRelease(nameWithOwner, tag, created=False)
            # Finished: clear the New-release inputs and disable Make Release so it can't re-fire.
            self._resetReleaseInputs()
            return

        self.logic.discardReleaseBackup()
        self.releaseUI.releaseCommentsEdit.plainText = ""
        self.updateCurrentVersionLabel()
        if createdTag:
            # #124: retire the pre-release announcement (unpin, unlabel, close) now that the
            # release exists — done unconditionally, independent of the optional item-close step
            # below, so the next cycle's announcement detection starts clean.
            self.logic.clearReleaseAnnouncement(nameWithOwner, createdTag)
            self.updateAnnouncementState(nameWithOwner)  # announcement retired -> clear the indicator
            self.maybeCloseOpenItemsForRelease(nameWithOwner, createdTag)
            # Reset the in-session screenshots so the next release starts clean.
            self.screenshots = []
            self.updateScreenshotCount()
        slicer.util.showStatusMessage("New release created. You can add more comments on the GitHub release page.")

    def _isArchivalRepo(self, nameWithOwner):
        """True if the repo is owned by an organization (archival). Release credit/DOI are
        archival-only (org-design Sec.9.6); personal short-term repos get no contributor record.
        On a transient lookup failure (or empty response) default to True and curate: the Release
        tab only ever lists archival/org repos, so 'unknown' must not silently drop the credit
        record.  A definitive User owner still returns False."""
        try:
            info = self.logic.ghJSON(["api", f"/repos/{nameWithOwner}"])
        except Exception as e:
            logging.warning(f"Could not determine owner type of {nameWithOwner}; assuming archival: {e}")
            return True
        if not info:
            logging.warning(f"Empty owner-type lookup for {nameWithOwner}; assuming archival.")
            return True
        return (info.get("owner") or {}).get("type") == "Organization"

    def _curateContributorsForRelease(self, nameWithOwner):
        """Show the contributor-credit grid for this release and stage CONTRIBUTORS.json into the
        working tree (so prepareReleaseSnapshot commits it in the release). Returns False to abort.

        Blocks only when a cited author has no real name; contributors with no name are credited by
        handle. See org-design Sec.9.6 / 9.7.
        """
        import MorphoDepotContributors as MDC
        repoDir = self.logic.localRepo.working_dir
        contribPath = os.path.join(repoDir, "CONTRIBUTORS.json")
        try:
            data = MDC.load(contribPath) if os.path.exists(contribPath) else MDC.new_record(nameWithOwner)
        except (ValueError, json.JSONDecodeError):
            logging.warning("CONTRIBUTORS.json is not valid JSON; starting from a fresh record")
            data = MDC.new_record(nameWithOwner)

        # Ensure the curator (CURATOR file, else the signed-in user) is a cited author.
        curator = None
        curatorPath = os.path.join(repoDir, "CURATOR")
        if os.path.exists(curatorPath):
            with open(curatorPath) as f:
                curator = f.read().strip() or None
        curator = curator or self.logic.whoami()
        MDC.ensure_person(data, github=curator, author=True, source="member")["curator"] = True

        tag = self.logic.nextReleaseTag() or ""
        # Gather segmentation contributions (merged issue-N PRs) FIRST so the member-resolution pass
        # below sees every contributor (not just those already in CONTRIBUTORS.json).  Best-effort —
        # a gh failure here must never block the release.
        try:
            self._gatherMergedContributions(nameWithOwner, data, tag)
        except Exception as e:
            logging.warning(f"Could not gather merged-PR contributions: {e}")
        # Pre-fill every contributor's identity AFTER gathering: members (the curator AND merged-PR
        # authors like @muratmaga) are resolved to name/ORCID/affiliation; true outsiders stay
        # handle-only.  Resolved via the App so it works even when the curator can't read the
        # owners-only onboarding records.
        self._enrichMembersViaApi(data)
        panel = MDC.make_contributor_panel(data, title=f"Release {tag} - confirm contributors")
        dialog = qt.QDialog(slicer.util.mainWindow())
        dialog.setWindowTitle("Contributors & credit")
        dialog.setMinimumSize(640, 480)
        dialogLayout = qt.QVBoxLayout(dialog)
        dialogLayout.addWidget(panel.widget)
        # addButton(text, role) — PythonQt does NOT honor the QDialogButtonBox(Ok | Cancel) flags
        # constructor (it yields a button-less box).
        buttonBox = qt.QDialogButtonBox()
        buttonBox.addButton("Confirm", qt.QDialogButtonBox.AcceptRole)
        buttonBox.addButton("Cancel", qt.QDialogButtonBox.RejectRole)
        buttonBox.accepted.connect(dialog.accept)
        buttonBox.rejected.connect(dialog.reject)
        dialogLayout.addWidget(buttonBox)

        while True:
            if not dialog.exec_():
                return False  # curator cancelled -> abort the release
            panel.syncFromTable()
            if panel.isReady():
                break
            slicer.util.warningDisplay(
                "Each cited author needs a real name (Family, Given). Fill the missing name(s), "
                "or untick Author for those rows.", windowTitle="Author name required")
        panel.saveTo(contribPath)
        return True

    def _gatherMergedContributions(self, nameWithOwner, data, releaseTag):
        """Collect merged issue-N segmentation PRs into `data` (people + contributions), mirroring the
        release-time sync_contributors Action so the Release dialog shows live credit even when that
        Action is not installed.  issue-N branch convention; MDC.add_contribution dedups.

        Only PRs merged *since the last release* are stamped with `releaseTag` — they genuinely belong
        to this release.  Older PRs (first seen now because the gather never ran before) are recorded
        with `release=None` rather than being mis-attributed to the current tag."""
        import re
        import MorphoDepotContributors as MDC
        issueBranch = re.compile(r"^issue-(\d+)$")
        releases = self.logic.ghJSON(
            ["release", "list", "--repo", nameWithOwner, "--json", "publishedAt"]) or []
        since = max((r.get("publishedAt") or "" for r in releases), default="")
        prs = self.logic.ghJSON([
            "pr", "list", "--repo", nameWithOwner, "--state", "merged", "--base", "main",
            "--limit", "500", "--json", "number,headRefName,author,mergedAt"]) or []
        for pr in prs:
            match = issueBranch.match(pr.get("headRefName", "") or "")
            if not match:
                continue  # not a segmentation PR (issue-N branch convention)
            author = pr.get("author") or {}
            login = author.get("login")
            if not login:
                continue
            mergedAt = pr.get("mergedAt") or ""
            stampRelease = releaseTag if (not since or mergedAt > since) else None
            MDC.ensure_person(data, github=login, github_id=author.get("id"), source="non-member")
            MDC.add_contribution(data, issue=int(match.group(1)), by=login, release=stampRelease)

    def buildReleaseConfirmation(self, plan, baselineNode, colorTableNode):
        """Compose the OK/Cancel summary describing every action that will run during release."""
        lines = []
        lines.append(f"Make release {plan['newTag']} for the loaded repository.")
        lines.append("")
        lines.append("If you click OK, the following will happen on main:")
        lines.append(
            f"• A pre-release-{plan['newTag']} archive branch will be created and pushed to GitHub, "
            f"capturing the pre-release state of main (including all per-issue segmentations) "
            f"so it stays browsable on the remote."
        )
        lines.append(f"• The selected segmentation '{baselineNode.GetName()}' will be saved as baseline.seg.nrrd (replacing any existing one).")
        lines.append(f"• The selected color table '{colorTableNode.GetName()}' will be saved (replacing any existing one).")
        if plan['archivedReadme']:
            lines.append(
                f"• README.md will be moved to {plan['archivedReadme']} and a new README.md will be generated for {plan['newTag']}."
            )
            lines.append(
                f"  ⚠ The new README.md is regenerated from MorphoDepotAccession.json — any manual edits in the current README.md "
                f"will be preserved in {plan['archivedReadme']} but NOT carried into the new README.md. "
                f"After the release, copy any sections you want to keep into the new README.md by hand."
            )
        else:
            lines.append(f"• A new README.md will be generated for {plan['newTag']}.")
        if plan['newScreenshotNames']:
            lines.append(
                f"• {len(plan['newScreenshotNames'])} new screenshot(s) will be added: "
                f"{', '.join(plan['newScreenshotNames'])}."
            )
        else:
            lines.append("• No new screenshots will be added.")
        if plan['issueSegFiles']:
            lines.append(
                f"• {len(plan['issueSegFiles'])} per-issue segmentation file(s) will be removed from the working tree "
                f"(preserved in the pre-release-{plan['newTag']} branch and in git history): {', '.join(plan['issueSegFiles'])}."
            )
        lines.append(
            f"• These changes will be committed and pushed to origin/main, then the {plan['newTag']} "
            f"release tag will be created at the same commit."
        )
        lines.append("")
        lines.append("No data is lost: prior versions of every changed file remain in git history, and clicking Cancel makes no changes at all.")
        lines.append("If anything fails partway, you will be offered the chance to reset the local repo to its pre-release state or to keep the partial changes for manual inspection.")
        return "\n".join(lines)

    def handleReleaseFailure(self, error):
        """Offer reset-to-backup or leave-for-debug after a release failure."""
        msg = (
            f"Release creation failed:\n{error}\n\n"
            "Click OK to reset the local repository to its pre-release state.\n"
            "Click Cancel to leave the working tree as is so you can salvage screenshots or other work.\n\n"
            "Note: a local reset cannot undo a push that may have already reached GitHub. For a personal "
            "repo that is origin/main; for an org/archival repo the push went to a "
            "'release-candidate-<version>' branch and main is untouched until approval. Either is safe "
            "to leave — a retry force-pushes the candidate again — but you may delete a stray "
            "release-candidate branch on GitHub if you prefer."
        )
        if (not self.testingMode) and slicer.util.confirmOkCancelDisplay(msg, windowTitle="Release failed"):
            try:
                self.logic.resetToReleaseBackup()
                slicer.util.showStatusMessage("Local repository reset to pre-release state.")
            except Exception as resetError:
                slicer.util.errorDisplay(f"Reset failed: {resetError}")
        else:
            slicer.util.showStatusMessage(
                f"Release left mid-flight. Backup branch: {getattr(self.logic, 'releaseBackupBranch', None)}"
            )

    def maybeCloseOpenItemsForRelease(self, nameWithOwner, version, created=True):
        """After a release (or its submission for review), offer to close all remaining open issues
        and PRs.  `created=False` for a gated release that has been *submitted* but not yet cut."""
        with slicer.util.tryWithErrorDisplay("Failed to query open items", waitCursor=True):
            issues, prs = self.logic.openIssuesAndPRs(nameWithOwner)
        if not issues and not prs:
            return
        state = "created" if created else "submitted for review"
        prompt = (
            f"Release {version} {state}.\n\n"
            f"Close {len(issues)} open issues and {len(prs)} open PRs as part of the release?\n"
            f"(Open PRs will be closed without merging — contributors must rebase onto the new baseline.)"
        )
        if not (self.testingMode or slicer.util.confirmOkCancelDisplay(prompt)):
            return
        with slicer.util.tryWithErrorDisplay("Failed to close open items", waitCursor=True):
            ni, np = self.logic.closeOpenItemsForRelease(nameWithOwner, version)
            slicer.util.showStatusMessage(f"Closed {ni} issues and {np} PRs for release {version}.")

    def _confirmReleaseAnnouncement(self, nameWithOwner):
        """Return True to proceed with the release, False to abort.

        Non-blocking reminder (#124): if collaborators have open items that a release would
        close but no pre-release announcement was made, prompt the user.  If an announcement
        exists but its deadline has not passed, prompt more softly.  Detection reads repo state
        only (the pinned, `release-pending`-labelled announcement issue) — no local flag.  Any
        detection failure proceeds silently so it can never block a release."""
        if self.testingMode:
            return True
        try:
            issues, prs = self.logic.openIssuesAndPRs(nameWithOwner)
            announcement = self.logic.findReleaseAnnouncement(nameWithOwner)
        except Exception as e:
            logging.warning(f"Release-announcement check skipped: {e}")
            return True
        annNumber = announcement["number"] if announcement else None
        openCount = len([i for i in (issues or []) if i.get("number") != annNumber]) + len(prs or [])
        if announcement is not None:
            # An announcement exists — remind only if its deadline has not yet passed.
            deadline = announcement.get("deadline")
            todayISO = qt.QDate.currentDate().toString(qt.Qt.ISODate)
            if deadline and todayISO < deadline:
                return slicer.util.confirmOkCancelDisplay(
                    f"You announced a release deadline of {deadline}, which has not passed yet.\n\n"
                    "Cutting the release now will close contributors' open work early. Continue?",
                    windowTitle="Announced deadline not reached")
            return True

        # No announcement was made.  Always nudge (non-blocking): softly when nothing is open
        # (a solo dataset), more firmly when open items would be closed.  'Proceed' is always there.
        msgBox = qt.QMessageBox()
        announceButton = msgBox.addButton("Announce first...", qt.QMessageBox.ActionRole)
        proceedButton = msgBox.addButton("Proceed anyway", qt.QMessageBox.AcceptRole)
        if openCount == 0:
            msgBox.setWindowTitle("No release announcement")
            msgBox.setIcon(qt.QMessageBox.Information)
            msgBox.setText("No upcoming-release announcement was made.")
            msgBox.setInformativeText(
                "If others contribute segmentations to this dataset, it is good practice to announce "
                "the upcoming release first (with a deadline) so they can finish in time. For a solo "
                "dataset you can simply proceed.")
            msgBox.setDefaultButton(proceedButton)
        else:
            msgBox.setWindowTitle("No pre-release announcement")
            msgBox.setIcon(qt.QMessageBox.Warning)
            msgBox.setText("No pre-release announcement has been made for this release.")
            msgBox.setInformativeText(
                f"{openCount} open issue(s)/PR(s) will be closed by this release, and the "
                "contributors have not been notified to finish their work before it is cut.\n\n"
                "Announce now to give them a deadline, proceed anyway, or cancel.")
            msgBox.addButton("Cancel", qt.QMessageBox.RejectRole)
        msgBox.exec_()
        clicked = msgBox.clickedButton()
        if clicked == announceButton:
            self.releaseUI.announcementCollapsibleButton.collapsed = False
            slicer.util.infoDisplay(
                "Set a deadline and message above, then click 'Notify contributors'. "
                "Re-run Make Release when you are ready.",
                windowTitle="Announce upcoming release")
            return False
        if clicked == proceedButton:
            return True
        # Esc / closed with no explicit choice: proceed for the soft (nothing-open) nudge,
        # treat as Cancel when real open items were at stake.
        return openCount == 0

    def onAnnounceUpcomingRelease(self):
        if not self.logic.localRepo:
            return
        nameWithOwner = self.logic.nameWithOwner("origin")
        deadlineISO = self.releaseUI.announcementDeadline.date.toString(qt.Qt.ISODate)
        message = self.releaseUI.announcementMessageEdit.plainText

        # If an announcement already exists, this updates it IN PLACE (same issue, new
        # deadline/message) and re-notifies — it does not close it and create a new one.
        existing = self.logic.findReleaseAnnouncement(nameWithOwner)
        with slicer.util.tryWithErrorDisplay("Failed to query open items", waitCursor=True):
            issues, prs = self.logic.openIssuesAndPRs(nameWithOwner)
        if not issues and not prs and not existing:
            slicer.util.infoDisplay(f"{nameWithOwner} has no open issues or PRs to notify.")
            return
        if existing:
            prompt = (f"There is already a published release announcement. Update it (and re-notify "
                      f"{len(issues)} open issues and {len(prs)} open PRs) with deadline {deadlineISO}?")
        else:
            prompt = (f"Post announcement to {len(issues)} open issues and {len(prs)} open PRs in "
                      f"{nameWithOwner}?\nDeadline: {deadlineISO}")
        if not (self.testingMode or slicer.util.confirmOkCancelDisplay(prompt)):
            return
        with slicer.util.tryWithErrorDisplay("Failed to post announcement", waitCursor=True):
            ni, np = self.logic.announceUpcomingRelease(nameWithOwner, deadlineISO, message)
            slicer.util.showStatusMessage(f"Posted announcement to {ni} issues and {np} PRs.")
        self.updateAnnouncementState(nameWithOwner)  # reflect the just-posted announcement

    def previewRepository(self, repoNameWithOwner):
        """Clones a repository and loads its data for previewing."""
        slicer.util.showStatusMessage(f"Previewing repository {repoNameWithOwner}...")
        if self.testingMode or slicer.util.confirmOkCancelDisplay("Close scene and load repository for preview?"):
            slicer.mrmlScene.Clear()
            with slicer.util.tryWithErrorDisplay("Failed to load repository", waitCursor=True):
                self.logic.loadRepoForPreview(repoNameWithOwner)
                repoDir = self.logic.localRepo.working_dir
                if os.path.exists(repoDir):
                    shutil.rmtree(repoDir)
                self.logic.localRepo = None
                slicer.util.showStatusMessage(f"Repository {repoNameWithOwner} loaded for preview.")
                slicer.util.messageBox("To contribute segmentations, right click on the search results row to open the repository web page and add an issue for your request.  The currently loaded data is not saved by default.",
                                       windowTitle = "You are in Preview mode",
                                       dontShowAgainSettingsKey = "MorphoDepot/DontShowPreviewNotice")


class MorphoDepotLogic(ScriptedLoadableModuleLogic, DepsMixin, GitHubMixin, ControlPlaneMixin, ObjectStoreMixin, RepoClerkMixin, RepoMixin, ContributeMixin, ReleaseMixin, AccessionMixin, SearchMixin):
    """This class should implement all the actual
    computation done by your module.  The interface
    should be such that other python code can import
    this class and make use of the functionality without
    requiring an instance of the Widget.
    Uses ScriptedLoadableModuleLogic base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    accessionFileFormatVersion = 2

    # Pre-release announcement (#124): a dedicated, pinned, labelled issue is the repo-state
    # signal that contributors have been warned of an upcoming release.  An invisible HTML-comment
    # marker in its body carries the machine-readable deadline/tag.
    releasePendingLabel = "release-pending"
    releaseAnnounceMarkerName = "morphodepot:release-announce"

    def __init__(self, progressMethod = None) -> None:
        """Called when the logic class is instantiated. Can be used for initializing member variables."""
        ScriptedLoadableModuleLogic.__init__(self)
        self.segmentationNode = None
        self.segmentationPath = None
        self.localRepo = None
        self.currentIssue = None
        self.progressMethod = progressMethod if progressMethod else lambda *args : None

        # for Search
        self.repoDataByNameWithOwner = {}

        self.executableExtension = '.exe' if os.name == 'nt' else ''
        modulePath = os.path.split(slicer.modules.morphodepot.path)[0]
        self.resourcesPath = os.path.normpath(modulePath + "/Resources")
        self.pixiInstallDir = os.path.normpath(self.resourcesPath + "/pixi")

        # use configured git and gh paths if selected,
        # else use system installed git and gh if available
        # Optionally install with pixi, but only if requireSystemGit is False
        # note: normpath returns "." when given ""
        gitPath = os.path.normpath(slicer.util.settingsValue("MorphoDepot/gitPath", "") or "")
        ghPath = os.path.normpath(slicer.util.settingsValue("MorphoDepot/ghPath", "") or "")
        if not gitPath or gitPath == "" or gitPath == ".":
            gitPath = shutil.which("git") or ""
        if not ghPath or ghPath == "" or ghPath == ".":
            ghPath = shutil.which("gh") or ""
        self.gitExecutablePath = gitPath
        self.ghExecutablePath = ghPath

        qt.QSettings().setValue("MorphoDepot/gitPath", self.gitExecutablePath)
        qt.QSettings().setValue("MorphoDepot/ghPath", self.ghExecutablePath)

class MorphoDepotTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    Uses ScriptedLoadableModuleTest base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def delayDisplay(self, message):
        print(message)

    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear()

    def runTest(self):
        """Run as few or as many tests as needed here."""
        self.setUp()
        widget = slicer.modules.MorphoDepotWidget
        widget.testingMode = True
        # Set to True to stop before exercising the Release tab so it can be inspected manually.
        # gh auth is left in creator mode at the stop point.
        widget.stopBeforeRelease = False
        self._createdTestRepo = None  # set by the test once the repo exists; deleted below
        try:
            self.test_MorphoDepot1()
        finally:
            # Keep the repo when stopping for manual inspection; otherwise delete it.
            if not getattr(widget, "stopBeforeRelease", False):
                self._cleanupTestRepo()
            widget.testingMode = False

    def _cleanupTestRepo(self):
        """Tidy up the test repo created in the testing org.  Deletion needs the `delete_repo`
        token scope, which developers may intentionally not grant; without it the repo is left in
        MorphoDepotTesting (hidden from the RepoClerk dashboard) and we just print a direct link
        for manual removal — no failed-delete noise, no nag."""
        nwo = getattr(self, "_createdTestRepo", None)
        if not nwo:
            return
        logic = slicer.modules.MorphoDepotWidget.logic
        try:
            logic.gh(["repo", "delete", nwo, "--yes"])
            self.delayDisplay(f"Cleaned up test repo {nwo}")
        except Exception:
            self.delayDisplay(
                f"Test repo left for manual cleanup: {nwo}\n"
                f"  delete it at https://github.com/{nwo}/settings (Danger Zone)")
        finally:
            self._createdTestRepo = None

    def _generate_random_species_name(self):
        """Generates a random species-like name for testing."""
        genus_prefixes = ["Testudo", "Pseudo", "Archeo", "Pico", "Nano", "Slicero"]
        genus_suffixes = ["saurus", "therium", "pithecus", "don", "raptor", "morpho"]
        species_epithets = ["minimus", "maximus", "communis", "vulgaris", "testus", "exempli"]

        genus = random.choice(genus_prefixes) + random.choice(genus_suffixes).lower()
        species = random.choice(species_epithets)

        # Create a unique repository name from the species name
        repo_name = f"test-{genus.lower()}-{species.lower()}-{math.floor(1000*random.random())}"
        species_name = f"{genus.capitalize()} {species.lower()}"
        return repo_name, species_name

    def test_MorphoDepot1(self):
        """
        This test emulates the repository creation and issue assignment workflow.
        """

        self.delayDisplay("Starting MorphoDepot flow test")

        # 1. Get creator and annotator accounts from settings
        creator = slicer.util.settingsValue("MorphoDepot/testingCreatorUser", "")
        annotator = slicer.util.settingsValue("MorphoDepot/testingAnnotatorUser", "")
        if not (creator and annotator):
            print("Both Creator and Annotator users must be set in Configure tab's Testing section")
            return

        widget = slicer.modules.MorphoDepotWidget
        logic = widget.logic

        # Helper function for switching user
        def switchUser(username):
            self.delayDisplay(f"Switching gh auth to {username}")
            logic.gh(["auth", "switch", "--user", username])

        # 2. Switch to Creator auth
        switchUser(creator)
        self.delayDisplay("Creating a test repository")
        widget.tabWidget.setCurrentWidget(widget.createUI.createRepository.parent().parent())

        # Use sample data for volume and color table
        import SampleData
        volumeNode = SampleData.SampleDataLogic().downloadMRHead()
        self.assertIsNotNone(volumeNode, "Failed to download MRHead sample data.")
        colorTable = slicer.util.getNode("Labels")
        # Sample "Labels" color table ships without terminology metadata; fill defaults so the
        # create-repo flow does not surface the "Missing Terminology" dialog.
        for colorIndex in range(1, colorTable.GetNumberOfColors()):
            colorTable.SetTerminology(colorIndex, "SCT", "85756007", "Tissue", "SCT", "85756007", "Tissue")
        widget.createUI.inputSelector.setCurrentNode(volumeNode)
        widget.createUI.colorSelector.setCurrentNode(colorTable)

        # Fill out the accession form
        form = widget.createUI.accessionForm
        repoName, speciesName = self._generate_random_species_name()
        form.questions["specimenSource"].optionButtons["Non-accessioned"].click()
        form.questions["species"].answerText.text = speciesName
        form.questions["biologicalSex"].optionButtons["Unknown"].click()
        form.questions["developmentalStage"].optionButtons["Adult"].click()
        form.questions["modality"].optionButtons["Micro CT (or synchrotron)"].click()
        form.questions["contrastEnhancement"].optionButtons["No"].click()
        form.questions["imageContents"].optionButtons["Whole specimen"].click()
        form.questions["redistributionAcknowledgement"].optionButtons["I have the right to allow redistribution of this data."].click()
        form.questions["license"].optionButtons["CC BY 4.0 (requires attribution, allows commercial usage)"].click()
        form.questions["githubRepoName"].answerText.text = repoName
        repoNameWithOwner = f"{logic.morphoDepotTestingOrg}/{repoName}"

        # In testingMode, onCreateRepository provisions the repo PRIVATE directly in the testing
        # org (release asset, no App/S3 — see onCreateRepository).  Record it so it is deleted
        # even if a later step fails, then publish IN PLACE (flip public + topics, no transfer).
        widget.onCreateRepository()
        slicer.app.processEvents()
        self._createdTestRepo = repoNameWithOwner
        self.delayDisplay(f"Repository staged privately in {logic.morphoDepotTestingOrg}; publishing")
        publishedNameWithOwner = logic.publishStagedRepo()
        self.assertEqual(publishedNameWithOwner, repoNameWithOwner,
                         f"Published name {publishedNameWithOwner} does not match expected {repoNameWithOwner}")
        slicer.app.processEvents()
        self.delayDisplay(f"Repository {repoNameWithOwner} created and published.")

        # Open the repository page
        self.delayDisplay(f"Opening repository page for {repoNameWithOwner}")
        repoURL = qt.QUrl(f"https://github.com/{repoNameWithOwner}")
        qt.QDesktopServices.openUrl(repoURL)

        # 3. Create two sample issues as Creator
        self.delayDisplay("Creating sample issues")
        issue1_title = "Segment the cranium"
        issue2_title = "Segment the mandible"
        logic.gh(["issue", "create", "--repo", repoNameWithOwner, "--title", issue1_title, "--body", "Please segment the entire cranium."])
        logic.gh(["issue", "create", "--repo", repoNameWithOwner, "--title", issue2_title, "--body", "Please segment the left and right dentary."])

        # 5. Switch to Annotator auth
        switchUser(annotator)

        # 6. List issues and comment as Annotator
        self.delayDisplay("Listing and commenting on issues as Annotator")
        issues = logic.ghJSON(f"issue list --repo {repoNameWithOwner} --json number,title")
        self.assertEqual(len(issues), 2)

        for issue in issues:
            issueNumber = issue['number']
            self.delayDisplay(f"Commenting on issue #{issueNumber}")
            logic.gh(["issue", "comment", str(issueNumber), "--repo", repoNameWithOwner, "--body", "I would like to work on this issue."])

        # 7. Switch back to Creator auth
        switchUser(creator)

        # 8. Assign issues to the Annotator
        self.delayDisplay("Assigning issues to Annotator")
        issues = logic.ghJSON(f"issue list --repo {repoNameWithOwner} --json number,title")
        for issue in issues:
            issueNumber = issue['number']
            self.delayDisplay(f"Assigning issue #{issueNumber} to {annotator}")
            logic.gh(["issue", "edit", str(issueNumber), "--repo", repoNameWithOwner, "--add-assignee", annotator])

        # Verify assignment
        assignedIssues = logic.ghJSON(f"issue list --repo {repoNameWithOwner} --assignee {annotator} --json number")
        self.assertEqual(len(assignedIssues), 2, f"Expected 2 issues to be assigned to {annotator}")

        # Add a third issue (unassigned, no PR) so it stays open through the release;
        # used to exercise repo-list counts/tooltip, pre-release announcement, and post-release cleanup.
        issue3_title = "Segment the postcranium"
        logic.gh(["issue", "create", "--repo", repoNameWithOwner, "--title", issue3_title,
                  "--body", "Skipping for v1; will land in a later release."])

        # 9. Switch to Annotator to work on issues
        switchUser(annotator)
        self.delayDisplay("Annotator listing assigned issues")
        # Avoid logic.issueList() here: it relies on GitHub's repository search index
        # (topic:MorphoDepot), which lags for newly-created repos. A direct REST query
        # against the known repo is immediate. Synthesize the dict shape loadIssue expects.
        rawIssues = logic.ghJSON(
            f"issue list --repo {repoNameWithOwner} --assignee {annotator} --state open --json number,title"
        )
        repoNameOnly = repoNameWithOwner.split("/")[1]
        annotatorIssues = [
            {'number': i['number'], 'title': i['title'],
             'repository': {'name': repoNameOnly, 'nameWithOwner': repoNameWithOwner}}
            for i in rawIssues
        ]
        self.assertEqual(len(annotatorIssues), 2, f"Annotator should have 2 issues for repo {repoNameWithOwner}.")

        # 10. Annotator loads each issue, makes a change, and creates a PR.
        repoDirectory = logic.localRepositoryDirectory()
        for issue in annotatorIssues:
            self.delayDisplay(f"Annotator working on issue #{issue['number']}: {issue['title']}")

            # Load issue
            slicer.mrmlScene.Clear()
            logic.loadIssue(issue, repoDirectory)

            # Check that things are loaded
            self.assertIsNotNone(logic.segmentationNode, "Segmentation node should be loaded.")
            self.assertTrue(len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")) > 0, "Volume node should be loaded.")

            # Make an arbitrary change to the segmentation
            self.delayDisplay("Making an arbitrary change to the segmentation")
            segmentation = logic.segmentationNode.GetSegmentation()
            segmentation.AddEmptySegment(f"test-segment-by-annotator-{issue['title']}")

            # Commit and push, which creates a draft PR
            commitMessage = f"Work on issue #{issue['number']}"
            self.delayDisplay(f"Committing and creating PR for issue #{issue['number']}")
            widget.annotateUI.messageTitle.text = commitMessage
            widget.onCommit()
            slicer.app.processEvents()

            # 11. Mark PR as ready. widget.onRequestReview -> requestReview -> issuePR ->
            # prList -> search index, which lags for the freshly created PR (silent failure
            # would leave the PR in draft and break the later merge). Find the PR via direct
            # REST and use gh directly.
            self.delayDisplay(f"Requesting review for work on issue #{issue['number']}")
            branchName = f"issue-{issue['number']}"
            rawPRs = logic.ghJSON(f"pr list --repo {repoNameWithOwner} --state open --json number,title")
            matching = [p for p in rawPRs if p['title'] == branchName]
            self.assertEqual(len(matching), 1, f"Expected 1 open PR for branch {branchName}; found {len(matching)}.")
            logic.gh(["pr", "ready", str(matching[0]['number']), "--repo", repoNameWithOwner])
            slicer.app.processEvents()

        # 12. Switch to Creator to review the PRs
        switchUser(creator)
        self.delayDisplay("Creator reviewing PRs")

        # Approve the first PR and request changes on the second

        issueIDs = []
        issuesByID = {}
        for issue in annotatorIssues:
            issueID = f"issue-{issue['number']}"
            issueIDs.append(issueID)
            issuesByID[issueID] = issue
        # Avoid logic.prList("reviewer") here for the same reason as issueList above:
        # it relies on the topic search index. Direct REST against the known repo is reliable.
        rawPRs = logic.ghJSON(
            f"pr list --repo {repoNameWithOwner} --state open --json number,title,author"
        )
        prList = [
            {'number': p['number'], 'title': p['title'], 'author': p['author'],
             'repository': {'name': repoNameOnly, 'nameWithOwner': repoNameWithOwner}}
            for p in rawPRs
        ]
        repoPRsByIssueID = {}
        for pr in prList:
            if pr['repository']['nameWithOwner'] == repoNameWithOwner and pr['title'] in issueIDs:
                repoPRsByIssueID[pr['title']] = pr
        prToApprove = repoPRsByIssueID[issueIDs[0]]
        prToRequestChanges = repoPRsByIssueID[issueIDs[1]]
        issueToChange = issuesByID[issueIDs[1]]

        # Approve the first PR. logic.approvePR() and logic.requestChanges() resolve the PR
        # via logic.issuePR() -> logic.prList() -> ghTopicData() -> the topic search index, which
        # lags for freshly created PRs. We have the PR numbers from the REST query above, so
        # invoke gh directly to avoid the indexing dependency.
        self.delayDisplay(f"Approving and merging PR #{prToApprove['number']}")
        logic.loadPR(prToApprove, repoDirectory)
        approveNum = str(prToApprove['number'])
        logic.gh(["pr", "review", approveNum, "--approve", "--repo", repoNameWithOwner, "--body", "Looks good!"])
        logic.gh(["pr", "merge", approveNum, "--repo", repoNameWithOwner, "--squash", "--body", "Merging and closing"])
        slicer.app.processEvents()

        # Request changes on the second PR (also direct-gh for the same reason).
        self.delayDisplay(f"Requesting changes on PR #{prToRequestChanges['number']}")
        logic.loadPR(prToRequestChanges, repoDirectory)
        changeNum = str(prToRequestChanges['number'])
        logic.gh(["pr", "review", changeNum, "--request-changes", "--repo", repoNameWithOwner, "--body", "Please add another segment."])
        logic.gh(["pr", "ready", changeNum, "--undo", "--repo", repoNameWithOwner])
        slicer.app.processEvents()

        # 13. Switch to Annotator to address feedback
        switchUser(annotator)
        self.delayDisplay("Annotator addressing feedback")

        # Find the issue that needs changes

        # Load the issue, make a change, and request review again
        logic.loadIssue(issueToChange, repoDirectory)
        self.delayDisplay("Making an additional change to the segmentation")
        segmentation = logic.segmentationNode.GetSegmentation()
        segmentation.AddEmptySegment("additional-annotator-segment")

        # Replace widget.onCommit -> commitAndPush. After pushing, commitAndPush calls
        # issuePR (search-index dependent) to check whether to create a PR; on lag it
        # returns None and attempts to create a duplicate. Replicate the save+commit+push
        # directly. The PR already exists, so no creation step is needed.
        self.delayDisplay(f"Committing additional change on issue #{issueToChange['number']}")
        ok = slicer.util.saveNode(logic.segmentationNode, logic.segmentationPath, properties={'useCompression': True})
        self.assertTrue(ok, "Segmentation save failed")
        logic.localRepo.index.add([logic.segmentationPath])
        logic.localRepo.index.commit(f"Addressing feedback on issue #{issueToChange['number']}")
        branchName = logic.localRepo.active_branch.name
        logic.localRepo.git.pull("--rebase", "origin", branchName)
        logic.localRepo.remote(name="origin").push(branchName)
        slicer.app.processEvents()
        # Mark PR ready (it was set back to draft by the request-changes step above).
        logic.gh(["pr", "ready", changeNum, "--repo", repoNameWithOwner])
        slicer.app.processEvents()

        # 14. Switch back to Creator to approve the updated PR (direct gh, same reason).
        switchUser(creator)
        self.delayDisplay(f"Creator approving updated PR #{prToRequestChanges['number']}")
        logic.loadPR(prToRequestChanges, repoDirectory)
        logic.gh(["pr", "review", changeNum, "--approve", "--repo", repoNameWithOwner, "--body", "Thanks for the update!"])
        logic.gh(["pr", "merge", changeNum, "--repo", repoNameWithOwner, "--squash", "--body", "Merging and closing"])
        slicer.app.processEvents()

        # Optional manual-inspection stop before the Release tab automation.
        # Re-asserts creator auth so the tester can drive the Release tab as the repo admin.
        if getattr(widget, 'stopBeforeRelease', False):
            switchUser(creator)
            self.delayDisplay(f"Stopping before release stage. Repo: {repoNameWithOwner}")
            self.delayDisplay(f"gh auth is set to creator: {creator}")
            return

        # Defensive patches for the Release tab automation:
        #   - widget.onRefreshReleaseTab -> administratedRepoList -> morphoRepos uses the
        #     topic-based repository search index, which lags for freshly-created repos.
        #   - widget.onReleaseRepoDoubleClicked -> updateReleaseNotesTemplate ->
        #     closedIssuesSinceLastRelease uses gh issue list --search, which lags for
        #     recently-closed issues.
        # Patch both to use direct REST against the known repo for the test, and restore
        # them after. Production code is unchanged; this whole search layer is going away
        # when RepoClerk lands.
        originalMorphoRepos = logic.morphoRepos
        originalClosedIssuesSince = logic.closedIssuesSinceLastRelease

        def directMorphoRepos():
            repoView = logic.ghJSON(
                f"repo view {repoNameWithOwner} --json name,owner,viewerPermission,pushedAt"
            )
            rawIssues = logic.ghJSON(
                f"issue list --repo {repoNameWithOwner} --state open --json number,title,author,assignees"
            )
            rawPRs = logic.ghJSON(
                f"pr list --repo {repoNameWithOwner} --state open --json number,title,author,isDraft"
            )
            issueNodes = [{
                'number': i['number'],
                'title': i['title'],
                'author': i.get('author') or {},
                'assignees': {'nodes': i.get('assignees', []) or []},
            } for i in rawIssues]
            prNodes = [{
                'number': p['number'],
                'title': p['title'],
                'author': p.get('author') or {},
                'isDraft': p.get('isDraft', False),
            } for p in rawPRs]
            return [{
                'name': repoView['name'],
                'owner': repoView['owner'],
                'viewerPermission': repoView['viewerPermission'],
                'pushedAt': repoView['pushedAt'],
                'issues': {'totalCount': len(issueNodes), 'nodes': issueNodes},
                'pullRequests': {'totalCount': len(prNodes), 'nodes': prNodes},
            }]

        def directClosedIssuesSinceLastRelease(nameWithOwner):
            releases = logic.ghJSON(
                f"release list --repo {nameWithOwner} --json tagName,publishedAt"
            ) or []
            sinceDate = releases[0].get('publishedAt') if releases else None
            allClosed = logic.ghJSON([
                "issue", "list", "--repo", nameWithOwner, "--state", "closed",
                "--json", "number,title,closedAt", "--limit", "200",
            ]) or []
            if sinceDate:
                allClosed = [i for i in allClosed if i.get('closedAt', '') > sinceDate]
            return [{'number': i['number'], 'title': i['title']} for i in allClosed]

        logic.morphoRepos = directMorphoRepos
        logic.closedIssuesSinceLastRelease = directClosedIssuesSinceLastRelease

        try:
            # 15. Create a release and open the repository page
            self.delayDisplay("Creating a new release")
            widget.tabWidget.setCurrentWidget(widget.releaseUI.repoList.parent().parent())
            widget.onRefreshReleaseTab()
            slicer.app.processEvents()

            # Find and select the repository in the list
            repoItem = None
            for i in range(widget.releaseUI.repoList.count):
                item = widget.releaseUI.repoList.item(i)
                repo = widget.reposByItem[item]
                if repo['nameWithOwner'] == repoNameWithOwner:
                    repoItem = item
                    break
            self.assertIsNotNone(repoItem, f"Repository {repoNameWithOwner} not found in release list.")

            # Repo-list row should show counts (1 open issue from issue3, 0 open PRs after merges)
            self.assertIn("(1 open issue, 0 open PRs)", repoItem.text(),
                          f"Repo row text was: {repoItem.text()}")
            self.assertIn(issue3_title, repoItem.toolTip(),
                          f"Tooltip should mention the open issue. Tooltip was: {repoItem.toolTip()}")

            widget.onReleaseRepoDoubleClicked(repoItem)
            slicer.app.processEvents()

            # The baseline picker is no longer auto-populated (issue #123) - the releaser must
            # consciously pick the new baseline. Emulate that: prefer a merged contribution
            # segmentation over the repo's existing baseline so this exercises the normal path.
            existingBaseline = widget.logic.baselineSegmentationNode
            segNodes = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
            newBaseline = next((n for n in segNodes if n is not existingBaseline), existingBaseline)
            widget.releaseUI.newBaselineSelector.setCurrentNode(newBaseline)
            slicer.app.processEvents()

            # Auto-generated change-log should list the two issues closed via merged PRs
            autoNotes = widget.releaseUI.releaseCommentsEdit.plainText
            self.assertIn("## Changes in this release", autoNotes,
                          f"Change-log header missing from auto notes: {autoNotes!r}")
            self.assertIn(issue1_title, autoNotes, f"Issue 1 title missing from change log: {autoNotes!r}")
            self.assertIn(issue2_title, autoNotes, f"Issue 2 title missing from change log: {autoNotes!r}")

            # Prepend owner prose; keep the auto-generated change log below
            ownerComment = "First segmentation complete."
            widget.releaseUI.releaseCommentsEdit.plainText = ownerComment + "\n" + autoNotes

            # Exercise pre-release announcement against the open postcranium issue
            testMarker = "MORPHODEPOT_TEST_ANNOUNCE"
            widget.releaseUI.announcementMessageEdit.plainText = f"{testMarker} (deadline {{deadline}})"
            widget.releaseUI.announcementDeadline.date = qt.QDate.currentDate().addDays(7)
            widget.onAnnounceUpcomingRelease()
            slicer.app.processEvents()

            # Verify the announcement comment landed on the open issue
            openIssues = logic.ghJSON(f"issue list --repo {repoNameWithOwner} --state open --json number,title")
            issue3 = next((i for i in openIssues if i['title'] == issue3_title), None)
            self.assertIsNotNone(issue3, "Open issue 3 should still be present before release.")
            issue3View = logic.ghJSON(f"issue view {issue3['number']} --repo {repoNameWithOwner} --json comments")
            issue3Comments = issue3View.get('comments', []) if isinstance(issue3View, dict) else []
            self.assertTrue(any(testMarker in c.get('body', '') for c in issue3Comments),
                            f"Announcement marker not found in any comment on issue #{issue3['number']}.")

            widget.onMakeRelease()
            slicer.app.processEvents()

            # In testingMode the cleanup confirm is auto-accepted; the lingering issue should now be closed.
            remainingOpen = logic.ghJSON(f"issue list --repo {repoNameWithOwner} --state open --json number")
            self.assertEqual(len(remainingOpen), 0,
                             f"Post-release cleanup should have closed all open items; {len(remainingOpen)} remain.")
        finally:
            logic.morphoRepos = originalMorphoRepos
            logic.closedIssuesSinceLastRelease = originalClosedIssuesSince

        # The test repo is deleted in runTest's finally (self._createdTestRepo), so it is cleaned
        # up even if a step above fails.

        self.delayDisplay("Test passed")
