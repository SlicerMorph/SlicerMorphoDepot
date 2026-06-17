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
        self.tabWidget.addTab(uiWidget, "Create")
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
        self.configureUI.creatorUser.toolTip = "GitHub user account for creating repositories in tests. Must be logged in via 'gh auth login' with 'delete_repo' scope."
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
        validationCallback = lambda valid, w=self.createUI.createRepository: w.setEnabled(valid)
        self.createUI.accessionForm = MorphoDepotAccessionForm(validationCallback=validationCallback)
        self.createUI.accessionLayout.addWidget(self.createUI.accessionForm.topWidget)

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

    def onCurrentTabChanged(self,index):
        qt.QSettings().setValue("MorphoDepot/tabIndex", index)
        self.updateRefreshButtonLabels()

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

    def onUserNameChanged(self, userName):
        if userName:
            self.logic.setGitConfig("user.name", userName)
        self.configureUI.userNameStatusLabel.visible = not bool(userName)

    def onUserEmailChanged(self, userEmail):
        if userEmail:
            self.logic.setGitConfig("user.email", userEmail)
        self.configureUI.userEmailStatusLabel.visible = not bool(userEmail)

    # Create
    def onCreateRepository(self):
        if self.createUI.inputSelector.currentNode() == None or self.createUI.colorSelector.currentNode() == None:
            slicer.util.errorDisplay("Need to select volume and color table")
            return

        sourceVolume = self.createUI.inputSelector.currentNode()
        sourceSegmentation = self.createUI.segmentationSelector.currentNode()
        colorTable = self.createUI.colorSelector.currentNode()

        validGithubAsset = r'^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$'
        if re.fullmatch(validGithubAsset, sourceVolume.GetName()) is None:
            slicer.util.errorDisplay("Please rename volume.\n"
                "Only alphanumerics, periods, hyphens and underscores accepted.")
            return
        if re.fullmatch(validGithubAsset, colorTable.GetName()) is None:
            slicer.util.errorDisplay("Please rename color table.\n"
                "Only alphanumerics, periods, hyphens and underscores accepted.\n"
                "Use the 'All nodes' tab of the Data module to access the color table and right-click to rename.")
            return

        slicer.util.showStatusMessage(f"Creating...")
        accessionData = self.createUI.accessionForm.accessionData()
        accessionData['scanDimensions'] = str(sourceVolume.GetImageData().GetDimensions())
        accessionData['scanSpacing'] = str(sourceVolume.GetSpacing())

        if accessionData["repoType"][1] == "Archival (intended for long-term maintenance)":
            for colorIndex in range(1, colorTable.GetNumberOfColors()):
                if colorTable.GetTerminologyAsString(colorIndex) == "~^^~^^~^^~~^^~^^~":
                    slicer.util.errorDisplay(f"Selected Color table is missing terminology for index {colorIndex}, {colorTable.GetColorName(colorIndex)}", windowTitle="Missing Terminology")
                    return
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
                    return


        if not self.showConfirmationDialog(sourceVolume, colorTable, accessionData, sourceSegmentation, self.screenshots):
            self.progressMethod("Repository creation aborted")
            return

        try:
            with slicer.util.tryWithErrorDisplay(_("Trouble creating repository"), waitCursor=True):
                self.logic.createAccessionRepo(sourceVolume, colorTable, accessionData, sourceSegmentation, self.screenshots)
                # Collect contact info — best-effort, runs in background thread so it never blocks the UI
                CONTACT_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLScqzoTAIklSg2Dc4sQHMw-_J8PPQUOSBqFrpLnWpLS-tvvVHQ/formResponse"
                CONTACT_FORM_ENTRY_EMAIL       = "entry.2057466047"  # Email Address
                CONTACT_FORM_ENTRY_GH_USER     = "entry.1912463514"  # GitHub Username
                CONTACT_FORM_ENTRY_REPO_NAME   = "entry.683034902"   # Repository Name
                CONTACT_FORM_ENTRY_REPO_TYPE   = "entry.156019116"   # Repository Type
                try:
                    ghUser = self.logic.gh("api user --jq .login").strip()
                except Exception:
                    ghUser = ""
                repoTypeFull = accessionData['repoType'][1]
                repoTypeShort = "Archival" if repoTypeFull.startswith("Archival") else "Short-term"
                formData = {
                    CONTACT_FORM_ENTRY_EMAIL:     self.createUI.accessionForm.contactEmailQuestion.answer().strip(),
                    CONTACT_FORM_ENTRY_GH_USER:   ghUser,
                    CONTACT_FORM_ENTRY_REPO_NAME: accessionData['githubRepoName'][1],
                    CONTACT_FORM_ENTRY_REPO_TYPE: repoTypeShort,
                }
                import threading
                def _submitContactForm(url, data):
                    try:
                        requests.post(url, data=data, timeout=5)
                    except Exception:
                        pass  # non-critical
                threading.Thread(target=_submitContactForm, args=(CONTACT_FORM_URL, formData), daemon=True).start()
        except Exception as e:
            slicer.util.showStatusMessage(f"Cleaning up...")
            repoName = accessionData['githubRepoName'][1]
            repoDir = os.path.join(self.logic.localRepositoryDirectory(), repoName)
            if os.path.exists(repoDir):
                shutil.rmtree(repoDir)

        self.createUI.createRepository.enabled = False
        self.createUI.openRepository.enabled = True
        slicer.util.showStatusMessage(f"")
        self.screenshots = []
        self.updateScreenshotCount()

    def showConfirmationDialog(self, sourceVolume, colorTable, accessionData, sourceSegmentation, screenshots):
        """Shows a confirmation dialog with a summary of the repository to be created."""
        if self.testingMode:
            return True
        dialog = qt.QDialog(slicer.util.mainWindow())
        dialog.setWindowTitle("Confirm Repository Creation")
        layout = qt.QVBoxLayout(dialog)

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
        nameWithOwner = self.logic.nameWithOwner("origin")
        repoURL = qt.QUrl(f"https://github.com/{nameWithOwner}")
        qt.QDesktopServices.openUrl(repoURL)

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
        form.contactEmailQuestion.answerText.text = "test@example.com"
        form.contactEmailConfirmQuestion.answerText.text = "test@example.com"

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
                    # Auto-select what the loader pulled out of the repo so the user has a
                    # working default for both required fields (still editable by the user).
                    self.releaseUI.newBaselineSelector.setCurrentNode(getattr(self.logic, 'baselineSegmentationNode', None))
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
        q,a,t = form["githubRepoName"]
        self.questions["githubRepoName"] = FormTextQuestion(q, self.validateForm)
        self.questions["githubRepoName"].questionBox.toolTip = t
        layout.addWidget(self.questions["githubRepoName"].questionBox)
        q,a,t = form["repoType"]
        self.questions["repoType"] = FormRadioQuestion(q, a, self.validateForm)
        layout.addWidget(self.questions["repoType"].questionBox)

        emailTooltip = "Your email will be added to the MorphoDepot contact list so you can be notified about new features and updates"
        self.contactEmailQuestion = FormTextQuestion("What is your email address?", self.validateForm)
        self.contactEmailQuestion.questionBox.toolTip = emailTooltip
        layout.addWidget(self.contactEmailQuestion.questionBox)
        self.contactEmailConfirmQuestion = FormTextQuestion("Confirm your email address:", self.validateForm)
        self.contactEmailConfirmQuestion.questionBox.toolTip = emailTooltip
        layout.addWidget(self.contactEmailConfirmQuestion.questionBox)

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
        emailRegex = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
        email = self.contactEmailQuestion.answer().strip()
        valid = valid and bool(re.match(emailRegex, email))
        valid = valid and (email == self.contactEmailConfirmQuestion.answer().strip().lower() or
                           email.lower() == self.contactEmailConfirmQuestion.answer().strip().lower())
        self.validationCallback(valid)

    def accessionData(self):
        data = {}
        for key in MorphoDepotAccessionForm.formQuestions.keys():
            data[key] = (self.questions[key].questionLabel.text, self.questions[key].answer())
        return data


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

