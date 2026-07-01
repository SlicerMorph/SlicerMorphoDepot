"""MorphoDepotWidget SearchTabMixin (split from MorphoDepot.py)."""
import os
import re
import html
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


class SearchTabMixin:
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

            # Create a rich HTML tooltip.  S9: every interpolated field below is journal-derived
            # (mirrored from arbitrary public repos), so escape it -- Qt rich text won't run JS but
            # <img>/markup could load remote/file:// resources or corrupt layout.
            esc = lambda v: html.escape(str(v))
            tooltipParts = [f"<b>{esc(repoName)}</b> by <b>{esc(owner)}</b><br><hr>"]
            tooltipParts.append("<table>")
            tooltipParts.append(f"<tr><td><b>Last Active:</b></td><td>{esc(activeText)}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Species:</b></td><td>{esc(species)}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Size (GB):</b></td><td>{esc(sizeText)}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Modality:</b></td><td>{esc(modality)}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Spacing:</b></td><td>{esc(spacingText)}</td></tr>")
            tooltipParts.append(f"<tr><td><b>Dimensions:</b></td><td>{esc(dimensionsText)}</td></tr>")
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

                    # S3: filename is a key from a RepoClerk journal (mirrored from arbitrary public
                    # repos); accept only a bare screenshot basename so it can't traverse out of the
                    # cache dir when downloadFile writes to it (path-traversal write primitive).
                    safeName = os.path.basename(filename)
                    if not re.fullmatch(r"screenshot-\d+\.png", safeName):
                        logging.warning(f"Skipping screenshot with unexpected filename: {filename!r}")
                        continue
                    urlPrefix = "https://raw.githubusercontent.com"
                    imageURL = f"{urlPrefix}/{owner}/{repoName}/main/screenshots/{safeName}"
                    localImagePath = os.path.join(screenshotCacheDir, owner, repoName, safeName)

                    if not os.path.exists(localImagePath):
                        try:
                            os.makedirs(os.path.dirname(localImagePath), exist_ok=True)
                            slicer.util.downloadFile(imageURL, localImagePath)
                        except Exception as e:
                            logging.warning(f"Could not download screenshot {imageURL}: {e}")

                    if os.path.exists(localImagePath):
                        # S9: owner/repoName are also in this path -- escape so a crafted value
                        # can't break out of the src="" attribute.
                        tooltipParts.append(f'<img src="file:///{html.escape(localImagePath)}" width="128"> ')

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
        copyUrlAction = menu.addAction("Copy Repository URL")
        previewAction = menu.addAction("Preview in Slicer")

        action = menu.exec_(self.searchUI.resultsTable.mapToGlobal(point))

        if action == openRepoAction:
            qt.QDesktopServices.openUrl(qt.QUrl(f"https://github.com/{fullRepoName}"))
        elif action == copyUrlAction:
            # Copy the GitHub URL so it can be pasted straight into the Collections member
            # picker (which accepts a URL or owner/name) -- avoids retyping and typos.
            url = f"https://github.com/{fullRepoName}"
            qt.QApplication.clipboard().setText(url)
            slicer.util.showStatusMessage(f"Copied {url} to clipboard", 3000)
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

    def previewRepository(self, repoNameWithOwner):
        """Clones a repository and loads its data for previewing."""
        slicer.util.showStatusMessage(f"Previewing repository {repoNameWithOwner}...")
        if self.testingMode or slicer.util.confirmOkCancelDisplay("Close scene and load repository for preview?"):
            slicer.mrmlScene.Clear()
            with slicer.util.tryWithErrorDisplay("Failed to load repository", waitCursor=True):
                self.logic.loadRepoForPreview(repoNameWithOwner)
                repoDir = self.logic.localRepo.working_dir
                # S11 safety: only ever rmtree a strict child of the configured working directory,
                # never whatever working_dir the clone happened to set.
                base = os.path.abspath(self.logic.localRepositoryDirectory())
                if not repoDir:
                    logging.warning("previewRepository: localRepo.working_dir is None/empty — skipping cleanup")
                absRepoDir = os.path.abspath(repoDir) if repoDir else base
                if absRepoDir != base and os.path.commonpath([base, absRepoDir]) == base \
                        and os.path.exists(absRepoDir):
                    shutil.rmtree(absRepoDir)
                self.logic.localRepo = None
                slicer.util.showStatusMessage(f"Repository {repoNameWithOwner} loaded for preview.")
                slicer.util.messageBox("To contribute segmentations, right click on the search results row to open the repository web page and add an issue for your request.  The currently loaded data is not saved by default.",
                                       windowTitle = "You are in Preview mode",
                                       dontShowAgainSettingsKey = "MorphoDepot/DontShowPreviewNotice")
