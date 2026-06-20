"""MorphoDepotWidget AnnotateTabMixin (split from MorphoDepot.py)."""
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


class AnnotateTabMixin:
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
            self.logic.requestReview()
            self.updateAnnotatePRList()
            self.annotateUI.messageTitle.text = ""
            self.annotateUI.messageBody.plainText = ""
