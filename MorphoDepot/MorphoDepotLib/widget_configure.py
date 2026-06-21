"""MorphoDepotWidget ConfigureTabMixin (split from MorphoDepot.py)."""
import os
import re
import sys
import csv
import glob
import json
import time
import math
import random
import shutil
import logging
import datetime
import fnmatch
import tempfile
import traceback
import subprocess
import git
import requests
import qt
import ctk
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from MorphoDepotLib.forms import (FormBaseQuestion, FormRadioQuestion, FormCheckBoxesQuestion,
    FormTextQuestion, FormComboBoxQuestion, FormSpeciesQuestion)
from MorphoDepotLib.accession_form import MorphoDepotAccessionForm
from MorphoDepotLib.search_form import MorphoDepotSearchForm
from MorphoDepotLib.screenshot_dialog import ScreenshotReviewDialog


class ConfigureTabMixin:
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
