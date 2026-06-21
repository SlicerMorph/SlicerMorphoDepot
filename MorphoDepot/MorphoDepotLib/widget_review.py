"""MorphoDepotWidget ReviewTabMixin (split from MorphoDepot.py)."""
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


class ReviewTabMixin:
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
            self._completeStepReset("Change request is successfully completed",
                                    "The change request was sent to the contributor.")

    def onApprove(self):
        with slicer.util.tryWithErrorDisplay("Failed to approve PR", waitCursor=True):
            slicer.util.showStatusMessage(f"Approving")
            self.logic.approvePR()
            self.reviewUI.reviewMessage.plainText = ""
            self.updateReviewPRList()
            self._completeStepReset("Review (approve) is successfully completed",
                                    "The pull request was approved and merged.")

    # Release
