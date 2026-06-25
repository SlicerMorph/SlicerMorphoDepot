"""MorphoDepotWidget ReleaseTabMixin (split from MorphoDepot.py)."""
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


class ReleaseTabMixin:
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
        # S8: reposByItem is shared across tabs; a stale/foreign list item is not a key -- bail
        # instead of raising KeyError outside any tryWithErrorDisplay.
        repoData = self.reposByItem.get(item)
        if repoData is None:
            return
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

    def onMakeRelease(self):
        if not self.logic.localRepo:
            return
        baselineNode = self.releaseUI.newBaselineSelector.currentNode()
        colorTableNode = self.releaseUI.newColorSelector.currentNode()
        if baselineNode is None or colorTableNode is None:
            return

        # Part E bad-input guards: an empty baseline or a non-terminology (e.g. continuous) color
        # table would publish a broken release.  Warn (consistent with the no-change warnings below).
        if self._segmentationIsEmpty(baselineNode):
            if not (self.testingMode or slicer.util.confirmOkCancelDisplay(
                    "The selected baseline segmentation has no segments, so this release would "
                    "publish an empty baseline.\n\nProceed anyway?", windowTitle="Empty baseline")):
                return
        if self._colorTableNotTerminology(colorTableNode):
            if not (self.testingMode or slicer.util.confirmOkCancelDisplay(
                    f"The selected color table '{colorTableNode.GetName()}' (type "
                    f"'{colorTableNode.GetTypeAsString()}') does not look like a terminology color "
                    "table loaded from a file -- it may be a built-in continuous colormap. MorphoDepot "
                    "expects a discrete, terminology-based color table.\n\nUse it anyway?",
                    windowTitle="Color table not terminology")):
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

        prompt = self.buildReleaseConfirmation(plan, baselineNode, colorTableNode, nameWithOwner)
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
        import MorphoDepotLib.contributors as MDC
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
        import MorphoDepotLib.contributors as MDC
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

    def buildReleaseConfirmation(self, plan, baselineNode, colorTableNode, nameWithOwner):
        """Compose the OK/Cancel summary describing every action that will run during release.

        Releases are archival/org-only (the Release tab lists only org repos), so this always
        describes the reviewed flow: the release commit goes to a release-candidate branch and
        the tag + DOI are cut by the App on approval -- main is untouched until then."""
        tag = plan['newTag']
        lines = []
        lines.append(f"Make release {tag} for {nameWithOwner}.")
        lines.append("")
        lines.append("This release will package:")
        lines.append(f"• Baseline segmentation '{baselineNode.GetName()}' → saved as baseline.seg.nrrd (replaces the current one).")
        lines.append(f"• Color table '{colorTableNode.GetName()}' → replaces the current one.")
        lines.append(f"• README.md → regenerated for {tag}.")
        if plan['archivedReadme']:
            lines.append(
                f"  ⚠ The new README.md is rebuilt from MorphoDepotAccession.json. Your current "
                f"README.md is preserved as {plan['archivedReadme']}, but manual edits in it are NOT "
                f"carried into the new one — copy anything you want to keep after the release."
            )
        if plan['newScreenshotNames']:
            lines.append(
                f"• {len(plan['newScreenshotNames'])} new screenshot(s) will be added: "
                f"{', '.join(plan['newScreenshotNames'])}."
            )
        else:
            lines.append("• No new screenshots will be added.")
        if plan['issueSegFiles']:
            lines.append(
                f"• {len(plan['issueSegFiles'])} per-issue segmentation file(s) will be removed from "
                f"the working tree (kept in git history): {', '.join(plan['issueSegFiles'])}."
            )
        lines.append("")
        # Release tab only lists org/archival repos (administratedRepoList); the short-term test path
        # skips this dialog via testingMode, so the release described here is always the reviewed one.
        lines.append("This is an archival repository, so the release is reviewed before it goes public:")
        lines.append(f"  1. The release commit is pushed to a release-candidate-{tag} branch — main is left untouched.")
        lines.append("  2. A review request is emailed to the MorphoDepot reviewers.")
        lines.append(
            f"  3. On approval the release is cut automatically: main is archived as pre-release-{tag}, "
            f"updated to the candidate, the {tag} tag is created, and a DOI is minted for the release."
        )
        lines.append("  4. If changes are requested, you'll get an email; nothing is published until you re-submit and it's approved.")
        lines.append("")
        lines.append("No data is lost: prior versions of every changed file remain in git history, and clicking Cancel makes no changes at all.")
        lines.append("If anything fails partway, you will be offered the chance to reset the local repository to its pre-release state or to keep the partial changes for inspection.")
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
