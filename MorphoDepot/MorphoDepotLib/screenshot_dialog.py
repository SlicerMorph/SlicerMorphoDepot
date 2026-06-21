"""ScreenshotReviewDialog (split from MorphoDepot.py)."""
import os
import qt
import slicer
from slicer.i18n import tr as _


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