class FormTextQuestion(FormBaseQuestion):
    def __init__(self, question, validator):
        super().__init__(question)
        self.answerText = qt.QLineEdit()
        self.answerText.connect("textChanged(QString)", validator)
        self.questionLayout.addWidget(self.answerText)

    def answer(self):
        return self.answerText.text

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
        """
        if volumeRef.startswith("http"):
            return volumeRef  # existing repos with hardcoded URL — use as-is
        return f"https://github.com/{repoNameWithOwner}/{volumeRef}"

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

        if repositoryName not in self.repositoryList():
            self.gh(f"repo fork {sourceRepository} --clone=false")
        self.gh(f"repo clone {repositoryName} {localDirectory}")
        self.localRepo = git.Repo(localDirectory)
        self.ensureUpstreamExists()

        originBranches = self.localRepo.remotes.origin.fetch()
        originBranchIDs = [ob.name for ob in originBranches]
        originBranchID = f"origin/{branchName}"

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
            logging.debug("Nothing local or remote, nothing in origin so make new branch %s", branchName)
            self.localRepo.git.checkout("origin/main")
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
                if configuration == "reviewer":
                    self.segmentationNode.CreateClosedSurfaceRepresentation()
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
        """Post a release-announcement comment on every open issue and PR.
        Substitutes {deadline} in the message body. Returns (issueCount, prCount)."""
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
        return len(issues), len(prs)

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

    def createAccessionRepo(self, sourceVolume, colorTable, accessionData, sourceSegmentation=None, screenshots=None):

        repoName = accessionData['githubRepoName'][1]
        repoDir = os.path.join(self.localRepositoryDirectory(), repoName)
        os.makedirs(repoDir)

        # save data
        repoFileNames = []
        sourceFileName = sourceVolume.GetName()
        sourceFilePath = os.path.join(repoDir, sourceFileName) + ".nrrd"
        slicer.util.saveNode(sourceVolume, sourceFilePath, properties={'useCompression': True})

        githubReleaseAssetSizeLimit = 2 * 2**30 - 1 # 2GB - 1
        if os.path.getsize(sourceFilePath) > githubReleaseAssetSizeLimit:
            raise ValueError("Volume file is too large, crop or resample so that saved size is less than 2GB")

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
        if accessionData["license"][1].startswith("CC BY-NC"):
            licenseURL = "https://creativecommons.org/licenses/by-nc/4.0/legalcode.txt"
        else:
            licenseURL = "https://creativecommons.org/licenses/by/4.0/legalcode.txt"
        response = requests.get(licenseURL)
        fp = open(os.path.join(repoDir, "LICENSE.txt"), "w")
        fp.write(response.content.decode('ascii', errors="ignore"))
        fp.close()

        if accessionData['iDigBioAccessioned'][1] == "Yes":
            idigbioURL = accessionData['iDigBioURL'][1]
            specimenID = idigbioURL.split("/")[-1]
            import idigbio
            api = idigbio.json()
            idigbioData = api.view("records", specimenID)
            if 'ala:species' in idigbioData['data']:
                speciesString = idigbioData['data']['ala:species']
            elif 'dwc:scientificName' in idigbioData['data']:
                speciesString = idigbioData['data']['dwc:scientificName']
            else:
                logging.warning(f"Could not find species for {idigbioURL}")
                logging.warning(f"Response from api: {idigbioData}")
                speciesString = "Unknown species"
        else:
            speciesString = accessionData['species'][1]
        speciesTopicString = speciesString.lower().replace(" ", "-")

        # write readme file
        readme_content = f"""
