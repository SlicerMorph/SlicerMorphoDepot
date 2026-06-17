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

        # Opt-in: bake a GitHub Actions workflow into the new repo that auto-assigns each new
        # issue back to its creator.  Offered only when the gh token has the `workflow` scope
        # (checked lazily on Create-tab entry); disabled with a hint otherwise.  Off by default.
        # The checkbox sits next to a "?" button that opens a fuller explanation, since the label
        # alone doesn't convey what the option actually does.
        self.createUI.autoAssignCheckBox = qt.QCheckBox(
            "Set the GitHub workflow to auto-assign new issues to their creators")
        self.createUI.autoAssignCheckBox.checked = False
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
            "When you click 'Publish / Go live', the repository is made public under this owner. "
            "Choose your personal account or an organization you belong to.")
        self.createUI.destinationQuestion.questionBox.setVisible(False)
        self.createUI.destinationPersonalLogin = ""
        goLiveLayout.addWidget(self.createUI.destinationQuestion.questionBox)
        goLiveButtonsLayout = qt.QHBoxLayout()
        self.createUI.publishButton = qt.QPushButton("Publish / Go live")
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

    def _segmentationContentSignature(self, segNode, referenceVolume=None):
        """A content signature of a segmentation (segment ids + binary-labelmap voxels), used
        to detect whether the loaded baseline was edited in the scene.  Hashes content, not
        file bytes, so it is robust to save non-determinism.  A referenceVolume is required to
        sample each segment's labelmap (the segmentation carries no reference geometry of its
        own) — the source volume serves that role.  Falls back to the source volume if not
        given, then to a modified-time string only as a last resort."""
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
        if self._resumedForEdit and self._stagedNameWithOwner:
            self.createUI.createSectionHeader.text = f"Editing staged repository: {self._stagedNameWithOwner}"
        else:
            self.createUI.createSectionHeader.text = "Create a new repository"

    def onCurrentTabChanged(self,index):
        qt.QSettings().setValue("MorphoDepot/tabIndex", index)
        self.updateRefreshButtonLabels()
        if index == self.createTabIndex:
            if not self.ownerSelectorPopulated:
                self.populateOwnerSelector()
            self._refreshAutoAssignAvailability()
            self.refreshStagedReposList()

    def _refreshAutoAssignAvailability(self):
        """Lazily (once) check whether the gh token has the `workflow` scope and enable the
        auto-assign checkbox accordingly.  Without the scope, pushing a `.github/workflows/`
        file would be rejected, so the option is disabled with a hint rather than failing the
        whole repo creation later."""
        if self._workflowScopeChecked:
            return
        self._workflowScopeChecked = True
        try:
            self._hasWorkflowScope = self.logic.hasWorkflowScope()
        except Exception as e:
            logging.warning(f"Could not check workflow scope: {e}")
            self._hasWorkflowScope = False
        checkBox = self.createUI.autoAssignCheckBox
        checkBox.enabled = self._hasWorkflowScope
        if not self._hasWorkflowScope:
            checkBox.checked = False
            checkBox.toolTip = (
                "Auto-assign needs the 'workflow' scope on your GitHub login. Enable it by "
                "running:  gh auth refresh -s workflow")

    def _onAccessionFormValidated(self, valid):
        """Enable the "Create (stage privately)" button when the accession form is valid — but
        only while no repo is staged (the Go-live gate hidden).  Once a repo is staged or open
        for editing, the gate is visible and Create is locked (Update/Publish are the actions)."""
        # The accession form's first validateForm() fires during setup, before goLiveGroup is
        # built — treat a missing gate as "not staged" (create mode).
        goLiveGroup = getattr(self.createUI, "goLiveGroup", None)
        gateVisible = goLiveGroup is not None and goLiveGroup.visible
        self.createUI.createRepository.enabled = valid and not gateVisible

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

        # Destination: members choose where the repo lives; non-members always use their own
        # account.  Org = S3, 10 GB, governed (you cannot delete a *public* org repo — owners do).
        # Personal = a GitHub release asset, 2 GB, fully yours (delete anytime) — for
        # disposable/classroom repos.
        useOrg = False
        testingOwner = None
        if self.testingMode:
            # Developer self-test: provision directly into the throwaway testing org using the
            # creator's own gh rights (release asset, no App, no S3) — never the personal account
            # or the production MorphoDepot org.  No destination prompt.
            testingOwner = self.logic.morphoDepotTestingOrg
        elif self.logic.userIsOrgMember():
            who = self.logic.whoami()
            org = self.logic.morphoDepotOrg
            items = [f"My account ({who}) — personal, 2 GB, you can delete it anytime",
                     f"{org} (organization) — S3, 10 GB, governed"]
            # PythonQt's static getItem doesn't return (text, ok) like PyQt, so drive an
            # explicit dialog: exec_() gives the OK/Cancel, textValue() the selection.
            dialog = qt.QInputDialog(slicer.util.mainWindow())
            dialog.setWindowTitle("Where should this repository live?")
            dialog.setLabelText("Create this repository under:")
            dialog.setComboBoxItems(items)
            dialog.setComboBoxEditable(False)
            dialog.setTextValue(items[0])
            if not dialog.exec_():
                self.progressMethod("Repository creation aborted")
                return
            choice = dialog.textValue()
            useOrg = (choice == items[1])

        if not self.showConfirmationDialog(sourceVolume, colorTable, accessionData, sourceSegmentation, self.screenshots, useOrg=useOrg):
            self.progressMethod("Repository creation aborted")
            return

        # Opt-in auto-assign workflow: only when the box is checked AND the token actually has
        # the `workflow` scope (double-gated — the box is disabled without the scope).
        enableAutoAssign = bool(self.createUI.autoAssignCheckBox.checked and getattr(self, "_hasWorkflowScope", False))

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

    def onPublish(self):
        """Take the staged repo live (make it public) where it already lives — the destination
        was fixed at create, so this no longer offers an org/personal choice or any transfer."""
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
                slicer.util.showStatusMessage("Publishing...")
                final = self.logic.publishStagedRepo()
        except Exception as e:
            slicer.util.showStatusMessage("")
            return
        slicer.util.showStatusMessage("")
        if not final:
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
        form.questions["repoType"].optionButtons["Short-term (e.g. repositories for classroom exercises, that are not meant to be maintained for long-term)"].click()
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

    # Annotate
    def onRefresh(self):
        with slicer.util.tryWithErrorDisplay("Failed to refresh from GitHub", waitCursor=True):
            self.logic.ghTopicClearCache()
            self.annotateUI.issueList.clear()
            self.annotateUI.prList.clear()
            self.updateIssueList()
            self.updateAnnotatePRList()

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
            self.selectedPR = self.prsByItem[item]
            self.annotateUI.openPRPageButton.enabled = True

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
        with slicer.util.tryWithErrorDisplay("Failed to update PR list", waitCursor=True):
            self.logic.ghTopicClearCache()
            self.updateReviewPRList()

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
        pr = self.prsByItem[item]
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

    def updateAnnouncementCounts(self, repoData):
        issues = repoData.get('issues', {}).get('totalCount', 0)
        prs = repoData.get('pullRequests', {}).get('totalCount', 0)
        self.releaseUI.announcementCounts.text = f"Will post to {issues} open issues and {prs} open PRs."

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
        with slicer.util.tryWithErrorDisplay("Failed to refresh search cache", waitCursor=True):
            slicer.util.showStatusMessage("Refreshing search cache...")
            self.logic.refreshSearchCache()
            self.searchUI.searchForm.searchBox.setPlaceholderText("Search...")
            self.searchUI.searchForm.topWidget.enabled = True
            self.searchUI.searchCollapsibleButton.collapsed = False
            self.doSearch()

    def doSearch(self):
        criteria = self.searchUI.searchForm.criteria()
        results = self.logic.search(criteria)
        self.updateSearchResults(results)

    def repoDataKetToRepoNameAndOwner(self, repoDataKey):
        nameWithOwnerSplit = repoDataKey.split('^')
        repoName = nameWithOwnerSplit[0]
        owner = nameWithOwnerSplit[1]
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
                screenshotCaptions = repoData.get('screenshotCaptions', {})
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

        # #123: guard against releasing the existing baseline unchanged (which discards any
        # merged contribution).  If the picked baseline is the very node loaded from the repo as
        # the current baseline, this release would carry no new segmentation work.
        loadedBaseline = getattr(self.logic, 'baselineSegmentationNode', None)
        if (loadedBaseline is not None and baselineNode is not None
                and baselineNode.GetID() == loadedBaseline.GetID()):
            if not (self.testingMode or slicer.util.confirmOkCancelDisplay(
                    "The selected baseline is the repository's current baseline, so this release "
                    "would incorporate no new segmentation work.\n\n"
                    "If you meant to publish merged contributions, click Cancel and select (or "
                    "build) the updated segmentation as the new baseline first.\n\n"
                    "Release with the unchanged baseline anyway?",
                    windowTitle="Baseline unchanged")):
                return

        # #124: non-blocking reminder if no pre-release announcement was made (or its deadline
        # has not passed). Never enforces — every path can proceed.
        if not self._confirmReleaseAnnouncement(nameWithOwner):
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

        self.logic.discardReleaseBackup()
        self.releaseUI.releaseCommentsEdit.plainText = ""
        self.updateCurrentVersionLabel()
        if createdTag:
            # #124: retire the pre-release announcement (unpin, unlabel, close) now that the
            # release exists — done unconditionally, independent of the optional item-close step
            # below, so the next cycle's announcement detection starts clean.
            self.logic.clearReleaseAnnouncement(nameWithOwner, createdTag)
            self.maybeCloseOpenItemsForRelease(nameWithOwner, createdTag)
            # Reset the in-session screenshots so the next release starts clean.
            self.screenshots = []
            self.updateScreenshotCount()
        slicer.util.showStatusMessage("New release created. You can add more comments on the GitHub release page.")

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
            "Note: a local reset cannot undo any push that may have already reached origin/main; "
            "if the push succeeded you may need to fix things on GitHub manually."
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

    def maybeCloseOpenItemsForRelease(self, nameWithOwner, version):
        """After a release, offer to close all remaining open issues and PRs."""
        with slicer.util.tryWithErrorDisplay("Failed to query open items", waitCursor=True):
            issues, prs = self.logic.openIssuesAndPRs(nameWithOwner)
        if not issues and not prs:
            return
        prompt = (
            f"Release {version} created.\n\n"
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
        if openCount == 0:
            return True
        if announcement is None:
            msgBox = qt.QMessageBox()
            msgBox.setWindowTitle("No pre-release announcement")
            msgBox.setIcon(qt.QMessageBox.Warning)
            msgBox.setText("No pre-release announcement has been made for this release.")
            msgBox.setInformativeText(
                f"{openCount} open issue(s)/PR(s) will be closed by this release, and the "
                "contributors have not been notified to finish their work before it is cut.\n\n"
                "Announce now to give them a deadline, proceed anyway, or cancel.")
            announceButton = msgBox.addButton("Announce now...", qt.QMessageBox.ActionRole)
            proceedButton = msgBox.addButton("Proceed anyway", qt.QMessageBox.AcceptRole)
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
            return clicked == proceedButton
        # An announcement exists — remind only if its deadline has not yet passed.
        deadline = announcement.get("deadline")
        todayISO = qt.QDate.currentDate().toString(qt.Qt.ISODate)
        if deadline and todayISO < deadline:
            return slicer.util.confirmOkCancelDisplay(
                f"You announced a release deadline of {deadline}, which has not passed yet.\n\n"
                "Cutting the release now will close contributors' open work early. Continue?",
                windowTitle="Announced deadline not reached")
        return True

    def onAnnounceUpcomingRelease(self):
        if not self.logic.localRepo:
            return
        nameWithOwner = self.logic.nameWithOwner("origin")
        deadlineISO = self.releaseUI.announcementDeadline.date.toString(qt.Qt.ISODate)
        message = self.releaseUI.announcementMessageEdit.plainText
        with slicer.util.tryWithErrorDisplay("Failed to query open items", waitCursor=True):
            issues, prs = self.logic.openIssuesAndPRs(nameWithOwner)
        if not issues and not prs:
            slicer.util.infoDisplay(f"{nameWithOwner} has no open issues or PRs to notify.")
            return
        prompt = (
            f"Post announcement to {len(issues)} open issues and {len(prs)} open PRs in {nameWithOwner}?\n"
            f"Deadline: {deadlineISO}"
        )
        if not (self.testingMode or slicer.util.confirmOkCancelDisplay(prompt)):
            return
        with slicer.util.tryWithErrorDisplay("Failed to post announcement", waitCursor=True):
            ni, np = self.logic.announceUpcomingRelease(nameWithOwner, deadlineISO, message)
            slicer.util.showStatusMessage(f"Posted announcement to {ni} issues and {np} PRs.")

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
        valid = valid and self.questions["githubRepoName"].answer() != ""
        valid = valid and self.questions["repoType"].answer() != ""
        repoNameRegex = r"^(?:([a-zA-Z\d]+(?:-[a-zA-Z\d]+)*)/)?([\w.-]+)$"
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
        self.checkSpeciesButton = qt.QPushButton("Check species")
        self.checkSpeciesButton.connect("clicked()", self.onCheckSpecies)
        self.questionLayout.addWidget(self.checkSpeciesButton)
        self.searchButton = qt.QPushButton()
        self.searchButton.setIcon(qt.QIcon(qt.QPixmap(":/Icons/Search.png")))
        self.searchButton.connect("clicked()", self.onSearchSpecies)
        self.questionLayout.addWidget(self.searchButton)
        self.speciesInfo = qt.QLabel()
        self.questionLayout.addWidget(self.speciesInfo)
        self.searchDialog = None

    def _setSpeciesInfoLabel(self, result):
        requiredKeys = ['matchType', 'rank', 'canonicalName', 'kingdom', 'phylum', 'class', 'order', 'family', 'genus', 'species']
        for key in requiredKeys:
            if key not in result:
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

    def onCheckSpecies(self):
        import pygbif
        result = pygbif.species.name_backbone(self.answerText.text)
        self._setSpeciesInfoLabel(result)

    def answer(self):
        return self.answerText.text


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

class MorphoDepotLogic(ScriptedLoadableModuleLogic):
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

    def slicerVersionCheck(self):
        return hasattr(slicer.vtkSegment, "SetTerminology")

    def resolveVolumeURL(self, volumeRef, repoNameWithOwner):
        """Convert a source_volume file reference to a full download URL.
        Accepts both legacy full URLs (backwards compatible) and new relative paths.
        A source_volume now holds an absolute object-store (JS2) URL; because it starts with
        "http" it passes straight through here unchanged — only the older relative
        "releases/download/v1/..." pointers are re-based onto the repo owner.
        """
        if volumeRef.startswith("http"):
            return volumeRef  # full URL (object-store or legacy hardcoded) — use as-is
        return f"https://github.com/{repoNameWithOwner}/{volumeRef}"

    def uploadSourceVolumeToObjectStore(self, sourceFilePath, sha256, creator, repo, filename):
        """Upload the source volume to the MorphoDepot object store (JS2) with a server-mediated
        S3 MULTIPART upload, and return the public URL of the stored object.

        The signing service holds the bucket credentials; the client never does.  The server
        runs create / complete / abort and signs each part URL on demand, so a slow upload never
        outlives a part URL (the client re-signs on a 403).  The client PUTs each chunk directly
        to S3 and verifies the returned part ETag.  On any failure the whole upload is aborted so
        no orphaned parts linger (a bucket lifecycle rule is the backstop).  The object is keyed
        {creator}/{repo}/{filename} with the volume's identity (sha256, creator, repo, original
        filename) stamped as immutable S3 user-metadata at create time; integrity is guaranteed
        end-to-end by the committed source_volume_checksum (SHA-256 of the whole file).  Endpoint
        + fallback token come from QSettings (MorphoDepot/uploadSignEndpoint,
        MorphoDepot/uploadSignToken).  See docs/ObjectStorage-model.md."""
        import requests
        qsettings = qt.QSettings()
        signEndpoint = qsettings.value("MorphoDepot/uploadSignEndpoint",
                                       "https://join.morphodepot.org/uploads/sign")
        # Derive the multipart base: …/uploads/sign -> …/uploads/multipart
        base = signEndpoint.rsplit("/uploads/", 1)[0] + "/uploads/multipart"
        # Authenticate with the user's own gh token (member tier — "git + gh, nothing else").
        # Falls back to a QSettings shared token only if gh is somehow unavailable.
        try:
            token = self._ghToken()
        except Exception:
            token = ""
        if not token:
            token = qsettings.value("MorphoDepot/uploadSignToken", "")
        authHeaders = {"Authorization": f"Bearer {token}"} if token else {}

        def post(path, body):
            r = requests.post(f"{base}/{path}", json=body, headers=authHeaders, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"Object-store multipart {path} failed "
                                   f"({r.status_code}): {r.text}")
            return r.json()

        size = os.path.getsize(sourceFilePath)
        self.progressMethod("Requesting multipart upload from the object-store signing service...")
        created = post("create", {"sha256": sha256, "size": size, "creator": creator,
                                  "repo": repo, "filename": filename})
        publicURL = created["public_url"]
        if created.get("already_exists"):
            self.progressMethod("Source volume already in the object store; skipping upload.")
            return publicURL

        key = created["key"]
        uploadId = created["upload_id"]
        partSize = int(created.get("part_size") or (128 * 2**20))
        parts = []
        try:
            with open(sourceFilePath, "rb") as fp:
                partNumber = 0
                uploaded = 0
                while True:
                    chunk = fp.read(partSize)
                    if not chunk:
                        break
                    partNumber += 1
                    etag = self._uploadOneVolumePart(post, key, uploadId, partNumber, chunk)
                    parts.append({"part_number": partNumber, "etag": etag})
                    uploaded += len(chunk)
                    self.progressMethod(f"Uploaded {uploaded / 2**20:.0f} / {size / 2**20:.0f} MB "
                                        "to the object store...")
            self.progressMethod("Finalizing multipart upload...")
            completed = post("complete", {"key": key, "upload_id": uploadId, "parts": parts})
            return completed.get("public_url", publicURL)
        except Exception:
            # Abort so a partial upload leaves no orphaned parts (lifecycle rule is the backstop).
            try:
                post("abort", {"key": key, "upload_id": uploadId})
                self.progressMethod("Upload failed — aborted the multipart upload (no orphaned data).")
            except Exception as abortError:
                logging.warning(f"Could not abort multipart upload {key}/{uploadId}: {abortError}")
            raise

    def _uploadOneVolumePart(self, post, key, uploadId, partNumber, chunk):
        """PUT one multipart chunk to a freshly-signed part URL, verify its ETag, and return it.
        Re-signs the URL (handling expiry) and retries on transient failure."""
        import requests
        import hashlib
        expectedMd5 = hashlib.md5(chunk).hexdigest()
        lastError = None
        for _attempt in range(4):
            signed = post("sign", {"key": key, "upload_id": uploadId, "part_number": partNumber})
            try:
                resp = requests.put(signed["url"], data=chunk, timeout=None)
            except Exception as e:
                lastError = e
                continue
            if resp.status_code == 403:   # part URL expired — re-sign and retry
                lastError = RuntimeError("part URL expired (403)")
                continue
            if resp.status_code not in (200, 201):
                lastError = RuntimeError(f"part {partNumber} PUT failed "
                                         f"({resp.status_code}): {resp.text}")
                continue
            etag = (resp.headers.get("ETag") or "").strip()
            clean = etag.strip('"').lower()
            # For our unencrypted bucket the part ETag is the part's MD5 — verify when it looks
            # like one (skip for non-MD5 ETags; the whole-file SHA-256 still backstops on download).
            if len(clean) == 32 and all(c in "0123456789abcdef" for c in clean) and clean != expectedMd5:
                lastError = RuntimeError(f"part {partNumber} checksum mismatch")
                continue
            if not etag:
                lastError = RuntimeError(f"part {partNumber} returned no ETag")
                continue
            return etag
        raise RuntimeError(f"part {partNumber} failed after retries: {lastError}")

    # --- Membership tier + App control plane (member repos are born in-org, App-mediated) ---

    morphoDepotOrg = "MorphoDepot"

    # Throwaway org for the developer Reload-and-Test self-test.  Test repos are created here
    # directly (the developer's own gh rights — no App, no S3), never on a personal account or in
    # the production org, and are deleted at the end of the test.  Both dev accounts are members.
    morphoDepotTestingOrg = "MorphoDepotTesting"

    def controlPlaneBase(self):
        return qt.QSettings().value(
            "MorphoDepot/controlPlaneBase", "https://join.morphodepot.org").rstrip("/")

    def userIsOrgMember(self, org=None):
        """True if the current user is an active member of the MorphoDepot org (so they get the
        S3 / in-org / App-mediated tier).  Asks the App control plane (`/me`), which checks
        membership with the App token — so the user's gh token needs no org scope.  Cached per
        session (reload the module after switching gh accounts or joining the org)."""
        org = org or self.morphoDepotOrg
        cache = getattr(self, "_orgMemberCache", None)
        if cache is not None and cache[0] == org:
            return cache[1]
        try:
            info = self.controlPlaneRequest("me", {})
            isMember = bool(info.get("is_member"))
        except Exception as e:
            logging.warning(f"Membership check failed (assuming non-member): {e}")
            isMember = False
        self._orgMemberCache = (org, isMember)
        return isMember

    def _ghToken(self):
        """The user's GitHub token (from gh) used to authenticate to the App control plane."""
        import subprocess
        try:
            out = subprocess.run([self.ghExecutablePath, "auth", "token"],
                                 capture_output=True, text=True, timeout=15)
            return (out.stdout or "").strip()
        except Exception as e:
            raise RuntimeError(f"Could not get a GitHub token from gh: {e}")

    def controlPlaneRequest(self, path, body):
        """POST to the intake App control plane, authenticated by the user's gh token.
        Returns the parsed JSON, or raises with the server's error detail."""
        import requests
        token = self._ghToken()
        if not token:
            raise RuntimeError("Not signed in to GitHub — run `gh auth login` first.")
        r = requests.post(f"{self.controlPlaneBase()}/{path}", json=body,
                          headers={"Authorization": f"Bearer {token}"}, timeout=120)
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise RuntimeError(f"Control plane '{path}' failed ({r.status_code}): {detail}")
        return r.json()

    def volumeChecksumIndexURL(self):
        """RepoClerk's published checksum->repo index (GitHub Pages JSON)."""
        return qt.QSettings().value(
            "MorphoDepot/volumeChecksumIndexURL",
            "https://MorphoDepot.github.io/RepoClerk/volume-checksums.json")

    def duplicateVolumeRepos(self, checksum, exclude=None):
        """Repos (nameWithOwner) that already hold a volume with this SHA-256, per RepoClerk's
        published checksum->repo index.  Best-effort and ADVISORY: returns [] on any failure
        (network down, index missing/stale) so it never blocks staging or publishing.  The index
        lags RepoClerk's crawl (~6 h), so a very recently published duplicate may not appear yet.
        `exclude` drops the repo being created/published (a repo never duplicates itself)."""
        if not checksum:
            return []
        sha = checksum.strip()
        if ":" in sha:  # the committed file is "SHA256:<hex>"; the index stores bare hex
            sha = sha.split(":", 1)[1].strip()
        sha = sha.lower()
        if not sha:
            return []
        try:
            resp = requests.get(self.volumeChecksumIndexURL(), timeout=10)
            resp.raise_for_status()
            index = resp.json().get("checksums", {})
        except Exception as e:
            logging.warning(f"Duplicate-volume check skipped ({e})")
            return []
        return [r for r in index.get(sha, []) if r != exclude]

    def localRepositoryDirectory(self):
        repoDirectory = os.path.normpath(slicer.util.settingsValue("MorphoDepot/repoDirectory", "") or "")
        if repoDirectory == "" or repoDirectory == ".":
            defaultScenePath = os.path.normpath(slicer.app.defaultScenePath)
            defaultRepoDir = os.path.join(defaultScenePath, "MorphoDepot")
            self.setLocalRepositoryDirectory(defaultRepoDir)
            repoDirectory = defaultRepoDir

        if repoDirectory and not os.path.exists(repoDirectory):
            message = f"The repository directory does not exist:\n\n{repoDirectory}\n\nCreate it now?"
            if slicer.util.confirmOkCancelDisplay(message, windowTitle="Create Directory"):
                try:
                    os.makedirs(repoDirectory)
                except OSError as e:
                    logging.error(f"Could not create repository directory {repoDirectory}: {e}")

        return repoDirectory

    def setLocalRepositoryDirectory(self, repoDir):
        qt.QSettings().setValue("MorphoDepot/repoDirectory", repoDir)

    def checkPythonDependencies(self):
        """See if pygbif and idigbio are available.
        The GitPython package is installed by default in slicer.
        """
        try:
            import pygbif
        except ModuleNotFoundError:
            return False

        try:
            import idigbio
        except ModuleNotFoundError:
            return False

        return True

    def installPythonDependencies(self):
        """Install pygbif and idigbio if needed
        """
        try:
            import pygbif
        except ModuleNotFoundError:
            self.progressMethod(f"Installing pygbif")
            slicer.util.pip_install("pygbif")
            import pygbif

        try:
            import idigbio
        except ModuleNotFoundError:
            self.progressMethod(f"Installing idigbio")
            slicer.util.pip_install("idigbio")
            import idigbio

    def checkCommand(self, command):
        try:
            completedProcess = subprocess.run(command, capture_output=True)
            returnCode = completedProcess.returncode
            stdout = completedProcess.stdout
            stderr = completedProcess.stderr
        except Exception as e:
            stdout =  ""
            stderr = str(e)
            returnCode = -1
        if returnCode != 0:
            self.progressMethod(f"{command} failed to run, returned {returnCode}")
            self.progressMethod(stdout)
            self.progressMethod(stderr)
            return False
        return True

    def checkGitDependencies(self):
        """Check that git, and gh are available
        """
        if not (self.gitExecutablePath and self.ghExecutablePath):
            self.progressMethod("git/gh paths are not set")
            return False
        if not (os.path.exists(self.gitExecutablePath) and os.path.exists(self.ghExecutablePath)):
            self.progressMethod("bad git/gh paths")
            self.progressMethod(f"git path is {self.gitExecutablePath}")
            self.progressMethod(f"gh path is {self.ghExecutablePath}")
            return False
        if not self.checkCommand([self.gitExecutablePath, '--version']):
            return False
        if not self.checkCommand([self.ghExecutablePath, 'auth', 'status']):
            return False
        return True

    def gh(self, command):
        """Execute `gh` command.  Multiline input string accepted for readablity.
        Do not include `gh` in the command string"""
        if not self.ghExecutablePath or self.ghExecutablePath == "":
            logging.error("Error, gh not found")
            return "Error, gh not found"
        if command.__class__() == "":
            commandList = command.replace("\n", " ").split()
        elif command.__class__() == []:
            commandList = command
        else:
            logging.error("command must be string or list")
        self.progressMethod(" ".join(commandList))
        fullCommandList = [self.ghExecutablePath] + commandList

        baseDelay = 1
        attempts = 4
        for attempt in range(attempts):
            originalLocale = locale.setlocale(locale.LC_ALL)
            locale.setlocale(locale.LC_ALL, "en_US.UTF-8")
            process = slicer.util.launchConsoleProcess(fullCommandList)
            result = process.communicate()
            locale.setlocale(locale.LC_ALL, originalLocale)
            needRetry = result[0].find("error: 503") != -1
            if process.returncode == 0 or not needRetry:
                if attempt > 0:
                    print(f"Command succeeded after try {attempt}")
                break
            error_message = f"gh command failed: {' '.join(commandList)}\nCode: {process.returncode}, Output: {result}"
            print(error_message)
            delay = baseDelay * (2 ** attempt)
            self.progressMethod(f"gh returned {process.returncode}, sleeping {delay} seconds before retry")
            time.sleep(delay)
        if process.returncode != 0:
            error_message = f"gh command failed: {' '.join(commandList)}\nOutput: {result}"
            logging.error(error_message)
            self.progressMethod(f"gh command error: {result}")
            raise RuntimeError(error_message)
        self.progressMethod(f"gh command finished: {result}")
        return result[0]

    def getGitConfig(self, key):
        """Get a value from the global git config."""
        if not self.gitExecutablePath:
            return ""
        try:
            command = [self.gitExecutablePath, 'config', '--global', key]
            result = subprocess.check_output(command, universal_newlines=True).strip()
            return result
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""

    def setGitConfig(self, key, value):
        """Set a value in the global git config."""
        if not self.gitExecutablePath:
            return
        try:
            subprocess.check_call([self.gitExecutablePath, 'config', '--global', key, value])
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logging.error(f"Failed to set git config {key}: {e}")

    def ghJSON(self, command):
        """Wrapper around gh that returns json loaded data or an empty list on error"""
        jsonString = self.gh(command)
        if jsonString:
            return json.loads(jsonString)
        return []

    def hasWorkflowScope(self):
        """True if the active gh token carries the `workflow` OAuth scope, which GitHub requires
        to push or create files under `.github/workflows/`.  Used to gate the optional auto-assign
        workflow at repo creation: without it, a push that includes a workflow file is rejected,
        so the option is offered only when the scope is present.  Reads GitHub's authoritative
        `X-Oauth-Scopes` response header via `gh api -i`."""
        try:
            output = self.gh("api -i user")
        except Exception as e:
            logging.warning(f"Could not determine gh token scopes: {e}")
            return False
        for line in output.splitlines():
            if line.lower().startswith("x-oauth-scopes:"):
                scopes = [s.strip() for s in line.split(":", 1)[1].split(",")]
                return "workflow" in scopes
        return False

    def ghTopicClearCache(self):
        self.gh("config clear-cache")

    def ghTopicData(self, topic="MorphoDepot"):
        query="""
            query($params: String!, $endCursor: String) {
                search(query: $params, type: REPOSITORY, first: 100, after: $endCursor) {
                    nodes {
                        ... on Repository {
                            nameWithOwner
                            pullRequests(states: [OPEN], first: 100) {
                                nodes {
                                    number title isDraft url
                                    author { login }
                                    closingIssuesReferences(first: 1) {
                                      nodes { title author {login} repository { name owner {login} } }
                                    }
                                }
                            }
                            issues(states: [OPEN], first: 100) {
                                totalCount
                                nodes {
                                    number title url author { login }
                                    assignees(first: 5) { nodes { login } }
                                }
                            }
                        }
                    }
                    pageInfo { endCursor hasNextPage }
                }
            }
        """
        params = f"topic:{topic} fork:true"
        command = ['api', 'graphql', "--cache", "10m", '--paginate', '--slurp',
                   '-f', f'query={query}', '-f', f'params={params}']
        searchData = self.ghJSON(command)
        return searchData[0]['data']['search']['nodes']

    def morphoRepos(self):
        # TODO: generalize for other topics
        query = """
            query($searchQuery: String!, $endCursor: String) {
              search(query: $searchQuery, type: REPOSITORY, first: 100, after: $endCursor) {
                nodes {
                  ... on Repository {
                    name
                    owner {
                      login
                    }
                    viewerPermission
                    pushedAt
                    issues(states: [OPEN], first: 100) {
                      totalCount
                      nodes {
                        number title
                        author { login }
                        assignees(first: 5) { nodes { login } }
                      }
                    }
                    pullRequests(states: [OPEN], first: 100) {
                      totalCount
                      nodes {
                        number title isDraft
                        author { login }
                      }
                    }
                  }
                }
                pageInfo { endCursor hasNextPage }
              }
            }
        """
        search_query_string = "topic:morphodepot fork:true"
        command = ['api', 'graphql', '--paginate', '--slurp',
                   '-f', f'query={query}', '-f', f'searchQuery={search_query_string}']
        pages = self.ghJSON(command)
        all_repos = [repo for page in pages for repo in page['data']['search']['nodes'] if repo]

        return all_repos

    def whoami(self):
        """ Get the active gh account """
        return(self.gh("auth status --active").split()[7])

    def ghUserProfile(self):
        """Return {login, name, email} for the active gh account from `gh api user`.

        Uses only the default `gh auth login` scopes (no `user`/`user:email` needed): `login`
        is always present; `name` (display name) and `email` are the *public* profile values and
        may be empty when the user keeps them private.  Returns empty strings on any error so
        callers can fall back to asking the user.  We deliberately do NOT read `user/emails`
        (the verified-primary endpoint) because that requires broadening the requested scopes."""
        try:
            data = self.ghJSON(["api", "user"])
        except Exception as e:
            logging.warning(f"Could not fetch gh user profile: {e}")
            return {"login": "", "name": "", "email": ""}
        if not isinstance(data, dict):
            return {"login": "", "name": "", "email": ""}
        return {
            "login": data.get("login") or "",
            "name": data.get("name") or "",
            "email": data.get("email") or "",
        }

    def userOrganizations(self):
        """Return the list of GitHub organization logins the active gh user belongs to.

        A user may belong to several organizations; all are returned so the Create tab
        can offer them as repository destinations.  Returns an empty list on any error
        (e.g. gh not configured) so callers can fall back to the personal account.
        """
        try:
            data = self.ghJSON(["api", "user/orgs", "--paginate"])
        except Exception as e:
            logging.warning(f"Could not fetch user organizations: {e}")
            return []
        if not isinstance(data, list):
            return []
        return [org["login"] for org in data if isinstance(org, dict) and "login" in org]

    def repoExists(self, nameWithOwner):
        """Read-only check: does a repo at {owner}/{name} exist and is it visible to us?

        Used as the pre-create / pre-publish collision preflight.  Creates nothing.  Returns
        True if the GET succeeds, False on a 404 (or any error, treated as 'available')."""
        try:
            self.gh(["api", f"/repos/{nameWithOwner}", "--silent"])
            return True
        except RuntimeError:
            return False

    def setRepoVisibility(self, nameWithOwner, public=True):
        """Flip a repository's visibility via the REST API.

        Using the API avoids `gh repo edit --visibility`'s required
        --accept-visibility-change-consequences confirmation flag."""
        privateValue = "false" if public else "true"
        self.gh(["api", "--method", "PATCH", f"/repos/{nameWithOwner}",
                 "-F", f"private={privateValue}"])

    def addMorphoTopics(self, nameWithOwner, speciesTopicString):
        """Publish topic transition (last step of go-live): add the discoverability topic(s)
        and REMOVE the `morphodepot-staging` marker, so the repo becomes findable by RepoClerk
        and the extension and simultaneously leaves the unpublished list.  The species topic is
        skipped when unknown (e.g. recovered repos with no species)."""
        command = f"repo edit {nameWithOwner} --add-topic morphodepot --remove-topic {self.stagingTopic}"
        if speciesTopicString:
            command = (f"repo edit {nameWithOwner} --add-topic morphodepot "
                       f"--add-topic md-{speciesTopicString} --remove-topic {self.stagingTopic}")
        self.gh(command)

    def issueList(self):
        me = self.whoami()
        repoData = self.ghTopicData()
        issueList = []
        for repo in repoData:
            for issue in repo['issues']['nodes']:
                assignees = [node['login'] for node in issue['assignees']['nodes']]
                if me in assignees:
                    repoName = repo['nameWithOwner'].split("/")[1]
                    issueList.append({'number': issue['number'],
                                      'title': issue['title'],
                                      'repository': { 'name': repoName, 'nameWithOwner': repo['nameWithOwner']}})
        return issueList

    def administratedRepoList(self):
        returnRepos = []
        for repo in self.morphoRepos():
            if repo['viewerPermission'] == 'ADMIN':
                repo['nameWithOwner'] = f"{repo['owner']['login']}/{repo['name']}"
                returnRepos.append(repo)
        return returnRepos

    def prList(self, role="segmenter"):
        """
        Fetch a list of open pull requests for the user, either as 'segmenter' or reviewer.
        Returns PRs, their associated issue titles, and repository topics.
        """
        me = self.whoami()
        repoData = self.ghTopicData()
        prList = []
        for repo in repoData:
            for pr in repo['pullRequests']['nodes']:
                if role == "segmenter":
                    parties = [pr['author']['login']]
                elif role == "reviewer":
                    parties = [issue['repository']['owner']['login'] for issue in pr['closingIssuesReferences']['nodes']]
                else:
                    raise BaseException(f"Unknown role {role}")
                issueTitles = [issue['title'] for issue in pr['closingIssuesReferences']['nodes']]
                if me in parties:
                    repoName = repo['nameWithOwner'].split("/")[1]
                    prList.append({'number': pr['number'],
                                      'title': pr['title'],
                                      'issueTitles': issueTitles,
                                      'isDraft': pr['isDraft'],
                                      'author': {'login': pr['author']['login']},
                                      'repository': { 'name': repoName, 'nameWithOwner': repo['nameWithOwner']}})
        return prList


    def repositoryList(self):
        repositories = json.loads(self.gh("repo list --json name"))
        repositoryList = [r['name'] for r in repositories]
        return repositoryList

    def ensureUpstreamExists(self):
        if not "upstream" in self.localRepo.remotes:
            # no upstream, so this is an issue assigned to the owner of the repo
            self.localRepo.create_remote("upstream", list(self.localRepo.remotes[0].urls)[0])

    def loadIssue(self, issue, repoDirectory):
        self.currentIssue = issue
        self.progressMethod(f"Loading issue {issue} into {repoDirectory}")
        issueNumber = issue['number']
        branchName=f"issue-{issueNumber}"
        sourceRepository = issue['repository']['nameWithOwner']
        repositoryName = issue['repository']['name']
        localDirectory = os.path.join(repoDirectory, f"{repositoryName}-{branchName}")

        self.cacheOldVersion(localDirectory)

        forkExists = repositoryName in self.repositoryList()
        if not forkExists:
            self.gh(f"repo fork {sourceRepository} --clone=false")
        self.gh(f"repo clone {repositoryName} {localDirectory}")
        self.localRepo = git.Repo(localDirectory)
        self.ensureUpstreamExists()

        # D2: keep the fork's default branch current with upstream.  GitHub forks do not
        # auto-sync, so a pre-existing fork drifts behind every merge/release.  This is a
        # best-effort, server-side fast-forward (no --force, so it cannot clobber a diverged
        # fork) — only meaningful for a genuine fork (origin != upstream); the owner case and a
        # freshly created fork are already current.  A failure here never aborts: D1 below
        # guarantees the new branch starts from the latest upstream regardless.
        if forkExists:
            forkNameWithOwner = self.nameWithOwner("origin")
            upstreamNameWithOwner = self.nameWithOwner("upstream")
            if forkNameWithOwner and upstreamNameWithOwner and forkNameWithOwner != upstreamNameWithOwner:
                try:
                    self.gh(["repo", "sync", forkNameWithOwner, "--source", upstreamNameWithOwner])
                except Exception as e:
                    logging.warning(f"Could not sync fork {forkNameWithOwner} from {upstreamNameWithOwner}: {e}")

        originBranches = self.localRepo.remotes.origin.fetch()
        originBranchIDs = [ob.name for ob in originBranches]
        originBranchID = f"origin/{branchName}"

        # D1: fetch upstream so a new issue branch can be cut from the current upstream default
        # branch (the latest published baseline), not the fork's possibly-stale main.
        try:
            self.localRepo.remote("upstream").fetch()
        except Exception as e:
            logging.warning(f"Could not fetch upstream: {e}")

        localIssueBranch = None
        for branch in self.localRepo.branches:
            if branch.name == branchName:
                localIssueBranch = branch
                break

        logging.debug("Making new branch")
        if originBranchID in originBranchIDs:
            logging.debug("Checking out existing from origin")
            self.localRepo.git.execute(f"git checkout --track {originBranchID}".split())
        else:
            # D1: branch off upstream/main (latest published state).  Fall back to origin/main
            # only if upstream/main is somehow unavailable, preserving the prior behavior.
            base = "upstream/main"
            try:
                self.localRepo.git.rev_parse("--verify", base)
            except Exception:
                base = "origin/main"
            logging.debug("Nothing in origin for %s; creating it from %s", branchName, base)
            self.localRepo.git.checkout(base)
            self.localRepo.git.branch(branchName)
            self.localRepo.git.checkout(branchName)

        self.loadFromLocalRepository()

    def loadPR(self, pr, repoDirectory):
        branchName = pr['title']
        repoNameWithOwner = f"{pr['author']['login']}/{pr['repository']['name']}"
        localDirectory = os.path.join(repoDirectory, f"{pr['repository']['name']}-{branchName}")
        self.progressMethod(f"Loading PR from {repoNameWithOwner} into {localDirectory}")

        self.cacheOldVersion(localDirectory)

        self.gh(f"repo clone {repoNameWithOwner} {localDirectory}")
        self.localRepo = git.Repo(localDirectory)
        self.ensureUpstreamExists()
        self.localRepo.remotes.origin.fetch()
        self.localRepo.git.checkout(branchName)

        self.loadFromLocalRepository(configuration="reviewer")
        return True

    def loadRepoForRelease(self, repoData):
        repoName = repoData['name']
        repoNameWithOwner = repoData['nameWithOwner'] # this is owner/name
        localDirectory = os.path.join(self.localRepositoryDirectory(), repoName)

        self.cacheOldVersion(localDirectory)

        # clone the main repo, not a fork
        self.gh(f"repo clone {repoNameWithOwner} {localDirectory}")

        self.localRepo = git.Repo(localDirectory)
        self.localRepo.git.checkout("main")
        self.loadFromLocalRepository(remoteName="origin", configuration="release")
        return True

    def loadRepoForPreview(self, repoNameWithOwner):
        repoName = repoNameWithOwner.split('/')[1]
        localDirectory = os.path.join(self.localRepositoryDirectory(), repoName)

        self.cacheOldVersion(localDirectory)

        self.gh(f"repo clone {repoNameWithOwner} {localDirectory}")

        self.localRepo = git.Repo(localDirectory)
        self.localRepo.git.checkout("main")
        self.loadFromLocalRepository(remoteName="origin", configuration="preview")
        return True

    def loadFromLocalRepository(self, remoteName="upstream", configuration="segment"):
        localDirectory = self.localRepo.working_dir
        branchName = self.localRepo.active_branch.name
        remoteNameWithOwner = self.nameWithOwner(remoteName)

        self.progressMethod(f"Loading {branchName} into {localDirectory}")

        self.colorTableNode = None
        try:
            colorPath = glob.glob(f"{localDirectory}/*.csv")[0]
            self.colorTableNode = slicer.util.loadColorTable(colorPath)
        except IndexError:
            try:
                colorPath = glob.glob(f"{localDirectory}/*.ctbl")[0]
                self.colorTableNode = slicer.util.loadColorTable(colorPath)
            except IndexError:
                self.ghProgressMethod(f"No color table found")

        # TODO: move from single volume file to segmentation specification json
        volumePath = os.path.join(localDirectory, "source_volume")
        if not os.path.exists(volumePath):
            volumePath = os.path.join(localDirectory, "master_volume") # for backwards compatibility
        volumeRef = open(volumePath).read().strip()
        # The source_volume pointer is "releases/download/v1/{originalName}.nrrd";
        # remember the original name so the UI can display it.
        self.sourceVolumeName = os.path.basename(volumeRef).rsplit('.nrrd', 1)[0]
        volumeURL = self.resolveVolumeURL(volumeRef, remoteNameWithOwner)
        cacheDirectory = os.path.join(self.localRepositoryDirectory(), "MorphoDepotCaches", "Volumes")
        os.makedirs(cacheDirectory, exist_ok=True)
        nrrdPath = os.path.join(cacheDirectory, f"{remoteNameWithOwner.replace('/', '-')}-volume.nrrd")
        checksum = None
        checksumFilePath = os.path.join(localDirectory, "source_volume_checksum")
        if os.path.exists(checksumFilePath):
            with open(checksumFilePath) as fp:
                checksum = fp.read().strip()
        if not os.path.exists(nrrdPath):
            slicer.util.downloadFile(volumeURL, nrrdPath, checksum=checksum)
        volumeNode = slicer.util.loadVolume(nrrdPath)

        # Load all segmentations
        segmentationNodesByName = {}
        for segmentationPath in glob.glob(f"{localDirectory}/*.seg.nrrd"):
            name = os.path.split(segmentationPath)[1].split(".")[0]
            segmentationNodesByName[name] = slicer.util.loadSegmentation(segmentationPath)
        # Default for the New release "baseline segmentation" picker: prefer a segmentation
        # named "baseline" if present, otherwise fall back to whatever loaded.
        self.baselineSegmentationNode = (
            segmentationNodesByName.get("baseline")
            or (next(iter(segmentationNodesByName.values()), None))
        )

        if configuration in ("segment", "reviewer"):
            for segmentationNode in segmentationNodesByName.values():
                segmentationNode.GetDisplayNode().SetVisibility(False)

            # Switch to Segment Editor module
            pluginHandlerSingleton = slicer.qSlicerSubjectHierarchyPluginHandler.instance()
            pluginHandlerSingleton.pluginByName("Default").switchToModule("SegmentEditor")
            editorWidget = slicer.modules.segmenteditor.widgetRepresentation().self()

            self.segmentationPath = os.path.join(localDirectory, branchName) + ".seg.nrrd"
            if branchName in segmentationNodesByName.keys():
                self.segmentationNode = segmentationNodesByName[branchName]
                self.segmentationNode.GetDisplayNode().SetVisibility(True)
            else:
                self.segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
                self.segmentationNode.CreateDefaultDisplayNodes()
                self.segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
                self.segmentationNode.SetName(branchName)
                if "baseline" in segmentationNodesByName.keys():
                    baselineSegmentation = segmentationNodesByName["baseline"].GetSegmentation()
                    newSegmentation = self.segmentationNode.GetSegmentation()
                    for segmentID in baselineSegmentation.GetSegmentIDs():
                        newSegmentation.CopySegmentFromSegmentation(baselineSegmentation, segmentID)

            editorWidget.parameterSetNode.SetAndObserveSegmentationNode(self.segmentationNode)
            editorWidget.parameterSetNode.SetAndObserveSourceVolumeNode(volumeNode)

    def nameWithOwner(self, remote):
        branchName = self.localRepo.active_branch.name
        repo = self.localRepo.remote(name=remote)
        repoURL = list(repo.urls)[0]
        if repoURL.find("@") != -1:
            # git ssh prototocol
            repoURL = "/".join(repoURL.split(":"))
            repoNameWithOwner = "/".join(repoURL.split("/")[-2:]).split(".")[0]
        elif repoURL.startswith("https://"):
            # https protocol
            repoNameWithOwner = "/".join(repoURL.split("/")[-2:]).split(".")[0]
        elif repoURL.startswith("git@"):
            # git@github.com:owner/repo.git
            repoNameWithOwner = repoURL.split(":")[1].replace(".git", "")
        elif os.path.exists(repoURL):
            # local path
            # this case happens during repo creation before pushing to remote
            return None
        else:
            # https protocol
            repoNameWithOwner = "/".join(repoURL.split("/")[-2:]).split(".")[0]
        return repoNameWithOwner

    def issuePR(self, role="segmenter"):
        """Find the issue for the issue currently being worked on or None if there isn't one"""
        if role not in ("segmenter", "reviewer"):
            raise ValueError(f"Invalid role {role}")
        if not self.localRepo:
            return None
        branchName = self.localRepo.active_branch.name
        try:
            upstreamNameWithOwner = self.nameWithOwner("upstream")
        except ValueError:
            return None
        issuePR = None
        prs = self.prList(role=role)
        for pr in prs:
            prRepoNameWithOwner = pr['repository']['nameWithOwner']
            if prRepoNameWithOwner == upstreamNameWithOwner and pr['title'] == branchName:
                issuePR = pr
        return issuePR

    def cacheOldVersion(self, directoryPath):
        """If directoryPath exists, move it to an archive in the cache."""
        if os.path.exists(directoryPath):
            self.progressMethod(f"Archiving old version of {directoryPath}")
            cacheDirectory = os.path.join(self.localRepositoryDirectory(), "MorphoDepotCaches", "OldRepositories")
            os.makedirs(cacheDirectory, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            archiveName = f"{os.path.basename(directoryPath)}-{timestamp}"
            archivePath = os.path.join(cacheDirectory, archiveName)
            shutil.move(directoryPath, archivePath)

    def commitAndPush(self, message):
        """Create a PR if needed and push current segmentation
        Mark the PR as a draft
        """
        if not self.segmentationNode:
            return False
        if not slicer.util.saveNode(self.segmentationNode, self.segmentationPath, properties={'useCompression': True}):
            logging.error(f"Segmentation save failed: path is {self.segmentationPath}")
            return False
        self.localRepo.index.add([self.segmentationPath])
        self.localRepo.index.commit(message)

        branchName = self.localRepo.active_branch.name
        remote = self.localRepo.remote(name="origin")

        # rebase branch if it exists in case other changes have been made (e.g. on another machine)
        branchNames = [branch.name.split("/")[1] for branch in self.localRepo.remotes['origin'].refs]
        if branchName in branchNames:
            pullResult = self.localRepo.git.pull(f"--rebase", "origin", branchName)
            self.progressMethod(pullResult)

        # Workaround for missing origin.push().raise_if_error() in 3.1.14
        # (see https://github.com/gitpython-developers/GitPython/issues/621):
        # https://github.com/gitpython-developers/GitPython/issues/621
        pushInfoList = remote.push(branchName)
        for pi in pushInfoList:
            for flag in [pi.REJECTED, pi.REMOTE_REJECTED, pi.REMOTE_FAILURE, pi.ERROR]:
                if pi.flags & flag:
                    self.progressMethod(f"Push failed with {flag}")
                    return False

        # create a PR if needed
        if not self.issuePR():
            issueNumber = branchName.split("-")[1]
            upstreamNameWithOwner = self.nameWithOwner("upstream")
            originNameWithOwner = self.nameWithOwner("origin")
            originOwner = originNameWithOwner.split("/")[0]
            prBody = f"Fixes #{issueNumber}"
            if self.currentIssue and 'author' in self.currentIssue and 'login' in self.currentIssue['author']:
                authorLogin = self.currentIssue['author']['login']
                prBody = f"Started work on this issue for @{authorLogin}. {prBody}"
            commandList = f"""
                pr create
                --draft
                --repo {upstreamNameWithOwner}
                --base main
                --title {branchName}
                --head {originOwner}:{branchName}
            """.replace("\n"," ").split()
            commandList += ["--body", prBody]
            self.gh(commandList)
            self.ghTopicClearCache()
        return True

    def requestReview(self):
        pr = self.issuePR(role="segmenter")
        if not pr:
            logging.error("No pull request found for the current issue branch.")
            return

        upstreamNameWithOwner = self.nameWithOwner("upstream")
        self.gh(f"""
            pr ready {pr['number']}
                --repo {upstreamNameWithOwner}
            """)
        self.ghTopicClearCache()

    def requestChanges(self, message=""):
        pr = self.issuePR(role="reviewer")
        upstreamNameWithOwner = self.nameWithOwner("upstream")
        commandList = f"""
            pr review {pr['number']}
                --request-changes
                --repo {upstreamNameWithOwner}
        """.replace("\n"," ").split()
        if message != "":
            commandList += ["--body", message]
        self.gh(commandList)
        self.gh(f"""
            pr ready {pr['number']}
                --undo
                --repo {upstreamNameWithOwner}
            """)
        self.ghTopicClearCache()

    def approvePR(self, message=""):
        pr = self.issuePR(role="reviewer")
        upstreamNameWithOwner = self.nameWithOwner("upstream")
        # TODO: this if the reviewer is also the creator of the PR
        # this generates an error from github that you aren't allowed
        # to approve your own PRs, but it's just a warning in this case.
        # Checking the name to avoid the approval or just skipping
        # approval since we are closing the PR anyway would be fine.
        commandList = f"""
            pr review {pr['number']}
                --approve
                --repo {upstreamNameWithOwner}
        """.replace("\n"," ").split()
        if message != "":
            commandList += ["--body", message]
        self.gh(commandList)
        commandList = f"""
            pr merge {pr['number']}
                --repo {upstreamNameWithOwner}
                --squash
        """.replace("\n"," ").split()
        commandList += ["--body", "Merging and closing"]
        self.gh(commandList)
        self.ghTopicClearCache()

    def getReleases(self):
        """Get list of releases for the current repository (latest first)."""
        if not self.localRepo:
            return None
        originNameWithOwner = self.nameWithOwner("origin")
        return self.ghJSON(f"release list --repo {originNameWithOwner} --json name,tagName,publishedAt")

    def closedIssuesSinceLastRelease(self, nameWithOwner):
        """Return a list of {number,title} for issues closed since the last published release.
        If there is no prior release, returns all closed issues for the repo."""
        releases = self.ghJSON(f"release list --repo {nameWithOwner} --json tagName,publishedAt") or []
        sinceDate = releases[0].get('publishedAt') if releases else None
        if sinceDate:
            cmd = ["issue", "list", "--repo", nameWithOwner,
                   "--json", "number,title",
                   "--search", f"is:issue is:closed closed:>{sinceDate}"]
        else:
            cmd = ["issue", "list", "--repo", nameWithOwner, "--state", "closed",
                   "--json", "number,title"]
        return self.ghJSON(cmd) or []

    def nextReleaseTag(self):
        """Compute the next vN tag based on existing releases. Returns None if no repo loaded."""
        if not self.localRepo:
            return None
        releases = self.getReleases() or []
        nextVersion = 1
        tagNames = [r['tagName'] for r in releases if r['tagName'].startswith('v')]
        versions = [int(t[1:]) for t in tagNames if t[1:].isdigit()]
        if versions:
            nextVersion = max(versions) + 1
        return f"v{nextVersion}"

    def previousReleaseTag(self):
        """Latest existing release tag, or None if no releases yet."""
        if not self.localRepo:
            return None
        releases = self.getReleases() or []
        tagNames = [r['tagName'] for r in releases if r['tagName'].startswith('v')]
        versions = [int(t[1:]) for t in tagNames if t[1:].isdigit()]
        if not versions:
            return None
        return f"v{max(versions)}"

    def existingScreenshotCount(self):
        """Highest existing screenshot-N.png index in the loaded repo's screenshots/ directory."""
        if not self.localRepo:
            return 0
        screenshotsDir = os.path.join(self.localRepo.working_dir, "screenshots")
        if not os.path.exists(screenshotsDir):
            return 0
        highest = 0
        for entry in os.listdir(screenshotsDir):
            match = re.match(r"^screenshot-(\d+)\.png$", entry)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest

    def releaseSnapshotPlan(self, newTag, baselineNode, colorTableNode, screenshots):
        """Build a description of what prepareReleaseSnapshot will do, for the confirmation UI."""
        if not self.localRepo:
            return None
        repoDir = self.localRepo.working_dir
        previousTag = self.previousReleaseTag()
        issueSegFiles = sorted(os.path.basename(p) for p in glob.glob(os.path.join(repoDir, "issue-*.seg.nrrd")))
        startIndex = self.existingScreenshotCount() + 1
        return {
            'newTag': newTag,
            'previousTag': previousTag,
            'baselineName': baselineNode.GetName() if baselineNode else None,
            'colorTableName': colorTableNode.GetName() if colorTableNode else None,
            'newScreenshotNames': [f"screenshot-{startIndex + i}.png" for i in range(len(screenshots or []))],
            'issueSegFiles': issueSegFiles,
            'archivedReadme': f"README-{previousTag}.md" if previousTag else None,
        }

    def generateReleaseReadme(self, newTag, newScreenshotEntries):
        """Build a new README.md body for the given release. Reuses metadata from
        MorphoDepotAccession.json. Links each previous release to its tag's tree on
        GitHub so the reader sees the repository at that release stage. Lists only
        screenshots from this release flow (older screenshots stay in their archived READMEs)."""
        repoDir = self.localRepo.working_dir
        accessionPath = os.path.join(repoDir, "MorphoDepotAccession.json")
        accession = {}
        if os.path.exists(accessionPath):
            try:
                with open(accessionPath) as f:
                    accession = json.load(f)
            except Exception as e:
                logging.warning(f"Could not parse MorphoDepotAccession.json: {e}")

        def field(key, default=""):
            v = accession.get(key)
            if isinstance(v, list) and len(v) >= 2:
                return v[1] or default
            return v or default

        species = field('species', "Unknown species")
        modality = field('modality', "Unknown")
        contrast = field('contrastEnhancement', "Unknown")
        scanDimensions = accession.get('scanDimensions', "Unknown")
        scanSpacing = accession.get('scanSpacing', "Unknown")

        try:
            originNameWithOwner = self.nameWithOwner("origin")
        except Exception:
            originNameWithOwner = None

        lines = [f"# Release {newTag}", ""]

        previousReadmes = sorted(
            glob.glob(os.path.join(repoDir, "README-v*.md")),
            key=lambda p: int(os.path.basename(p).replace("README-v", "").replace(".md", "")),
        )
        versions = []
        for p in previousReadmes:
            nStr = os.path.basename(p).replace("README-v", "").replace(".md", "")
            if nStr.isdigit():
                versions.append(int(nStr))
        newTagN = int(newTag[1:]) if newTag.startswith('v') and newTag[1:].isdigit() else None
        if versions:
            lines.append("## Previous releases")
            # Each previous version vN links to the pre-release-v(N+1) branch's README.md:
            # that branch captured main right before v(N+1) was prepped, i.e. the README as
            # it was while vN was the most recent release (and reflects any edits made to
            # README.md between vN and v(N+1)).
            for i, v in enumerate(versions):
                nextN = versions[i + 1] if i + 1 < len(versions) else newTagN
                if originNameWithOwner and nextN is not None:
                    url = f"https://github.com/{originNameWithOwner}/blob/pre-release-v{nextN}/README.md"
                    lines.append(f"- [v{v}]({url})")
                else:
                    lines.append(f"- [v{v}](README-v{v}.md)")
            lines.append("")

        lines.append("## MorphoDepot Repository")
        lines.append("Repository for segmentation of a specimen scan. See [this JSON file](MorphoDepotAccession.json) for specimen details.")
        lines.append(f"* Species: {species}")
        lines.append(f"* Modality: {modality}")
        lines.append(f"* Contrast: {contrast}")
        lines.append(f"* Dimensions: {scanDimensions}")
        lines.append(f"* Spacing (mm): {scanSpacing}")

        if newScreenshotEntries:
            lines.append("")
            lines.append(f"## Screenshots for {newTag}")
            for name, caption in newScreenshotEntries:
                altText = caption or name
                lines.append(f"![{altText}](screenshots/{name})")
                if caption:
                    lines.append(f"_{caption}_")

        return "\n".join(lines) + "\n"

    def prepareReleaseSnapshot(self, newTag, baselineNode, colorTableNode, screenshots):
        """Stage the working tree for a release tag: write baseline.seg.nrrd from the picked
        segmentation, overwrite the repo's color table with the picked one, rotate README.md
        to README-{previousTag}.md and generate a fresh README.md, append new screenshots
        with sequential numbering (and update screenshots/captions.json), drop issue-*.seg.nrrd
        from the working tree (still in git history). Then commit and push to origin/main."""
        if not self.localRepo:
            return None
        repoDir = self.localRepo.working_dir
        previousTag = self.previousReleaseTag()

        # Baseline segmentation
        baselinePath = os.path.join(repoDir, "baseline.seg.nrrd")
        if not slicer.util.saveNode(baselineNode, baselinePath, properties={'useCompression': True}):
            raise RuntimeError(f"Failed to save baseline segmentation to {baselinePath}")

        # Color table — overwrite the existing repo color file (.csv preferred over .ctbl).
        csvPaths = glob.glob(f"{repoDir}/*.csv")
        ctblPaths = glob.glob(f"{repoDir}/*.ctbl")
        if csvPaths:
            colorTablePath = csvPaths[0]
        elif ctblPaths:
            colorTablePath = ctblPaths[0]
        else:
            colorTablePath = os.path.join(repoDir, f"{colorTableNode.GetName()}.csv")
        if not slicer.util.saveNode(colorTableNode, colorTablePath):
            raise RuntimeError(f"Failed to save color table to {colorTablePath}")

        # New screenshots — continue sequential numbering from existing files.
        newScreenshotEntries = []
        if screenshots:
            screenshotsDir = os.path.join(repoDir, "screenshots")
            os.makedirs(screenshotsDir, exist_ok=True)
            startIndex = self.existingScreenshotCount() + 1
            captionsPath = os.path.join(screenshotsDir, "captions.json")
            captions = {}
            if os.path.exists(captionsPath):
                try:
                    with open(captionsPath) as f:
                        captions = json.load(f)
                except Exception:
                    captions = {}
            for i, ss in enumerate(screenshots):
                name = f"screenshot-{startIndex + i}.png"
                shutil.copy(ss['path'], os.path.join(screenshotsDir, name))
                captions[name] = ss.get('caption', '')
                newScreenshotEntries.append((name, ss.get('caption', '')))
            with open(captionsPath, "w") as f:
                json.dump(captions, f, indent=2)

        # README rotation + fresh README for the new tag
        readmePath = os.path.join(repoDir, "README.md")
        if previousTag and os.path.exists(readmePath):
            shutil.move(readmePath, os.path.join(repoDir, f"README-{previousTag}.md"))
        with open(readmePath, "w") as f:
            f.write(self.generateReleaseReadme(newTag, newScreenshotEntries))

        # Drop per-issue segmentations from the working tree (kept in history)
        for path in glob.glob(os.path.join(repoDir, "issue-*.seg.nrrd")):
            os.remove(path)

        # Stage everything (added, modified, deleted), commit, push.
        self.localRepo.git.add("--all")
        self.localRepo.index.commit(f"Prepare release {newTag}")
        self.localRepo.remote(name="origin").push("main")

    def resetToReleaseBackup(self):
        """Hard-reset main to the pre-release archive branch and discard untracked changes.
        Local-only: does NOT undo anything that was already pushed to origin/main, and does
        NOT delete the archive branch (it is intentionally kept as a permanent record)."""
        backup = getattr(self, 'releaseBackupBranch', None)
        if not (self.localRepo and backup):
            return False
        self.localRepo.git.reset("--hard", backup)
        self.localRepo.git.clean("-fd")
        self.releaseBackupBranch = None
        return True

    def discardReleaseBackup(self):
        """Clear the in-memory reference to the archive branch after a successful release.
        The branch itself is intentionally kept (locally and on origin) as a permanent
        archive of the pre-release state."""
        self.releaseBackupBranch = None

    def createRelease(self, releaseNotes="", baselineSegmentationNode=None, colorTableNode=None, screenshots=None):
        """Create a new release: create and push a pre-release-{tag} archive branch
        capturing main as it is right now (so per-issue segmentations and any other
        about-to-be-removed files stay browsable on GitHub), prepare the working tree
        (baseline, color table, README rotation, drop issue segmentations, screenshots),
        commit, push to origin/main, then create the gh release tag at that commit.
        On exception the archive branch is left in place so resetToReleaseBackup can
        roll back the local repo. Returns the new tag on success."""
        if not self.localRepo:
            return None
        if baselineSegmentationNode is None or colorTableNode is None:
            raise RuntimeError("A baseline segmentation and color table must both be selected.")
        tag = self.nextReleaseTag()
        if tag is None:
            return None

        backupName = f"pre-release-{tag}"
        # Create the archive branch locally and push it before any working-tree changes
        # so the pre-release state is preserved on the remote even if later steps fail.
        self.localRepo.git.branch(backupName)
        self.releaseBackupBranch = backupName
        self.localRepo.remote(name="origin").push(backupName)

        self.prepareReleaseSnapshot(tag, baselineSegmentationNode, colorTableNode, screenshots or [])

        originNameWithOwner = self.nameWithOwner("origin")
        if releaseNotes == "":
            releaseNotes = f"Version {tag} release."
        commandList = ["release", "create", tag, "--repo", originNameWithOwner]
        commandList += ["--notes", releaseNotes]
        self.gh(commandList)
        return tag

    def openIssuesAndPRs(self, nameWithOwner):
        """Return (issues, prs) lists of open items for the given repo, each with number and title."""
        issues = self.ghJSON(f"issue list --repo {nameWithOwner} --state open --json number,title")
        prs = self.ghJSON(f"pr list --repo {nameWithOwner} --state open --json number,title")
        return issues, prs

    def announceUpcomingRelease(self, nameWithOwner, deadlineISO, message):
        """Announce an upcoming release.  Posts a comment on every open issue and PR (so people
        who watch notifications are pinged) AND creates a dedicated, pinned, `release-pending`
        announcement issue (so people who only visit the repo see it).  The announcement issue
        body carries an invisible marker encoding the target tag and deadline, which is the
        repo-state signal `findReleaseAnnouncement` later reads.  Substitutes {deadline} in the
        message body. Returns (issueCount, prCount)."""
        issues, prs = self.openIssuesAndPRs(nameWithOwner)
        body = message.replace("{deadline}", deadlineISO)
        for issue in issues:
            n = str(issue['number'])
            self.progressMethod(f"Announcement on issue #{n}: {issue['title']}")
            self.gh(["issue", "comment", n, "--repo", nameWithOwner, "--body", body])
        for pr in prs:
            n = str(pr['number'])
            self.progressMethod(f"Announcement on PR #{n}: {pr['title']}")
            self.gh(["pr", "comment", n, "--repo", nameWithOwner, "--body", body])
        self._createReleaseAnnouncementIssue(nameWithOwner, deadlineISO, body)
        return len(issues), len(prs)

    def _releaseAnnounceMarker(self, tag, deadlineISO):
        """Invisible HTML-comment marker embedded in the announcement issue body."""
        return f"<!-- {self.releaseAnnounceMarkerName} tag={tag} deadline={deadlineISO} -->"

    def _ensureReleasePendingLabel(self, nameWithOwner):
        """Create (or update) the release-pending label; idempotent via --force."""
        try:
            self.gh(["label", "create", self.releasePendingLabel, "--repo", nameWithOwner,
                     "--color", "FBCA04",
                     "--description", "An upcoming release has been announced but not yet cut",
                     "--force"])
        except Exception as e:
            logging.warning(f"Could not ensure '{self.releasePendingLabel}' label: {e}")

    def _createReleaseAnnouncementIssue(self, nameWithOwner, deadlineISO, body):
        """Create a pinned, release-pending announcement issue carrying the deadline marker.
        Best-effort: a failure here must not abort the announcement comments above."""
        tag = self.nextReleaseTag() or ""
        self._ensureReleasePendingLabel(nameWithOwner)
        marker = self._releaseAnnounceMarker(tag, deadlineISO)
        title = f"Upcoming release {tag} - finish by {deadlineISO}".strip()
        try:
            url = self.gh(["issue", "create", "--repo", nameWithOwner,
                           "--title", title, "--body", f"{body}\n\n{marker}",
                           "--label", self.releasePendingLabel])
        except Exception as e:
            logging.warning(f"Could not create announcement issue: {e}")
            return None
        number = url.strip().rstrip("/").split("/")[-1]
        try:
            self.gh(["issue", "pin", number, "--repo", nameWithOwner])
        except Exception as e:
            logging.warning(f"Could not pin announcement issue #{number}: {e}")
        return number

    def findReleaseAnnouncement(self, nameWithOwner):
        """Return {'number', 'deadline'} for the open release-pending announcement issue, or None.
        Reads repo state only; returns None on any error (e.g. the label does not exist yet)."""
        try:
            items = self.ghJSON(["issue", "list", "--repo", nameWithOwner, "--state", "open",
                                 "--label", self.releasePendingLabel, "--json", "number,body"])
        except Exception:
            return None
        for item in items or []:
            body = item.get("body", "") or ""
            match = re.search(re.escape(self.releaseAnnounceMarkerName) + r"\b([^>]*)", body)
            if match:
                deadlineMatch = re.search(r"deadline=(\S+)", match.group(1))
                return {"number": item["number"],
                        "deadline": deadlineMatch.group(1) if deadlineMatch else None}
        return None

    def clearReleaseAnnouncement(self, nameWithOwner, tag=None):
        """Retire the pre-release announcement after a release: unpin it, remove the
        release-pending label, comment, and close it.  Best-effort and idempotent."""
        announcement = self.findReleaseAnnouncement(nameWithOwner)
        if not announcement:
            return
        n = str(announcement["number"])
        message = (f"Release {tag} has been published; closing this announcement." if tag
                   else "The release has been published; closing this announcement.")
        for command in (
            ["issue", "unpin", n, "--repo", nameWithOwner],
            ["issue", "edit", n, "--repo", nameWithOwner, "--remove-label", self.releasePendingLabel],
            ["issue", "comment", n, "--repo", nameWithOwner, "--body", message],
            ["issue", "close", n, "--repo", nameWithOwner],
        ):
            try:
                self.gh(command)
            except Exception as e:
                logging.warning(f"Announcement cleanup step failed ({command[1]}): {e}")

    def closeOpenItemsForRelease(self, nameWithOwner, version, message=None):
        """Comment on and close every open issue and PR. PRs are closed without merging.
        Returns (issueCount, prCount)."""
        if message is None:
            message = (
                f"Release {version} has been published. This will be closed.\n"
                f"Open a new issue or PR on the updated baseline to continue contributing."
            )
        issues, prs = self.openIssuesAndPRs(nameWithOwner)
        for issue in issues:
            n = str(issue['number'])
            self.progressMethod(f"Closing issue #{n}: {issue['title']}")
            self.gh(["issue", "comment", n, "--repo", nameWithOwner, "--body", message])
            self.gh(["issue", "close", n, "--repo", nameWithOwner])
        for pr in prs:
            n = str(pr['number'])
            self.progressMethod(f"Closing PR #{n}: {pr['title']}")
            self.gh(["pr", "comment", n, "--repo", nameWithOwner, "--body", message])
            self.gh(["pr", "close", n, "--repo", nameWithOwner])
        return len(issues), len(prs)

    def _resolveSpeciesString(self, accessionData, fallbackSpecies=""):
        """Determine the species string from accession data — via the iDigBio record when the
        specimen is accessioned there, otherwise the directly-entered species field.  Shared by
        create (_stageRepoFiles) and edit (saveStagedRepoEdits) so the README/topic stay
        consistent.  Robust to an unavailable/empty iDigBio response (network error, bad
        specimen id, service down): it never raises, and on the edit path `fallbackSpecies`
        (the species already recorded for the repo) is used so a transient outage does not
        blank a previously-resolved species."""
        if accessionData.get('iDigBioAccessioned', ['', ''])[1] == "Yes":
            idigbioURL = accessionData.get('iDigBioURL', ['', ''])[1]
            specimenID = idigbioURL.split("/")[-1] if idigbioURL else ""
            try:
                import idigbio
                api = idigbio.json()
                idigbioData = api.view("records", specimenID) if specimenID else None
                data = idigbioData.get('data') if isinstance(idigbioData, dict) else None
                if data:
                    if 'ala:species' in data:
                        return data['ala:species']
                    if 'dwc:scientificName' in data:
                        return data['dwc:scientificName']
                logging.warning(f"Could not find species for '{idigbioURL}' (response: {idigbioData})")
            except Exception as e:
                logging.warning(f"iDigBio species lookup failed for '{idigbioURL}': {e}")
            # Lookup unavailable/empty: keep the already-recorded species (edit), then the
            # directly-entered field, then a safe default — never crash, never blindly blank.
            return fallbackSpecies or accessionData.get('species', ['', ''])[1] or "Unknown species"
        return accessionData.get('species', ['', ''])[1]

    def _writeLicense(self, repoDir, accessionData):
        """Write LICENSE.txt for the chosen Creative Commons license."""
        if accessionData["license"][1].startswith("CC BY-NC"):
            licenseURL = "https://creativecommons.org/licenses/by-nc/4.0/legalcode.txt"
        else:
            licenseURL = "https://creativecommons.org/licenses/by/4.0/legalcode.txt"
        response = requests.get(licenseURL)
        with open(os.path.join(repoDir, "LICENSE.txt"), "w") as fp:
            fp.write(response.content.decode('ascii', errors="ignore"))

    def _renderReadme(self, accessionData, speciesString, screenshotItems=None):
        """Build README.md text from accession data.  screenshotItems is an ordered list of
        (filename, caption) for images under the screenshots/ directory."""
        readme_content = f"""
## MorphoDepot Repository
Repository for segmentation of a specimen scan.  See [this JSON file](MorphoDepotAccession.json) for specimen details.
* Species: {speciesString}
* Modality: {accessionData['modality'][1]}
* Contrast: {accessionData['contrastEnhancement'][1]}
* Dimensions: {accessionData['scanDimensions']}
* Spacing (mm): {accessionData['scanSpacing']}
"""
        if screenshotItems:
            readme_content += "\n\n## Screenshots\n"
            for filename, caption in screenshotItems:
                readme_content += f"\n![{caption or filename}](screenshots/{filename})\n"
                if caption:
                    readme_content += f"_{caption}_\n"
        return readme_content

    def _readScreenshotCaptions(self, repoDir):
        """Read an existing repo's screenshots/captions.json into an ordered list of
        (filename, caption) so the README can be regenerated without the live screenshots."""
        captionsPath = os.path.join(repoDir, "screenshots", "captions.json")
        if not os.path.exists(captionsPath):
            return []
        try:
            with open(captionsPath) as fp:
                captions = json.load(fp)
        except Exception:
            return []
        return [(name, captions[name]) for name in sorted(captions.keys())]

    def _speciesFromReadme(self, repoDir):
        """Extract the already-recorded species from a repo's committed README (the
        '* Species: ...' line).  Used as the fallback when re-resolving species on edit so a
        transient iDigBio outage cannot blank a previously-resolved species."""
        readmePath = os.path.join(repoDir, "README.md")
        if not os.path.exists(readmePath):
            return ""
        try:
            with open(readmePath) as fp:
                for line in fp:
                    if line.strip().startswith("* Species:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    # Optional, opt-in at Create (gated on the `workflow` token scope): a GitHub Actions
    # workflow that assigns each newly opened issue back to whoever opened it.  Uses the
    # auto-provisioned GITHUB_TOKEN — no secrets to configure.  Verified to assign even
    # non-collaborator authors on public repos.
    autoAssignWorkflow = """name: Auto-assign issue to creator

# When a new issue is opened, assign it to the person who opened it.
# Uses the built-in GITHUB_TOKEN (auto-provisioned per run) — no secrets to configure.
on:
  issues:
    types: [opened]

jobs:
  assign:
    runs-on: ubuntu-latest
    permissions:
      issues: write
    steps:
      - name: Assign the new issue to its author
        uses: actions/github-script@v7
        with:
          script: |
            const author = context.payload.issue.user.login;
            const res = await github.rest.issues.addAssignees({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.payload.issue.number,
              assignees: [author],
            });
            const assigned = (res.data.assignees || []).map(a => a.login);
            if (!assigned.includes(author)) {
              core.warning(`'${author}' was not assigned (likely not an assignable user on this repo).`);
            }
"""

    def _stageRepoFiles(self, repoDir, sourceVolume, colorTable, accessionData, sourceSegmentation=None, screenshots=None, useOrg=False, targetOwner=None, enableAutoAssign=False):
        """Build the repository content on disk: save every file (including the CURATOR
        file), `git init`, and make the initial commit.  No GitHub interaction beyond the
        non-GitHub license/iDigBio lookups.  `enableAutoAssign` additionally writes a GitHub
        Actions workflow that assigns new issues back to their creator (opt-in, scope-gated in
        the UI).  Returns a build context dict consumed by provisionStagedRepo()."""

        # The CURATOR is the person creating the repo and is responsible for reviewing its
        # segmentation PRs.  It is always the creator, regardless of where the repo ends up.
        curator = self.whoami()
        repoName = os.path.basename(repoDir.rstrip(os.sep))

        os.makedirs(repoDir)

        # save data
        repoFileNames = []
        sourceFileName = sourceVolume.GetName()
        sourceFilePath = os.path.join(repoDir, sourceFileName) + ".nrrd"
        slicer.util.saveNode(sourceVolume, sourceFilePath, properties={'useCompression': True})

        # Size cap depends on tier: members upload to S3 via multipart (10 GiB cap); non-members
        # store the volume as a GitHub release asset (2 GiB limit). See docs/ObjectStorage-model.md.
        sourceBytes = os.path.getsize(sourceFilePath)
        sizeGB = sourceBytes / 2**30
        if useOrg:
            if sourceBytes > 10 * 2**30:
                raise ValueError(
                    f"This volume ({sizeGB:.1f} GB) exceeds the 10 GB limit; volumes that large are "
                    "not currently supported. Crop or resample the volume.")
        elif sourceBytes > 2 * 2**30:
            if self.userIsOrgMember():
                raise ValueError(
                    f"This volume ({sizeGB:.1f} GB) exceeds the 2 GB limit for personal "
                    "repositories. Create it under MorphoDepot instead — the org supports up to 10 GB.")
            raise ValueError(
                f"This volume is {sizeGB:.1f} GB. Personal repositories cap files at 2 GB (a GitHub "
                "restriction). To publish volumes up to 10 GB, join the MorphoDepot organization and "
                "create your repository there.")

        # calculate and save checksum
        checksum = slicer.util.computeChecksum('SHA256', sourceFilePath)
        checksumFilePath = os.path.join(repoDir, "source_volume_checksum")
        with open(checksumFilePath, "w") as fp:
            fp.write(f"SHA256:{checksum}")

        colorTableName = colorTable.GetName()
        slicer.util.saveNode(colorTable, os.path.join(repoDir, colorTableName) + ".csv")
        repoFileNames.append(f"{colorTableName}.csv")

        # write accessionData file
        accessionData['fileFormatVersion'] = MorphoDepotLogic.accessionFileFormatVersion
        fp = open(os.path.join(repoDir, "MorphoDepotAccession.json"), "w")
        fp.write(json.dumps(accessionData, indent=4))
        fp.close()

        # write license file
        self._writeLicense(repoDir, accessionData)

        speciesString = self._resolveSpeciesString(accessionData)
        speciesTopicString = speciesString.lower().replace(" ", "-")

        # write readme file
        screenshotItems = None
        if screenshots:
            screenshotItems = [(f"screenshot-{i+1}.png", ss['caption']) for i, ss in enumerate(screenshots)]
        readme_content = self._renderReadme(accessionData, speciesString, screenshotItems)
        fp = open(os.path.join(repoDir, "README.md"), "w")
        fp.write(readme_content)
        fp.close()

        # write CURATOR file: the GitHub handle of the person responsible for curating this
        # repository and reviewing its segmentation PRs.  Always the creator, regardless of
        # whether the repo eventually lives under a personal account or an organization.
        with open(os.path.join(repoDir, "CURATOR"), "w") as fp:
            fp.write(f"{curator}\n")

        # create initial repo
        repo = git.Repo.init(repoDir, initial_branch='main')

        repoFileNames += [
            "README.md",
            "LICENSE.txt",
            "MorphoDepotAccession.json",
            "source_volume_checksum",
            "CURATOR",
        ]
        if sourceSegmentation:
            segmentationName = "baseline" # keyword used to detect segmentation to import when startin new issue
            slicer.util.saveNode(sourceSegmentation, os.path.join(repoDir, segmentationName) + ".seg.nrrd")
            repoFileNames.append(f"{segmentationName}.seg.nrrd")

        if screenshots:
            # Copy screenshots
            screenshotsDir = os.path.join(repoDir, "screenshots")
            os.makedirs(screenshotsDir, exist_ok=True)
            for i, screenshotInfo in enumerate(screenshots):
                newScreenshotName = f"screenshot-{i+1}.png"
                newScreenshotPath = os.path.join(screenshotsDir, newScreenshotName)
                shutil.copy(screenshotInfo['path'], newScreenshotPath)
                repoFileNames.append(os.path.join("screenshots", newScreenshotName))

            # Save captions to a file
            captions = {f"screenshot-{i+1}.png": ss['caption'] for i, ss in enumerate(screenshots)}
            captionsPath = os.path.join(screenshotsDir, "captions.json")
            with open(captionsPath, "w") as f:
                json.dump(captions, f, indent=2)
            repoFileNames.append(os.path.join("screenshots", "captions.json"))
        # Optional auto-assign workflow (opt-in at Create, only when the token has `workflow`
        # scope — gated in the UI).  Committed like any other file so it survives the staged
        # flow's history rewrites and ships with the published repo.
        if enableAutoAssign:
            workflowDir = os.path.join(repoDir, ".github", "workflows")
            os.makedirs(workflowDir, exist_ok=True)
            with open(os.path.join(workflowDir, "auto-assign.yml"), "w") as fp:
                fp.write(MorphoDepotLogic.autoAssignWorkflow)
            repoFileNames.append(os.path.join(".github", "workflows", "auto-assign.yml"))

        repoFilePaths = [os.path.join(repoDir, fileName) for fileName in repoFileNames]
        repo.index.add(repoFilePaths)
        repo.index.commit("Initial commit")

        # Best-effort duplicate-volume check while the staging progress is already up, so the
        # network round-trip is hidden; surfaced passively in the Go-live status and at publish.
        # Skipped for the developer self-test (targetOwner) — test volumes are not real data.
        duplicateRepos = [] if targetOwner else self.duplicateVolumeRepos(checksum)

        return {
            "repoDir": repoDir,
            "repo": repo,
            "curator": curator,
            "repoName": repoName,
            "sourceFilePath": sourceFilePath,
            "sourceFileName": sourceFileName,
            "checksum": checksum,
            "duplicateRepos": duplicateRepos,
            "useOrg": useOrg,
            "targetOwner": targetOwner,
            "speciesTopicString": speciesTopicString,
            "species": speciesString,
            "committedFiles": repoFileNames,
        }

    def createAccessionRepo(self, sourceVolume, colorTable, accessionData, sourceSegmentation=None, screenshots=None, useOrg=None, targetOwner=None, enableAutoAssign=False):
        """Stage a new accession repository: build it locally, then provision it.  `useOrg`
        chooses the destination — True = born in the MorphoDepot org (members only; S3, 10 GB,
        governed); False = the creator's personal account (GitHub release asset, 2 GB, fully
        theirs).  When unspecified, defaults to the org for members.  `targetOwner` (set only by
        the developer self-test) overrides routing: provision directly into that org via the
        creator's own gh rights, non-member style (release asset, no App/S3).  Returns the staged
        nameWithOwner."""
        repoName = accessionData['githubRepoName'][1].split("/")[-1]
        if useOrg is None:
            useOrg = self.userIsOrgMember()
        if targetOwner:
            useOrg = False  # targetOwner forces the direct-to-org (non-member-style) path

        # Fail fast on a GitHub name collision (the authoritative check) BEFORE building
        # anything locally, so we never half-build for a name that is already taken.
        curator = self.whoami()
        # Collision-check the namespace the repo will actually land in.
        if targetOwner:
            target, where = f"{targetOwner}/{repoName}", f"the {targetOwner} organization"
        elif useOrg:
            target, where = f"{self.morphoDepotOrg}/{repoName}", f"the {self.morphoDepotOrg} organization"
        else:
            target, where = f"{curator}/{repoName}", f"your account ({curator})"
        if self.repoExists(target):
            raise ValueError(
                f"A repository named '{repoName}' already exists in {where}. "
                "If it is a repo you staged earlier but never published, reopen the MorphoDepot "
                "module to resume or discard it, or delete it on GitHub; otherwise choose a "
                "different name.")

        # Local clones are disposable working copies.  Clear any stale leftover (e.g. from a
        # previous interrupted attempt) so it never blocks a fresh build with the same name.
        repoDir = os.path.join(self.localRepositoryDirectory(), repoName)
        if os.path.exists(repoDir):
            self.progressMethod(f"Removing stale local directory {repoDir}")
            shutil.rmtree(repoDir, ignore_errors=True)

        buildContext = self._stageRepoFiles(repoDir, sourceVolume, colorTable, accessionData,
                                            sourceSegmentation, screenshots, useOrg=useOrg,
                                            targetOwner=targetOwner, enableAutoAssign=enableAutoAssign)
        return self.provisionStagedRepo(buildContext)

    def _provisionStagedRepoInOrg(self, buildContext):
        """Member tier: the App creates the repo IN-ORG (private, staged topic, {handle}-team
        Write); we push the built content and upload the source volume to S3.  No personal
        account, no transfer.  Returns the org nameWithOwner (MorphoDepot/<name>)."""
        repoDir = buildContext["repoDir"]
        repo = buildContext["repo"]
        curator = buildContext["curator"]
        repoName = buildContext["repoName"]
        sourceFilePath = buildContext["sourceFilePath"]
        sourceFileName = buildContext["sourceFileName"]
        checksum = buildContext["checksum"]

        self.progressMethod(f"Creating {self.morphoDepotOrg}/{repoName} (private) via the App...")
        info = self.controlPlaneRequest("repos/create", {"name": repoName})
        nameWithOwner = info["full_name"]
        cloneURL = info["clone_url"]

        # Push the locally built content to the empty in-org repo (member has Write via team).
        self.localRepo = repo
        try:
            repo.create_remote("origin", cloneURL)
        except Exception:
            repo.remote(name="origin").set_url(cloneURL)
        branchName = repo.active_branch.name
        lastError = None
        for delay in (0, 2, 4, 8, 16):
            if delay:
                self.progressMethod(f"Push retry in {delay}s...")
                time.sleep(delay)
            try:
                repo.git.push("--set-upstream", "origin", branchName)
                lastError = None
                break
            except Exception as pushError:
                lastError = pushError
        if lastError is not None:
            raise RuntimeError(f"Push to {nameWithOwner} failed: {lastError}")

        owner, name = nameWithOwner.split("/", 1)
        try:
            self.gh(f"api --method PUT /repos/{owner}/{name}/subscription --field subscribed=true --field ignored=false")
        except Exception as e:
            logging.warning(f"Could not subscribe to {nameWithOwner}: {e}")

        # v1 release (version anchor, no asset)
        self.gh(["release", "create", "--repo", nameWithOwner, "v1", "--notes", "Initial release"])

        # Upload the source volume to S3 and record the absolute public URL.
        publicURL = self.uploadSourceVolumeToObjectStore(
            sourceFilePath, checksum, curator, repoName, f"{sourceFileName}.nrrd")
        with open(os.path.join(repoDir, "source_volume"), "w") as fp:
            fp.write(publicURL)
        repo.index.add([f"{repoDir}/source_volume"])
        repo.index.commit("Add source file url file")
        repo.remote(name="origin").push()

        self.stagingContext = {
            "repoDir": repoDir,
            "personalNameWithOwner": nameWithOwner,
            "repoName": repoName,
            "curator": curator,
            "speciesTopicString": buildContext["speciesTopicString"],
            "checksum": checksum,
            "duplicateRepos": buildContext.get("duplicateRepos", []),
            "isMember": True,
        }
        self.localRepo = None
        if os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        return nameWithOwner

    def provisionStagedRepo(self, buildContext):
        """Provision the staged repo.  Members: born in-org via the App control plane
        (_provisionStagedRepoInOrg).  Non-members: PRIVATE on the creator's personal account
        (the path below).  The developer self-test (`targetOwner`) uses this same non-member path
        but creates directly in the testing org via the creator's own gh rights.  Records staging
        state and returns the nameWithOwner."""
        if buildContext.get("useOrg"):
            return self._provisionStagedRepoInOrg(buildContext)
        repoDir = buildContext["repoDir"]
        repo = buildContext["repo"]
        curator = buildContext["curator"]
        repoName = buildContext["repoName"]
        sourceFilePath = buildContext["sourceFilePath"]
        sourceFileName = buildContext["sourceFileName"]
        checksum = buildContext["checksum"]

        # The GitHub name collision was already checked in createAccessionRepo (before build).
        # Owner is the creator's account by default, or an explicit targetOwner (testing org).
        owner = buildContext.get("targetOwner") or curator
        personalTarget = f"{owner}/{repoName}"

        # Create PRIVATE: the staging state is invisible (private + no topic) until go-live.
        try:
            self.gh(f"repo create {personalTarget} --disable-wiki --private --source {repoDir} --push")
        except RuntimeError as e:
            # gh repo create --push can race with GitHub provisioning the new repo for
            # git-over-HTTPS access; the create succeeds but the immediate push fails with
            # "Repository not found". gh has already added the origin remote, so retry the
            # push from the local clone with the branch specified (no upstream is set yet).
            if "Repository not found" not in str(e):
                raise
            branchName = repo.active_branch.name
            lastError = None
            for delay in (2, 4, 8, 16):
                self.progressMethod(f"Initial push raced with repo provisioning; retrying in {delay}s...")
                time.sleep(delay)
                try:
                    repo.git.push("--set-upstream", "origin", branchName)
                    lastError = None
                    break
                except Exception as pushError:
                    lastError = pushError
            if lastError is not None:
                raise RuntimeError(f"Initial push retry failed after multiple attempts: {lastError}")

        self.localRepo = repo
        repoNameWithOwner = self.nameWithOwner("origin")

        self.gh(f"repo edit {repoNameWithOwner} --enable-projects=false --enable-discussions=false")

        # Tag the repo as staged-but-unpublished.  This topic is the durable, queryable record
        # of staging state (no client-side marker); publish removes it.
        self.gh(f"repo edit {repoNameWithOwner} --add-topic {self.stagingTopic}")

        # subscribe to all notifications for the new repository
        # gh repo watch was removed in newer gh CLI versions; use the API directly
        owner, name = repoNameWithOwner.split("/", 1)
        self.gh(f"api --method PUT /repos/{owner}/{name}/subscription --field subscribed=true --field ignored=false")

        # Non-member tier: create the v1 release and upload the source volume AS A RELEASE ASSET
        # (the volume lives on GitHub, capped at 2 GB). Members use S3 instead — see
        # _provisionStagedRepoInOrg and docs/org-design.md §1.0.
        commandList = ["release", "create", "--repo", repoNameWithOwner, "v1"]
        commandList += ["--notes", "Initial release"]
        self.gh(commandList)
        self.gh(f"release upload --repo {repoNameWithOwner} v1 {sourceFilePath}#{sourceFileName}.nrrd")

        # write source volume pointer: an owner-relative path resolved against the repo's current
        # owner at read time (resolveVolumeURL).
        fp = open(os.path.join(repoDir, "source_volume"), "w")
        fp.write(f"releases/download/v1/{sourceFileName}.nrrd")
        fp.close()

        repo.index.add([f"{repoDir}/source_volume"])
        repo.index.commit("Add source file url file")
        repo.remote(name="origin").push()

        self.stagingContext = {
            "repoDir": repoDir,
            "personalNameWithOwner": repoNameWithOwner,
            "repoName": repoName,
            "curator": curator,
            "speciesTopicString": buildContext["speciesTopicString"],
            "checksum": checksum,
            "duplicateRepos": buildContext.get("duplicateRepos", []),
        }

        # The local working copy is disposable now that the repo is fully on GitHub — remove it
        # (and the multi-GB source-volume file it holds) immediately.  Editing later re-clones
        # via the reopen path.
        self.localRepo = None
        if os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        return repoNameWithOwner

    def _publishStagedRepoInOrg(self, ctx):
        """Member tier: publish via the App — one-way private->public + topic swap (the App owns
        topics; members only have Write)."""
        repoName = ctx["repoName"]
        species = ctx.get("speciesTopicString")
        topics = ["morphodepot"] + ([f"md-{species}"] if species else [])
        self.progressMethod(f"Publishing {ctx['personalNameWithOwner']} (making public)...")
        self.controlPlaneRequest("repos/publish", {"repo": repoName, "topics": topics})
        self.ghTopicClearCache()
        finalNameWithOwner = ctx["personalNameWithOwner"]
        repoDir = ctx.get("repoDir")
        self.localRepo = None
        if repoDir and os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        self.stagingContext = None
        return finalNameWithOwner

    def publishStagedRepo(self):
        """Publish the staged repo IN PLACE — it already lives at its final location (members:
        in the MorphoDepot org via the App; non-members: their personal account; the developer
        self-test: the testing org).  Members go through the App's one-way private->public
        (_publishStagedRepoInOrg); everyone else flips public + adds discoverability topics
        directly.  No transfer.  Returns the final nameWithOwner."""
        ctx = getattr(self, "stagingContext", None)
        if not ctx:
            raise RuntimeError("No staged repository to publish.")
        if ctx.get("isMember"):
            return self._publishStagedRepoInOrg(ctx)
        personal = ctx["personalNameWithOwner"]
        speciesTopicString = ctx["speciesTopicString"]

        # Flip public, then add the discoverability topic(s) and drop the staging topic — the
        # moment the repo becomes discoverable and leaves the unpublished list.
        self.setRepoVisibility(personal, public=True)
        self.addMorphoTopics(personal, speciesTopicString)
        self.ghTopicClearCache()

        # The local working copy is no longer needed once published — remove it.
        repoDir = ctx.get("repoDir")
        self.localRepo = None
        if repoDir and os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        self.stagingContext = None
        return personal

    def discardStagedRepo(self):
        """Abandon the staged repo.  Deleting a repository needs the `delete_repo` token
        scope, which we deliberately do not request — so instead of calling the API, we hand
        the user off to the repo's GitHub Settings page (Danger Zone) to delete it from the
        web, and clean up our own side: remove the local clone and the in-memory staging
        state.  Returns the repo's settings URL for the caller to open.  Note: if the user
        does not actually delete it, the repo keeps its `morphodepot-staging` topic and so
        correctly stays in the unpublished list."""
        ctx = getattr(self, "stagingContext", None)
        if not ctx:
            return None
        personal = ctx["personalNameWithOwner"]
        repoDir = ctx.get("repoDir")
        if ctx.get("isMember"):
            # Member tier: the App deletes the in-org repo AND its S3 object (only while private).
            self.progressMethod(f"Discarding {personal} (deleting repo + volume)...")
            self.controlPlaneRequest("repos/discard", {"repo": ctx["repoName"]})
            self.localRepo = None
            if repoDir and os.path.exists(repoDir):
                shutil.rmtree(repoDir, ignore_errors=True)
            self.stagingContext = None
            return None    # already deleted — nothing for the UI to open
        self.localRepo = None
        if repoDir and os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        self.stagingContext = None
        return f"https://github.com/{personal}/settings"

    # --- Staged-repo recovery via the `morphodepot-staging` topic.  GitHub is the source of
    # truth: a staged-but-unpublished repo is simply one carrying this topic.  No durable
    # client state, so recovery works from any machine and survives a /tmp flush. ---

    stagingTopic = "morphodepot-staging"

    def listStagedRepos(self):
        """Return the active user's repositories that are staged but not yet published,
        identified by the `morphodepot-staging` topic.  Uses the repo LIST endpoint (topics
        are reflected immediately, unlike the search index which lags for fresh repos), so it
        is reliable right after staging and from any machine.  Returns marker-shaped dicts."""
        me = self.whoami()
        # Include org repos too — member-tier staged repos are born in MorphoDepot, owned by the
        # org but accessible only to the member's {handle}-team.
        try:
            repos = self.ghJSON(["api", "/user/repos?affiliation=owner,organization_member&per_page=100", "--paginate"])
        except Exception as e:
            logging.warning(f"listStagedRepos: could not list repositories: {e}")
            return []
        staged = []
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            owner = (repo.get("owner") or {}).get("login")
            if owner not in (me, self.morphoDepotOrg):
                continue
            if self.stagingTopic not in (repo.get("topics") or []):
                continue
            name = repo.get("name")
            nameWithOwner = repo.get("full_name") or f"{owner}/{name}"
            staged.append({
                "nameWithOwner": nameWithOwner,
                "repoName": name,
                "curator": me,
                "repoDir": os.path.join(self.localRepositoryDirectory(), name),
                "summary": nameWithOwner,
            })
        return staged

    def _fetchAccessionData(self, nameWithOwner):
        """Fetch and decode a repo's MorphoDepotAccession.json, or None if it has none.
        Used to re-derive the species topic when resuming a repo from the unpublished list."""
        try:
            data = self.ghJSON(["api", f"/repos/{nameWithOwner}/contents/MorphoDepotAccession.json"])
        except Exception:
            return None
        if not isinstance(data, dict) or "content" not in data:
            return None
        try:
            import base64
            decoded = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
            return json.loads(decoded)
        except Exception:
            return None

    def resumeStagedRepo(self, stagedRepo):
        """Resume a repo chosen from the unpublished list: CLONE it fresh (so its accession
        form can be pre-filled and edits applied), rebuild the in-memory staging context, and
        return the loaded accession data.  Cloning is cheap — the source volume is a release
        asset, not a git-tracked file — and gives a working tree for saveStagedRepoEdits()."""
        nameWithOwner = stagedRepo.get("nameWithOwner")
        repoName = stagedRepo.get("repoName")
        repoDir = os.path.join(self.localRepositoryDirectory(), repoName)
        if os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        self.gh(f"repo clone {nameWithOwner} {repoDir}")
        self.localRepo = git.Repo(repoDir)

        accession = {}
        accessionPath = os.path.join(repoDir, "MorphoDepotAccession.json")
        if os.path.exists(accessionPath):
            try:
                with open(accessionPath) as fp:
                    accession = json.load(fp)
            except Exception as e:
                logging.warning(f"Could not read accession data for {nameWithOwner}: {e}")

        species = ""
        try:
            species = (accession.get("species") or ["", ""])[1] or ""
        except Exception:
            species = ""
        # Carry the committed checksum so the duplicate-volume warning works on reopen too
        # (the clone persists here, unlike a fresh stage whose dir is deleted after provision).
        checksum = None
        checksumPath = os.path.join(repoDir, "source_volume_checksum")
        if os.path.exists(checksumPath):
            try:
                with open(checksumPath) as fp:
                    checksum = fp.read().strip()
            except Exception as e:
                logging.warning(f"Could not read source_volume_checksum for {nameWithOwner}: {e}")
        self.stagingContext = {
            "repoDir": repoDir,
            "personalNameWithOwner": nameWithOwner,
            "repoName": repoName,
            "curator": stagedRepo.get("curator"),
            "speciesTopicString": species.lower().replace(" ", "-") if species else "",
            "checksum": checksum,
            "duplicateRepos": self.duplicateVolumeRepos(checksum, exclude=nameWithOwner),
            "isMember": nameWithOwner.split("/", 1)[0] == self.morphoDepotOrg,
        }
        return accession

    def saveStagedRepoEdits(self, accessionData, colorTable=None, sourceSegmentation=None, screenshots=None):
        """Apply edits to the currently-resumed staged repo (clone in stagingContext): rewrite
        the metadata-derived files from `accessionData`, optionally replace the color table
        and/or baseline segmentation, regenerate the screenshots from `screenshots` (None =
        leave as-is), then — only if something actually changed — rewrite the repo's `main` as
        a single clean commit and force-push it.  The source volume and its release asset are
        NEVER touched (out of scope by design).  Recomputes the staging context's species topic
        for the subsequent publish.  Returns True if a change was pushed, False if the repo was
        already up to date."""
        ctx = getattr(self, "stagingContext", None)
        if not ctx:
            raise RuntimeError("No staged repository is open to edit.")
        repoDir = ctx.get("repoDir")
        repo = self.localRepo
        if not (repoDir and repo and os.path.exists(repoDir)):
            raise RuntimeError("No local working copy is available to apply edits.")

        # Capture the already-recorded species (from the committed README, before we rewrite
        # it) so a transient iDigBio outage during re-resolution can't blank it.
        priorSpecies = self._speciesFromReadme(repoDir)

        # Regenerate the metadata-derived files from the edited data.
        accessionData['fileFormatVersion'] = MorphoDepotLogic.accessionFileFormatVersion
        with open(os.path.join(repoDir, "MorphoDepotAccession.json"), "w") as fp:
            fp.write(json.dumps(accessionData, indent=4))

        speciesString = self._resolveSpeciesString(accessionData, fallbackSpecies=priorSpecies)
        ctx["speciesTopicString"] = speciesString.lower().replace(" ", "-") if speciesString else ""

        self._writeLicense(repoDir, accessionData)

        # Replace the color table only if a new one was supplied (else keep the committed CSV).
        if colorTable is not None:
            for existing in os.listdir(repoDir):
                if existing.endswith(".csv"):
                    os.remove(os.path.join(repoDir, existing))
            slicer.util.saveNode(colorTable, os.path.join(repoDir, colorTable.GetName()) + ".csv")

        # Replace the baseline segmentation only if a new one was supplied.
        if sourceSegmentation is not None:
            slicer.util.saveNode(sourceSegmentation, os.path.join(repoDir, "baseline.seg.nrrd"))

        # Screenshots: when a set is supplied, fully regenerate screenshots/ + captions.json
        # from it (add/remove/recaption); when None, leave the committed screenshots untouched.
        screenshotsDir = os.path.join(repoDir, "screenshots")
        if screenshots is not None:
            if os.path.exists(screenshotsDir):
                shutil.rmtree(screenshotsDir)
            screenshotItems = []
            if screenshots:
                os.makedirs(screenshotsDir, exist_ok=True)
                captions = {}
                for i, ss in enumerate(screenshots):
                    name = f"screenshot-{i+1}.png"
                    shutil.copy(ss["path"], os.path.join(screenshotsDir, name))
                    captions[name] = ss.get("caption", "")
                    screenshotItems.append((name, ss.get("caption", "")))
                with open(os.path.join(screenshotsDir, "captions.json"), "w") as f:
                    json.dump(captions, f, indent=2)
        else:
            screenshotItems = self._readScreenshotCaptions(repoDir)

        # Regenerate the README (screenshot section reflects the current screenshot set).
        readme = self._renderReadme(accessionData, speciesString, screenshotItems)
        with open(os.path.join(repoDir, "README.md"), "w") as fp:
            fp.write(readme)

        # Push only if the working tree actually changed.
        repo.git.add("-A")
        if not repo.git.diff("--cached", "--name-only").strip():
            return False

        # Reset history to a single clean commit ("as if created correctly") and force-push.
        repo.git.checkout("--orphan", "_morphodepot_clean")
        repo.git.add("-A")
        repo.git.commit("-m", "MorphoDepot accession")
        repo.git.branch("-M", "main")
        repo.git.push("--force", "origin", "main")
        return True

    #
    # Search
    #

    def refreshSearchCache(self):
        """Gets accession data from all repositories"""
        repos = self.morphoRepos()

        searchDirectory = os.path.join(self.localRepositoryDirectory(), "MorphoDepotCaches", "SearchData")
        os.makedirs(searchDirectory, exist_ok=True)

        self.repoDataByNameWithOwner = {}

        for repo in repos:
            try:
                repoName = repo['name']
                ownerLogin = repo['owner']['login']
                nameWithOwner = f"{repoName}^{ownerLogin}"
                filePath = f"{searchDirectory}/{nameWithOwner}-repoData.json"

                self.progressMethod(f"Refreshing {nameWithOwner}")

                repoData = None
                if os.path.exists(filePath):
                    with open(filePath) as fp:
                        repoData = json.load(fp)

                urlPrefix = "https://raw.githubusercontent.com"
                if not repoData:
                    accessionURL = f"{urlPrefix}/{ownerLogin}/{repoName}/main/MorphoDepotAccession.json"
                    request = requests.get(accessionURL)
                    if request.status_code == 200:
                        repoData = json.loads(request.text)
                    else:
                        self.progressMethod(f"Failed to load {accessionURL}")

                if repoData:
                    repoData['pushedAt'] = repo['pushedAt']
                    # Also fetch screenshot captions if they exist
                    if 'screenshotCount' not in repoData:
                        captionsURL = f"{urlPrefix}/{ownerLogin}/{repoName}/main/screenshots/captions.json"
                        captions_request = requests.get(captionsURL)
                        if captions_request.status_code == 200:
                            captionsData = captions_request.json()
                            repoData['screenshotCount'] = len(captionsData)
                            repoData['screenshotCaptions'] = captionsData
                        else:
                            repoData['screenshotCount'] = 0
                            repoData['screenshotCaptions'] = {}

                    # Fetch volume size if not already cached in the repoData
                    if 'volumeSize' not in repoData:
                        sourceVolumeURL_path = f"{urlPrefix}/{ownerLogin}/{repoName}/main/source_volume"
                        self.progressMethod(f"Getting {sourceVolumeURL_path}")
                        sourceVolumeURL_req = requests.get(sourceVolumeURL_path)
                        if sourceVolumeURL_req.status_code == 200:
                            volumeRef = sourceVolumeURL_req.text.strip()
                            volumeURL = self.resolveVolumeURL(volumeRef, f"{ownerLogin}/{repoName}")
                            self.progressMethod(f"Getting head of {volumeURL}")
                            head_req = requests.head(volumeURL, allow_redirects=True)
                            if head_req.status_code == 200 and 'Content-Length' in head_req.headers:
                                repoData['volumeSize'] = int(head_req.headers['Content-Length'])
                            else:
                                repoData['volumeSize'] = None # Explicitly mark as checked but not found
                            self.progressMethod(f"Volume size {repoData['volumeSize']}")

                    self.repoDataByNameWithOwner[nameWithOwner] = repoData
                    with open(filePath, "w") as fp:
                        fp.write(json.dumps(repoData))

            except Exception as e:
                # Use a more specific name here since repo is a dict
                repoIdentifier = f"{repo.get('owner', {}).get('login', 'N/A')}/{repo.get('name', 'N/A')}"
                logging.warning(f"Could not process repo {repoIdentifier}: {e}")

        self.progressMethod(f"Finished refreshing caches")


    def search(self, criteria):
        if self.repoDataByNameWithOwner == {}:
            return {}

        excludedRepos = set()
        for nameWithOwner, repoData in self.repoDataByNameWithOwner.items():
            for question in criteria:
                # Handle repoType with default assumption
                if question == "repoType":
                    repoValue = repoData.get("repoType", (None, "Archival (intended for long-term maintenance)"))[1]
                    if repoValue not in criteria["repoType"]:
                        excludedRepos.add(nameWithOwner)
                    continue

                if question == "subjectType":
                    # if subjectType is not present, assume "Biological specimen"
                    repoValue = repoData.get("subjectType", (None, "Biological specimen"))[1]
                    if repoValue not in criteria["subjectType"]:
                        excludedRepos.add(nameWithOwner)
                    continue

                # Handle other criteria
                if question in repoData:
                    repoValue = repoData[question][1]
                    if repoValue.__class__() == []:
                        valueInCriterion = False
                        for value in repoValue:
                            if value in criteria[question]:
                                valueInCriterion = True
                            if not valueInCriterion:
                                excludedRepos.add(nameWithOwner)
                    else:
                        if repoValue != "" and repoValue not in criteria[question]:
                            excludedRepos.add(nameWithOwner)

        matchString = f"*{criteria['freeText'].lower()}*"
        matchingRepos = set()
        textFields = ["githubRepoName", "species"]
        if "Other" in criteria.get("subjectType", []):
            textFields.append("otherSubjectDescription")
        for nameWithOwner, repoData in self.repoDataByNameWithOwner.items():
            if fnmatch.fnmatch(nameWithOwner, matchString):
                matchingRepos.add(nameWithOwner)
            for textField in textFields:
                if textField in repoData:
                    if fnmatch.fnmatch(repoData[textField][1].lower(), matchString):
                        matchingRepos.add(nameWithOwner)

        results = {}
        for nameWithOwner in matchingRepos:
            if nameWithOwner not in excludedRepos:
                results[nameWithOwner] = self.repoDataByNameWithOwner[nameWithOwner]

        return results

class ScreenshotReviewDialog(qt.QDialog):
    def __init__(self, screenshots, parent=None, selectLast=False):
        super(ScreenshotReviewDialog, self).__init__(parent)
        self.setWindowTitle("Review Screenshots")
        self.screenshots = [ss.copy() for ss in screenshots] # Work on a copy
        self.currentScreenshotIndex = -1

        self.setLayout(qt.QVBoxLayout())

        splitter = qt.QSplitter(qt.Qt.Horizontal)
        self.layout().addWidget(splitter)

        # Left side: Thumbnail list
        thumbnailWidget = qt.QWidget()
        thumbnailLayout = qt.QVBoxLayout(thumbnailWidget)
        thumbnailLayout.setContentsMargins(0,0,0,0)
        self.thumbnailList = qt.QListWidget()
        self.thumbnailList.setIconSize(qt.QSize(128, 128))
        self.thumbnailList.setFlow(qt.QListView.TopToBottom)
        self.thumbnailList.setMovement(qt.QListView.Static)
        self.thumbnailList.setViewMode(qt.QListView.IconMode)
        self.thumbnailList.setResizeMode(qt.QListView.Adjust)
        thumbnailLayout.addWidget(self.thumbnailList)
        splitter.addWidget(thumbnailWidget)

        # Right side: Main view
        rightSplitter = qt.QSplitter(qt.Qt.Vertical)

        self.screenshotLabel = qt.QLabel("Select a screenshot to view")
        self.screenshotLabel.setAlignment(qt.Qt.AlignCenter)
        rightSplitter.addWidget(self.screenshotLabel)

        captionGroup = qt.QGroupBox("Caption")
        captionLayout = qt.QVBoxLayout(captionGroup)
        self.captionEdit = qt.QTextEdit()
        self.captionEdit.setPlaceholderText("Enter caption for the selected screenshot...")
        self.captionEdit.enabled = False
        self.captionEdit.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Expanding)
        captionLayout.addWidget(self.captionEdit)
        rightSplitter.addWidget(captionGroup)
        splitter.addWidget(rightSplitter)

        splitter.setSizes([200, 600])
        rightSplitter.setSizes([600, 200]) # 3/4 for image, 1/4 for caption

        # Bottom buttons
        bottomLayout = qt.QHBoxLayout()
        self.deleteButton = qt.QPushButton("Delete Screenshot")
        self.deleteButton.enabled = False
        bottomLayout.addWidget(self.deleteButton)
        bottomLayout.addStretch()

        self.saveButton = qt.QPushButton("Save")
        self.cancelButton = qt.QPushButton("Cancel")
        bottomLayout.addWidget(self.saveButton)
        bottomLayout.addWidget(self.cancelButton)
        self.layout().addLayout(bottomLayout)

        # Connections
        self.thumbnailList.currentItemChanged.connect(self.onCurrentItemChanged)
        self.captionEdit.textChanged.connect(self.onCaptionChanged)
        self.deleteButton.clicked.connect(self.onDelete)
        self.saveButton.clicked.connect(lambda: self.accept())
        self.cancelButton.clicked.connect(lambda: self.reject())

        self.populateThumbnails()
        if self.thumbnailList.count > 0:
            if selectLast:
                self.thumbnailList.setCurrentRow(self.thumbnailList.count - 1)
            else:
                self.thumbnailList.setCurrentRow(0)

    def populateThumbnails(self):
        self.thumbnailList.clear()
        for i, ss_info in enumerate(self.screenshots):
            pixmap = qt.QPixmap(ss_info['path'])
            icon = qt.QIcon(pixmap)
            caption = ss_info['caption'] or ""
            if len(caption) > 50:
                caption = caption[:50] + "..."

            text = caption
            item = qt.QListWidgetItem(icon, text)
            self.thumbnailList.addItem(item)

    def onCurrentItemChanged(self, current, previous):
        if not current:
            self.screenshotLabel.setText("No screenshot selected.")
            self.captionEdit.clear()
            self.captionEdit.enabled = False
            self.deleteButton.enabled = False
            self.currentScreenshotIndex = -1
            return

        self.currentScreenshotIndex = self.thumbnailList.row(current)
        ss_info = self.screenshots[self.currentScreenshotIndex]

        # Update main image
        pixmap = qt.QPixmap(ss_info['path'])
        scaled_pixmap = pixmap.scaled(self.screenshotLabel.size, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation)
        self.screenshotLabel.setPixmap(scaled_pixmap)

        # Update caption (block signals to prevent loop)
        self.captionEdit.blockSignals(True)
        self.captionEdit.setText(ss_info['caption'])
        self.captionEdit.blockSignals(False)

        self.captionEdit.enabled = True
        self.deleteButton.enabled = True
        self.captionEdit.setFocus()

    def onCaptionChanged(self):
        if self.currentScreenshotIndex != -1:
            self.screenshots[self.currentScreenshotIndex]['caption'] = self.captionEdit.toPlainText()

    def onDelete(self):
        if self.currentScreenshotIndex == -1:
            return

        reply = qt.QMessageBox.question(self, 'Delete Screenshot',
                                        "Are you sure you want to delete this screenshot?",
                                        qt.QMessageBox.Yes | qt.QMessageBox.No, qt.QMessageBox.No)

        if reply == qt.QMessageBox.Yes:
            # Store the index and then clear the selection to prevent signals
            # from firing with a stale index.
            index_to_delete = self.currentScreenshotIndex
            self.thumbnailList.setCurrentRow(-1)
            self.currentScreenshotIndex = -1

            del self.screenshots[index_to_delete]
            self.populateThumbnails()
            self.thumbnailList.setCurrentRow(min(index_to_delete, self.thumbnailList.count - 1))

    def getUpdatedScreenshots(self):
        return self.screenshots


#
# MorphoDepotTest
#


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
