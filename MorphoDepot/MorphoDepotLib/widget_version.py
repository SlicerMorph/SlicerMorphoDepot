"""MorphoDepotWidget VersionUIMixin (issue #203): update notification and manual update.

The check runs in the background the first time the module is entered in a Slicer session,
and says nothing at all unless there is something to say.  Updating is always the user's
explicit choice.

Once per session rather than once per day: a day-long cache would mean restarting Slicer
and still being told nothing about a version released that morning, which is the opposite
of what this is for.  One authenticated API call per launch costs nothing.

The background call follows the pattern slicer.util uses for non-blocking process output:
the worker thread only puts its result on a queue, and a QTimer started on the MAIN thread
polls that queue.  Posting the result with QTimer.singleShot from inside the worker would
create a timer on a thread with no event loop, where it would never fire.
"""

import datetime
import logging
import queue
import threading

import qt
import ctk
import slicer
from slicer.i18n import tr as _

from MorphoDepotLib.logic_version import (
    VERSION_CHECK_REPO,
    SHAPE_BUILD_TREE,
    SHAPE_CLONE,
    SHAPE_DEV_CLONE,
    SHAPE_EXTENSION,
    SHAPE_UNKNOWN,
)

VERSION_SETTINGS_PREFIX = "MorphoDepot/versionCheck/"
VERSION_POLL_INTERVAL_MS = 200
# Bounds the poll loop if the worker dies before it can report anything.
VERSION_POLL_DEADLINE_SECONDS = 120

SHAPE_DESCRIPTIONS = {
    SHAPE_EXTENSION: "Extension Manager",
    SHAPE_CLONE: "Developer checkout",
    SHAPE_DEV_CLONE: "Developer checkout",
    SHAPE_BUILD_TREE: "Built from source",
    SHAPE_UNKNOWN: "Unrecognized installation",
}


