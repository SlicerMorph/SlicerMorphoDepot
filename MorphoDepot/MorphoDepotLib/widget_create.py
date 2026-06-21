"""MorphoDepotWidget CreateTabMixin (split from MorphoDepot.py)."""
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


class CreateTabMixin:
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
                        self.logic.gh(["release", "download", "v1", "--repo", nameWithOwner,
                                       "--pattern", f"{sourceVolumeName}.nrrd", "--dir", cacheDir,
                                       "--clobber"], timeout=None)  # large download: no timeout
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

        # Part E bad-input guard: a selected-but-empty baseline segmentation would stage an empty
        # baseline. (Color-table terminology is validated below for archival / warned for short-term.)
        if self._segmentationIsEmpty(sourceSegmentation):
            if not (self.testingMode or slicer.util.confirmOkCancelDisplay(
                    "The selected baseline segmentation has no segments. Stage it anyway (the "
                    "repository would start with an empty baseline)?", windowTitle="Empty baseline")):
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
        if self.testingMode:
            # Tests / e2e (e2eCreate -> e2ePublish) drive stage->publish in ONE session, hold this
            # widget ref, and read _stagedNameWithOwner; keep the go-live state for them rather than
            # the interactive reset (which reloads the module and would invalidate that ref).
            self._enterGoLiveState(staged)
        else:
            # UI #2: staging is a save-point, not go-live -- confirm, then reset the scene + form. The
            # curator resumes (double-click in the unpublished list) to edit/publish later.
            self._completeStepReset(
                "Staging completed",
                "Staging is completed. Scene is reset.\n\n"
                "You can go back and edit the repo by double-clicking it in the staged-only repos list.\n\n"
                "Repos that are not requested to be made public within 14 days of staging will be discarded.")

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

    def _repoHasMintedDoi(self, nameWithOwner):
        """True if the App already minted a DOI on this still-private repo (``.zenodo/state.json``) --
        i.e. it was approved and is awaiting the member's flip (#20).  Used to SKIP the edit-resave on
        a finish-flip, which would otherwise rewrite main and drop the App's DOI citation block."""
        try:
            info = self.logic.ghJSON(["api", f"/repos/{nameWithOwner}/contents/.zenodo/state.json"])
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
        # Tailor the publish prompt to the actual flow: a personal repo publishes straight to the
        # member's account; an org repo goes through the MD-reviewers gate, and the baseline-
        # contributor "next screen" below (org-design 9.6/9.7) only appears when the repo ships a
        # baseline at go-live -- so only promise that screen when it will actually show.
        nameWithOwner = ctx.get("personalNameWithOwner")
        with slicer.util.WaitCursor():  # _repoHasBaseline hits the GitHub API; show feedback (review)
            hasBaseline = bool(isOrg and nameWithOwner and self._repoHasBaseline(nameWithOwner))
        if not isOrg:
            prompt = f"Publish {where}?\n\nThis makes it public on your account."
        elif hasBaseline:
            prompt = (
                f"Publish {where}?\n\n"
                "If you have anyone other than yourself to acknowledge contributing to the "
                "segmentation, enter them on the next screen. Otherwise click Done there to skip.\n\n"
                "After that we will proceed with automated quality controls. Repo admins do not "
                "review your repo until these checks are completed.")
        else:
            prompt = (
                f"Publish {where}?\n\n"
                "We will now proceed with automated quality controls. Repo admins do not review "
                "your repo until these checks are completed.")
        if not (self.testingMode or slicer.util.confirmOkCancelDisplay(prompt, windowTitle="Publish repository")):
            return
        # Member-driven finish (#20): a resumed repo that is already approved (the App minted its DOI on
        # the still-private repo, awaiting the member's flip) must NOT have its edits re-saved -- that
        # rewrites main and drops the App's DOI citation block.  Just flip it.
        nwoForFinish = ctx.get("personalNameWithOwner")
        approvedFinish = bool(isOrg and nwoForFinish and self._repoHasMintedDoi(nwoForFinish))
        # Gather any pending edits up front so we can abort cleanly (without publishing) if the
        # loaded segmentation was edited in place.
        editsBundle = None
        if self._resumedForEdit and not approvedFinish:
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
        if hasBaseline:
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
        # Member-driven cutover (#20): automated validation (#19) bounced the submission — show the
        # specific blocking failures and leave the repo staged so the curator can fix and re-submit.
        if isinstance(final, dict) and final.get("changesRequested"):
            v = final.get("validation") or {}
            fails = v.get("failures") or []
            lines = "\n".join(f"  • {f.get('title')}: {f.get('detail')}" for f in fails) or "  • (see the review report)"
            self._stagedNameWithOwner = final.get("nameWithOwner")
            self.refreshStagedReposList(force=True)
            if not self.testingMode:
                slicer.util.errorDisplay(
                    "Automated checks must pass before this can be reviewed:\n\n"
                    f"{lines}\n\n"
                    "Fix these, then click Make Public again to re-submit."
                    + (f"\n\nTracking issue: {final.get('issueUrl')}" if final.get("issueUrl") else ""),
                    windowTitle="Changes required before publishing")
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
                    f"A request was emailed to {to}. Once a reviewer approves you'll get an email — "
                    "then reopen this repo from your unpublished list and click Make Public again to "
                    "publish it. Until then it stays private (reopening to make changes will require "
                    "requesting review again).",
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

    def _completeStepReset(self, title, message):
        """Shared close-out for a completed step (UI #2 theme): reset the scene, confirm with a popup,
        then reset the form so the next action starts from a clean slate.  onClearForm reloads the
        scripted module (rebuilding this widget), so it MUST be the final call here.  No-op in
        testingMode: the popup would block the automated run and the reload would invalidate the
        test's widget reference."""
        if self.testingMode:
            return
        slicer.mrmlScene.Clear()
        slicer.util.infoDisplay(message, windowTitle=title)
        self.onClearForm()

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
