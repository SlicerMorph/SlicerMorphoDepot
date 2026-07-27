"""MorphoDepotWidget VersionUIMixin (issue #203): update notification and manual update.

The check runs in the background whenever the module is entered, at most once a day, and
says nothing at all unless there is something to say.  Updating is always the user's
explicit choice.

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
    VERSION_CHECK_TTL_SECONDS,
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
    SHAPE_CLONE: "git clone",
    SHAPE_DEV_CLONE: "git clone (working copy)",
    SHAPE_BUILD_TREE: "build tree",
    SHAPE_UNKNOWN: "unrecognized install",
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

    def cachedVersionStatus(self):
        """The last check result if it is still fresh, otherwise None."""
        checkedAt = self.versionSetting("lastCheckAt")
        if not checkedAt:
            return None
        try:
            age = (datetime.datetime.now().astimezone()
                   - datetime.datetime.fromisoformat(checkedAt)).total_seconds()
        except ValueError:
            return None
        if age < 0 or age > VERSION_CHECK_TTL_SECONDS:
            return None
        latestSha = self.versionSetting("latestSha")
        if not latestSha:
            return None
        return {
            "available": slicer.util.settingsValue(
                VERSION_SETTINGS_PREFIX + "available", False, converter=slicer.util.toBool),
            "latestSha": latestSha,
            "latestDate": self.versionSetting("latestDate"),
            "aheadBy": int(self.versionSetting("aheadBy", "0") or 0),
            "compareUrl": self.versionSetting("compareUrl"),
            "error": "",
        }

    def cacheVersionStatus(self, status):
        self.setVersionSetting("available", status.get("available", False))
        self.setVersionSetting("latestSha", status.get("latestSha", ""))
        self.setVersionSetting("latestDate", status.get("latestDate", ""))
        self.setVersionSetting("aheadBy", str(status.get("aheadBy", 0)))
        self.setVersionSetting("compareUrl", status.get("compareUrl", ""))
        self.setVersionSetting("lastCheckAt", datetime.datetime.now().astimezone().isoformat())

    # ---------------------------------------------------------------------- UI

    def setupVersionUI(self):
        """Build the update banner and the version controls on the Configure tab."""
        self._versionWidgetAlive = True
        self._versionCheckRunning = False
        self._versionInstalled = None
        self._versionStatus = None

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
        # gh is how the check talks to GitHub, so there is nothing to do (and nothing worth
        # complaining about) until the user has finished setting up their dependencies.
        if not self.logic or not self.logic.ghExecutablePath:
            return

        installed = self.logic.installedVersion()
        self._versionInstalled = installed
        self._refreshVersionDisplay()

        if not force:
            cached = self.cachedVersionStatus()
            if cached:
                self._versionStatus = cached
                self._refreshVersionDisplay()
                return

        installedSha = installed.get("sha", "")
        try:
            environment = slicer.util.startupEnvironment()
        except Exception:
            environment = None

        resultQueue = queue.Queue()
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=VERSION_POLL_DEADLINE_SECONDS)

        def check():
            try:
                resultQueue.put(self.logic.fetchUpdateStatus(installedSha, environment))
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
        threading.Thread(target=check, daemon=True).start()
        qt.QTimer.singleShot(0, poll)

    def _onVersionCheckFinished(self, status):
        if not getattr(self, "_versionWidgetAlive", False):
            return
        self._versionStatus = status
        if not status.get("error"):
            self.cacheVersionStatus(status)
        self._refreshVersionDisplay()

    # ------------------------------------------------------------------ display

    def _versionDisplayName(self, sha, date):
        if not sha:
            return _("unknown")
        label = sha[:7]
        return f"{label} ({date})" if date else label

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

        shape = installed.get("shape", SHAPE_UNKNOWN)
        installedLabel = self._versionDisplayName(installed.get("sha", ""), installed.get("date", ""))
        self.configureUI.installedVersionLabel.text = "{version} - {shape}".format(
            version=installedLabel, shape=SHAPE_DESCRIPTIONS.get(shape, shape))

        upToDate = self._sameRevision(installed.get("sha", ""), status.get("latestSha", ""))
        if status.get("error"):
            self.configureUI.versionStatusLabel.text = status["error"]
        elif not status:
            self.configureUI.versionStatusLabel.text = _("Not checked yet.")
        elif status.get("available") and not upToDate:
            self.configureUI.versionStatusLabel.text = _("Update available: {version}").format(
                version=self._versionDisplayName(status.get("latestSha", ""), status.get("latestDate", "")))
        elif installed.get("sha"):
            self.configureUI.versionStatusLabel.text = _("Up to date.")
        else:
            self.configureUI.versionStatusLabel.text = _("The installed version could not be determined.")

        if installed.get("blockedReason"):
            self.configureUI.versionStatusLabel.text += "\n" + installed["blockedReason"]

        self._refreshVersionBanner(installed, status)

    def _refreshVersionBanner(self, installed, status):
        latestSha = status.get("latestSha", "")
        if not status.get("available") or not latestSha:
            self.versionBanner.visible = False
            return
        if self._sameRevision(installed.get("sha", ""), latestSha):
            # A cached result that the install has since caught up with.
            self.versionBanner.visible = False
            return
        if self.versionSetting("dismissedSha") == latestSha:
            self.versionBanner.visible = False
            return

        aheadBy = status.get("aheadBy", 0)
        message = _("A newer MorphoDepot is available: {latest} (you have {installed}).").format(
            latest=self._versionDisplayName(latestSha, status.get("latestDate", "")),
            installed=self._versionDisplayName(installed.get("sha", ""), installed.get("date", "")))
        if aheadBy:
            message += " " + _("{count} commits behind.").format(count=aheadBy)
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
        # Drop the cached result and re-read what is now on disk, so the Configure tab is
        # honest even if the user picks "Later" and keeps working in this session.
        self.setVersionSetting("lastCheckAt", "")
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