class VersionUIMixin:

    # ---------------------------------------------------------------- settings

    def versionSetting(self, name, default=""):
        return slicer.util.settingsValue(VERSION_SETTINGS_PREFIX + name, default)

    def setVersionSetting(self, name, value):
        qt.QSettings().setValue(VERSION_SETTINGS_PREFIX + name, value)

    def versionCheckEnabled(self):
        return slicer.util.settingsValue(
            VERSION_SETTINGS_PREFIX + "enabled", True, converter=slicer.util.toBool)

    def _forgetCacheSettings(self):
        """Drop the settings the removed 24-hour cache used to write.

        Nothing reads them any more, but they would otherwise sit in every existing
        settings file forever, and a stale 'available=true' left behind is exactly the
        sort of thing that confuses a later diagnosis.
        """
        settings = qt.QSettings()
        for orphaned in ("lastCheckAt", "latestSha", "latestDate", "aheadBy",
                         "available", "compareUrl", "compareStatus"):
            settings.remove(VERSION_SETTINGS_PREFIX + orphaned)

    # ---------------------------------------------------------------------- UI

    def setupVersionUI(self):
        """Build the update banner and the version controls on the Configure tab."""
        self._versionWidgetAlive = True
        self._versionCheckRunning = False
        self._versionCheckedThisSession = False
        self._versionInstalled = None
        self._versionStatus = None
        self._forgetCacheSettings()

        self.versionBanner = qt.QFrame()
        self.versionBanner.setFrameShape(qt.QFrame.StyledPanel)
        # palette() keeps this readable in both the light and dark Slicer themes.
        self.versionBanner.setStyleSheet(
            """
            QFrame {
                background-color: palette(midlight);
                border: 1px solid palette(highlight);
                border-radius: 4px;
                padding: 4px;
            }
            """
        )
        bannerLayout = qt.QVBoxLayout(self.versionBanner)
        bannerLayout.setContentsMargins(6, 6, 6, 6)

        self.versionBannerLabel = qt.QLabel()
        self.versionBannerLabel.wordWrap = True
        bannerLayout.addWidget(self.versionBannerLabel)

        buttonRow = qt.QHBoxLayout()
        self.versionUpdateButton = qt.QPushButton(_("Update Now"))
        self.versionUpdateButton.toolTip = _("Download and install the latest MorphoDepot")
        self.versionWhatsNewButton = qt.QPushButton(_("What's New"))
        self.versionWhatsNewButton.toolTip = _("Show the changes on GitHub")
        self.versionDismissButton = qt.QPushButton(_("Dismiss"))
        self.versionDismissButton.toolTip = _("Hide this notice until the next update is released")
        buttonRow.addWidget(self.versionUpdateButton)
        buttonRow.addWidget(self.versionWhatsNewButton)
        buttonRow.addWidget(self.versionDismissButton)
        buttonRow.addStretch(1)
        bannerLayout.addLayout(buttonRow)

        self.versionBanner.visible = False
        # Above the tab widget, so the notice is visible from every tab.
        self.layout.insertWidget(0, self.versionBanner)

        self.versionUpdateButton.connect("clicked()", self.onVersionUpdate)
        self.versionWhatsNewButton.connect("clicked()", self.onVersionWhatsNew)
        self.versionDismissButton.connect("clicked()", self.onVersionDismiss)

        # Configure tab: always available, whatever the banner is doing.
        self.configureUI.versionCollapsibleButton = ctk.ctkCollapsibleButton()
        self.configureUI.versionCollapsibleButton.text = _("MorphoDepot Version")
        self.configureUI.versionCollapsibleButton.collapsed = True
        versionLayout = qt.QFormLayout(self.configureUI.versionCollapsibleButton)

        self.configureUI.installedVersionLabel = qt.QLabel(_("Checking..."))
        self.configureUI.installedVersionLabel.wordWrap = True
        versionLayout.addRow(_("Installed:"), self.configureUI.installedVersionLabel)

        # Where it came from is a separate fact from whether it is current.  Keeping them
        # on one line made an ordinary state read like a warning.
        self.configureUI.versionSourceLabel = qt.QLabel("")
        self.configureUI.versionSourceLabel.wordWrap = True
        versionLayout.addRow(_("Source:"), self.configureUI.versionSourceLabel)

        self.configureUI.versionStatusLabel = qt.QLabel("")
        self.configureUI.versionStatusLabel.wordWrap = True
        versionLayout.addRow(_("Status:"), self.configureUI.versionStatusLabel)

        self.configureUI.versionCheckNowButton = qt.QPushButton(_("Check for Updates Now"))
        versionLayout.addRow(self.configureUI.versionCheckNowButton)

        self.configureUI.versionCheckEnabledCheckBox = qt.QCheckBox(_("Check for updates at startup"))
        self.configureUI.versionCheckEnabledCheckBox.checked = self.versionCheckEnabled()
        versionLayout.addRow(self.configureUI.versionCheckEnabledCheckBox)

        self.configureUI.configureCollapsibleButton.layout().addWidget(
            self.configureUI.versionCollapsibleButton)

        self.configureUI.versionCheckNowButton.connect("clicked()", self.onVersionCheckNow)
        self.configureUI.versionCheckEnabledCheckBox.connect("toggled(bool)", self.onVersionCheckEnabledToggled)

        self._setupVersionTestingFields()

    def _setupVersionTestingFields(self):
        """Let a developer point the check at another repository or branch.

        Lives in the Testing section, which is already developer-mode-gated, because the
        main use is testing an update against a branch before it reaches main.  It doubles
        as an escape hatch if the repository moves again, as it has once already.
        """
        testingLayout = getattr(self.configureUI, "testingLayout", None)
        if testingLayout is None:
            return

        self.configureUI.versionRepositoryLineEdit = qt.QLineEdit()
        self.configureUI.versionRepositoryLineEdit.text = self.logic.versionCheckRepository()
        self.configureUI.versionRepositoryLineEdit.toolTip = _(
            "owner/name of the repository the update check compares against")
        testingLayout.addRow(_("Update repository:"), self.configureUI.versionRepositoryLineEdit)

        self.configureUI.versionBranchLineEdit = qt.QLineEdit()
        self.configureUI.versionBranchLineEdit.text = self.logic.versionCheckBranch()
        self.configureUI.versionBranchLineEdit.toolTip = _(
            "branch the update check compares against")
        testingLayout.addRow(_("Update branch:"), self.configureUI.versionBranchLineEdit)

        self.configureUI.versionRepositoryLineEdit.connect(
            "textChanged(QString)", self.onVersionRepositoryChanged)
        self.configureUI.versionBranchLineEdit.connect(
            "textChanged(QString)", self.onVersionBranchChanged)

    def onVersionRepositoryChanged(self, text):
        self.setVersionSetting("repository", text.strip())
        # This session's answer was about a different repository.
        self._versionCheckedThisSession = False

    def onVersionBranchChanged(self, text):
        self.setVersionSetting("branch", text.strip())
        self._versionCheckedThisSession = False

    # ----------------------------------------------------------------- running

    def startVersionCheck(self, force=False):
        """Resolve the installed version, then check GitHub on a background thread.

        Called from enter().  Everything that reads Slicer state happens here, on the main
        thread; the worker only runs gh.
        """
        if not getattr(self, "_versionWidgetAlive", False) or self._versionCheckRunning:
            return
        if not force and not self.versionCheckEnabled():
            return
        if not force and self._versionCheckedThisSession:
            return
        # gh is how the check talks to GitHub, so there is nothing to do (and nothing worth
        # complaining about) until the user has finished setting up their dependencies.
        if not self.logic or not self.logic.ghExecutablePath:
            return

        installed = self.logic.installedVersion()
        self._versionInstalled = installed
        self._refreshVersionDisplay()

        installedSha = installed.get("sha", "")
        # Resolved here, on the main thread: both read QSettings.
        repository = self.logic.versionCheckRepository()
        branch = self.logic.versionCheckBranch()
        try:
            environment = slicer.util.startupEnvironment()
        except Exception:
            environment = None

        resultQueue = queue.Queue()
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=VERSION_POLL_DEADLINE_SECONDS)

        def check():
            try:
                resultQueue.put(self.logic.fetchUpdateStatus(
                    installedSha, environment, repository=repository, branch=branch))
            except Exception as e:
                logging.debug(f"MorphoDepot version check failed: {e}")
                resultQueue.put({"available": False, "error": str(e), "latestSha": "",
                                 "latestDate": "", "aheadBy": 0, "compareUrl": ""})

        def poll():
            if not getattr(self, "_versionWidgetAlive", False):
                return
            try:
                status = resultQueue.get_nowait()
            except queue.Empty:
                if datetime.datetime.now() > deadline:
                    logging.debug("MorphoDepot version check: giving up waiting for a result")
                    self._versionCheckRunning = False
                    return
                qt.QTimer.singleShot(VERSION_POLL_INTERVAL_MS, poll)
                return
            self._versionCheckRunning = False
            self._onVersionCheckFinished(status)

        self._versionCheckRunning = True
        # Marked as the session's check when it starts, not when it finishes, so a run of
        # failures (no network, say) cannot turn every tab switch into another attempt.
        self._versionCheckedThisSession = True
        threading.Thread(target=check, daemon=True).start()
        qt.QTimer.singleShot(0, poll)

    def _onVersionCheckFinished(self, status):
        if not getattr(self, "_versionWidgetAlive", False):
            return
        self._versionStatus = status
        self._refreshVersionDisplay()

    # ------------------------------------------------------------------ display

    def _versionDisplayName(self, sha, date):
        """Date first, revision in parentheses.

        A date is what a user can reason about; the revision is kept visible for everyone
        because it is the first thing worth knowing when diagnosing a report.
        """
        if not sha:
            return _("unknown")
        label = sha[:7]
        return f"{date} ({label})" if date else label

    def _sameRevision(self, first, second):
        """Whether two revisions are the same commit.

        One side is usually the .s4ext's abbreviated SHA and the other a full one, so this
        compares by prefix.  It is what keeps a cached "update available" from surviving an
        update that happened some other way -- through the Extension Manager, say -- in
        between two checks.
        """
        if not first or not second:
            return False
        length = min(len(first), len(second), 40)
        if length < 7:
            return False
        return first[:length] == second[:length]

    def _refreshVersionDisplay(self):
        installed = self._versionInstalled or {}
        status = self._versionStatus or {}

        self.configureUI.installedVersionLabel.text = self._versionDisplayName(
            installed.get("sha", ""), installed.get("date", ""))

        shape = installed.get("shape", SHAPE_UNKNOWN)
        source = SHAPE_DESCRIPTIONS.get(shape, shape)
        branch = installed.get("branch", "")
        if branch and shape in (SHAPE_CLONE, SHAPE_DEV_CLONE):
            source = _("{source}, branch '{branch}'").format(source=source, branch=branch)
        self.configureUI.versionSourceLabel.text = source

        updateAvailable = status.get("available") and not self._sameRevision(
            installed.get("sha", ""), status.get("latestSha", ""))

        if status.get("error"):
            statusText = status["error"]
        elif not status:
            statusText = _("Not checked yet.")
        elif updateAvailable:
            statusText = _("Update available: {version}").format(
                version=self._versionDisplayName(status.get("latestSha", ""), status.get("latestDate", "")))
            # Why an update is being withheld is only worth saying when one is actually
            # being withheld.  Said unconditionally it read as a warning about a healthy
            # install.
            if installed.get("blockedReason"):
                statusText += "\n" + installed["blockedReason"]
        elif status.get("compareStatus") == "behind":
            # We hold commits the tracked branch does not: a development build, which is
            # not the same thing as being level with the release.
            statusText = _("This is a development version, ahead of the released one.")
        elif installed.get("sha"):
            statusText = _("Up to date.")
        else:
            statusText = installed.get("blockedReason") or _(
                "The installed version could not be determined.")
        self.configureUI.versionStatusLabel.text = statusText

        self._refreshVersionBanner(installed, status)

    def _refreshVersionBanner(self, installed, status):
        latestSha = status.get("latestSha", "")
        if not status.get("available") or not latestSha:
            self.versionBanner.visible = False
            return
        if self._sameRevision(installed.get("sha", ""), latestSha):
            # The install has caught up with the latest by some other route -- an
            # Extension Manager update, say -- since this answer was fetched.
            self.versionBanner.visible = False
            return
        if self.versionSetting("dismissedSha") == latestSha:
            self.versionBanner.visible = False
            return

        # Deliberately no commit count: it means nothing to the researchers this is for,
        # and the two dates already say how far behind they are.  "What's New" is there
        # for anyone who wants the detail.
        message = _("MorphoDepot {latest} is available.  You have {installed}.").format(
            latest=self._versionDisplayName(latestSha, status.get("latestDate", "")),
            installed=self._versionDisplayName(installed.get("sha", ""), installed.get("date", "")))
        if installed.get("blockedReason"):
            message += "\n" + installed["blockedReason"]
        self.versionBannerLabel.text = message

        self.versionUpdateButton.visible = bool(installed.get("canUpdate"))
        self.versionWhatsNewButton.visible = bool(status.get("compareUrl"))
        self.versionBanner.visible = True

    # ----------------------------------------------------------------- handlers

    def onVersionCheckNow(self):
        self.startVersionCheck(force=True)

    def onVersionCheckEnabledToggled(self, checked):
        self.setVersionSetting("enabled", checked)

    def onVersionWhatsNew(self):
        url = (self._versionStatus or {}).get("compareUrl") or f"https://github.com/{VERSION_CHECK_REPO}"
        qt.QDesktopServices.openUrl(qt.QUrl(url))

    def onVersionDismiss(self):
        latestSha = (self._versionStatus or {}).get("latestSha", "")
        if latestSha:
            # Suppress this version only, so the next release speaks up again.
            self.setVersionSetting("dismissedSha", latestSha)
        self.versionBanner.visible = False

    def onVersionUpdate(self):
        installed = self._versionInstalled or {}
        status = self._versionStatus or {}
        targetSha = status.get("latestSha", "")
        if not installed.get("canUpdate") or not targetSha:
            slicer.util.messageBox(installed.get("blockedReason")
                                   or _("MorphoDepot cannot update this installation."))
            return

        confirmation = qt.QMessageBox()
        confirmation.setWindowTitle(_("Update MorphoDepot"))
        confirmation.setIcon(qt.QMessageBox.Question)
        if installed.get("shape") == SHAPE_CLONE:
            confirmation.setText(_("Fast-forward the MorphoDepot clone to {version}?").format(
                version=self._versionDisplayName(targetSha, status.get("latestDate", ""))))
            confirmation.setInformativeText(_("Location: {path}").format(
                path=installed.get("repositoryRoot", "")))
        else:
            confirmation.setText(_("Install MorphoDepot {version}?").format(
                version=self._versionDisplayName(targetSha, status.get("latestDate", ""))))
            confirmation.setInformativeText(_(
                "The installed module files will be replaced.  A copy of the current version is "
                "kept so the update can be undone.\n\nLocation: {path}").format(
                    path=installed.get("moduleDirectory", "")))
        confirmation.addButton(qt.QMessageBox.Cancel)
        updateButton = confirmation.addButton(_("Update"), qt.QMessageBox.AcceptRole)
        confirmation.exec_()
        if confirmation.clickedButton() != updateButton:
            return

        slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
        try:
            succeeded, message = self.logic.applyUpdate(installed, targetSha)
        finally:
            slicer.app.restoreOverrideCursor()

        if not succeeded:
            slicer.util.errorDisplay(message, windowTitle=_("MorphoDepot Update"))
            return

        self.versionBanner.visible = False
        # Re-read what is now on disk, so the Configure tab is honest even if the user
        # picks "Later" and keeps working in this session, and let the next module entry
        # check again rather than trusting this session's answer.
        self._versionCheckedThisSession = False
        self._versionInstalled = self.logic.installedVersion()
        self._refreshVersionDisplay()
        self._offerReloadAfterUpdate(message)

    def _offerReloadAfterUpdate(self, message):
        """Ask how to apply the new files; never restart on the user's behalf.

        Reloading is enough for a pure-Python module and keeps the scene, which matters
        because a MorphoDepot user usually has an unsaved segmentation open.
        """
        prompt = qt.QMessageBox()
        prompt.setWindowTitle(_("MorphoDepot Update"))
        prompt.setIcon(qt.QMessageBox.Information)
        prompt.setText(message)
        prompt.setInformativeText(_(
            "Reload MorphoDepot to use the new version now, or restart Slicer if anything "
            "looks wrong afterwards."))
        reloadButton = prompt.addButton(_("Reload MorphoDepot"), qt.QMessageBox.AcceptRole)
        restartButton = prompt.addButton(_("Restart Slicer"), qt.QMessageBox.DestructiveRole)
        prompt.addButton(_("Later"), qt.QMessageBox.RejectRole)
        prompt.exec_()

        clicked = prompt.clickedButton()
        if clicked == reloadButton:
            # Deferred: onReload destroys this widget, which is running the current slot.
            qt.QTimer.singleShot(0, self.onReload)
        elif clicked == restartButton:
            if slicer.mrmlScene.GetStorableNodesModifiedSinceRead():
                if not slicer.util.confirmOkCancelDisplay(
                        _("There is unsaved data in the scene.  Restart Slicer anyway?")):
                    return
            slicer.util.restart()
