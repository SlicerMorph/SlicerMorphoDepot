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
        # Re-evaluate the reviewer section here (with a FRESH team-membership check), because Slicer's
        # module Reload rebuilds the widget but does NOT call enter() — so without this a repoadminteam
        # change (or the section being rebuilt hidden on reload) is only reflected on a full module
        # re-entry. Now clicking Refresh Github updates it. Fail-closed (hidden) on 'unknown'.
        section = getattr(self, "reviewerInspectSection", None)
        if section is not None:
            section.visible = self.logic.isRepoAdmin(forceRefresh=True)

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

    # --- Reviewer tools: inspect a staged publish candidate (repoadminteam only; org-design Sec.11.6) ---

    def _setupReviewerInspect(self):
        """Build the repoadmin-only 'Repositories awaiting review' section and insert it at the TOP of
        the Review tab. Hidden by default; enter() reveals it only for a confirmed repoadminteam
        member. Loads a staged candidate's volume + segmentation read-only via the App (no clone,
        no GitHub access to the private repo needed)."""
        self._reviewCandidatesByItem = {}
        section = ctk.ctkCollapsibleButton()
        section.text = "Repositories awaiting review (reviewers only)"
        section.collapsed = False
        layout = qt.QVBoxLayout(section)
        hint = qt.QLabel(
            "Inspect a staged publish candidate's source volume and segmentation in Slicer, "
            "read-only. Approve or request changes from the review email.")
        hint.wordWrap = True
        layout.addWidget(hint)
        self.reviewQueueRefreshButton = qt.QPushButton("Refresh review queue")
        layout.addWidget(self.reviewQueueRefreshButton)
        self.reviewQueueList = qt.QListWidget()
        self.reviewQueueList.setToolTip("Staged candidates awaiting publication review.")
        layout.addWidget(self.reviewQueueList)
        self.inspectCandidateButton = qt.QPushButton("Inspect in Slicer (read-only)")
        self.inspectCandidateButton.enabled = False
        layout.addWidget(self.inspectCandidateButton)

        self.reviewTabWidget.layout().insertWidget(0, section)
        self.reviewerInspectSection = section
        section.visible = False  # enter() reveals it only for a confirmed repoadminteam member

        self.reviewQueueRefreshButton.connect("clicked(bool)", self.onReviewQueueRefresh)
        self.reviewQueueList.itemSelectionChanged.connect(self.onReviewQueueSelectionChanged)
        self.reviewQueueList.itemDoubleClicked.connect(self.onInspectCandidate)
        self.inspectCandidateButton.connect("clicked(bool)", self.onInspectCandidate)

    def onReviewQueueRefresh(self):
        with slicer.util.tryWithErrorDisplay("Failed to load the review queue", waitCursor=True):
            slicer.util.showStatusMessage("Loading review queue...")
            self.reviewQueueList.clear()
            self._reviewCandidatesByItem = {}
            self.inspectCandidateButton.enabled = False
            candidates = self.logic.reviewQueue()
            if not candidates:
                placeholder = qt.QListWidgetItem("(no repositories awaiting review)")
                placeholder.setFlags(qt.Qt.NoItemFlags)
                self.reviewQueueList.addItem(placeholder)
                return
            for c in candidates:
                curator = c.get("curator") or "?"
                item = qt.QListWidgetItem(f"{c.get('nameWithOwner')}    (curator: {curator})")
                self._reviewCandidatesByItem[item] = c
                self.reviewQueueList.addItem(item)
            slicer.util.showStatusMessage(f"{len(candidates)} awaiting review")

    def onReviewQueueSelectionChanged(self):
        selected = self.reviewQueueList.selectedItems()
        self.inspectCandidateButton.enabled = any(
            i in self._reviewCandidatesByItem for i in selected)

    def onInspectCandidate(self, item=None):
        candidate = self._reviewCandidatesByItem.get(item) if item is not None else None
        if candidate is None:
            selected = self.reviewQueueList.selectedItems()
            candidate = self._reviewCandidatesByItem.get(selected[0]) if selected else None
        if candidate is None:
            return
        nameWithOwner = candidate.get("nameWithOwner")
        if not (self.testingMode or slicer.util.confirmOkCancelDisplay(
                f"Close the current scene and load {nameWithOwner} for review?")):
            return
        with slicer.util.tryWithErrorDisplay("Failed to inspect the candidate", waitCursor=True):
            slicer.util.showStatusMessage(f"Loading {nameWithOwner} for review...")
            payload = self.logic.inspectCandidate(candidate.get("name"))
            slicer.mrmlScene.Clear()
            self.logic.loadCandidateForReview(payload)
            slicer.util.showStatusMessage(f"Loaded {nameWithOwner} (read-only review)")
            slicer.util.infoDisplay(
                f"Loaded {nameWithOwner} for review (read-only).\n\n"
                "This is a reviewer preview — inspect the source volume and segmentation. "
                "Approve or request changes from the review email.",
                windowTitle="Reviewing a candidate",
                dontShowAgainSettingsKey="MorphoDepot/DontShowReviewInspectNotice")

    # Release
