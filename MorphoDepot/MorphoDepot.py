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
from MorphoDepotLib.widget_create import CreateTabMixin
from MorphoDepotLib.widget_configure import ConfigureTabMixin
from MorphoDepotLib.widget_annotate import AnnotateTabMixin
from MorphoDepotLib.widget_review import ReviewTabMixin
from MorphoDepotLib.widget_release import ReleaseTabMixin
from MorphoDepotLib.widget_search import SearchTabMixin
from MorphoDepotLib.widget_validation import ValidationMixin


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


class MorphoDepotWidget(ScriptedLoadableModuleWidget, VTKObservationMixin, EnableModuleMixin, ValidationMixin, CreateTabMixin, ConfigureTabMixin, AnnotateTabMixin, ReviewTabMixin, ReleaseTabMixin, SearchTabMixin):
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

        # Give push buttons a subtle raised "surface" so they read as buttons instead of
        # blending into the adjacent text fields and combo boxes (Slicer's default Button
        # color is nearly identical to the field/window color in both themes). All colors
        # come from the active Qt palette via the palette() stylesheet function, so this
        # tracks the user's Slicer theme automatically -- the gray is #e6e6e6 in the light
        # theme and #5a5a5b in the dark theme. Applied once on the tab widget, it cascades
        # to every QPushButton in all tabs; ctkCollapsibleButton headers are a different
        # class and are left untouched.
        self.tabWidget.setStyleSheet(
            """
            QPushButton {
                background-color: palette(midlight);
                border: 1px solid palette(mid);
                border-radius: 4px;
                padding: 4px 12px;
                min-height: 18px;
            }
            QPushButton:hover { background-color: palette(light); }
            QPushButton:pressed { background-color: palette(button); }
            QPushButton:disabled {
                background-color: palette(window);
                border: 1px solid palette(midlight);
                color: palette(mid);
            }
            """
        )

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

    def onCurrentTabChanged(self,index):
        qt.QSettings().setValue("MorphoDepot/tabIndex", index)
        self.updateRefreshButtonLabels()
        if index == self.createTabIndex:
            if not self.ownerSelectorPopulated:
                self.populateOwnerSelector()
            self._refreshAutoAssignAvailability()
            self._refreshArchivalAvailability()
            self.refreshStagedReposList()

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