## MorphoDepot Repository
Repository for segmentation of a specimen scan.  See [this JSON file](MorphoDepotAccession.json) for specimen details.
* Species: {speciesString}
* Modality: {accessionData['modality'][1]}
* Contrast: {accessionData['contrastEnhancement'][1]}
* Dimensions: {accessionData['scanDimensions']}
* Spacing (mm): {accessionData['scanSpacing']}
"""
        if screenshots:
            readme_content += "\n\n## Screenshots\n"
            for i, screenshotInfo in enumerate(screenshots):
                screenshot_filename = f"screenshot-{i+1}.png"
                caption = screenshotInfo['caption']
                readme_content += f"\n![{caption or screenshot_filename}](screenshots/{screenshot_filename})\n"
                if caption:
                    readme_content += f"_{caption}_\n"
        fp = open(os.path.join(repoDir, "README.md"), "w")
        fp.write(readme_content)
        fp.close()

        # create initial repo
        repo = git.Repo.init(repoDir, initial_branch='main')

        repoFileNames += [
            "README.md",
            "LICENSE.txt",
            "MorphoDepotAccession.json",
            "source_volume_checksum",
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
        repoFilePaths = [os.path.join(repoDir, fileName) for fileName in repoFileNames]
        repo.index.add(repoFilePaths)
        repo.index.commit("Initial commit")

        try:
            self.gh(f"repo create {repoName} --add-readme --disable-wiki --public --source {repoDir} --push")
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

        self.gh(f"repo edit {repoNameWithOwner} --add-topic morphodepot --add-topic md-{speciesTopicString}")

        # subscribe to all notifications for the new repository
        # gh repo watch was removed in newer gh CLI versions; use the API directly
        owner, repoName = repoNameWithOwner.split("/", 1)
        self.gh(f"api --method PUT /repos/{owner}/{repoName}/subscription --field subscribed=true --field ignored=false")

        # create initial release and add asset
        # use list for command to handle spaces in notes
        commandList = ["release", "create", "--repo", repoNameWithOwner, "v1"]
        commandList += ["--notes", "Initial release"]
        self.gh(commandList)
        self.gh(f"release upload --repo {repoNameWithOwner} v1 {sourceFilePath}#{sourceFileName}.nrrd")

        # write source volume pointer file (owner-agnostic relative path for transfer safety)
        fp = open(os.path.join(repoDir, "source_volume"), "w")
        fp.write(f"releases/download/v1/{sourceFileName}.nrrd")
        fp.close()

        repo.index.add([f"{repoDir}/source_volume"])
        repo.index.commit("Add source file url file")
        repo.remote(name="origin").push()

        self.ghTopicClearCache()

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
        self.test_MorphoDepot1()
        widget.testingMode = False

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
        form.questions["githubRepoName"].answerText.text = f"MorphoDepotTesting/{repoName}"
        repoNameWithOwner = form.questions["githubRepoName"].answerText.text

        # Create the repository
        widget.onCreateRepository()
        slicer.app.processEvents()
        self.delayDisplay(f"Repository {repoName} created.")

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

        #logic.gh(f"repo delete {repoNameWithOwner} --yes")

        self.delayDisplay("Test passed")
