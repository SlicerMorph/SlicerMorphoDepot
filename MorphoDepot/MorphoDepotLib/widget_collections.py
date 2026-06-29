"""MorphoDepotWidget CollectionsTabMixin — the Collections tab UI.

Lets an org member create a curated "repo of repos" collection: enter a title, pick (or paste)
at least two member repositories from the known corpus, and create.  The member's own ``gh``
creates the in-org repo, seeds a canonical README + ``CURATOR``, and tags it ``md-collection``;
RepoClerk then renders it as a gallery.  See logic_collections.CollectionsMixin and
SlicerMorph/SlicerMorphoDepot#180.

The collapsible-button containers come from MorphoDepotCollections.ui; the controls are built
here (same convention the Configure tab uses).
"""
import logging
import qt
import slicer


class CollectionsTabMixin:
    def setupCollectionsTab(self):
        """Build the Collections tab controls into the .ui containers and connect them."""
        ui = self.collectionsUI

        # --- Create a Collection ---
        createLayout = ui.createCollapsibleButton.layout()

        intro = qt.QLabel(
            "A collection groups existing MorphoDepot repositories under a theme "
            "(e.g. \"Mammal Skulls of the PNW\"). Enter a title and add at least two repositories.")
        intro.setWordWrap(True)
        createLayout.addWidget(intro)

        form = qt.QFormLayout()
        ui.titleEdit = qt.QLineEdit()
        ui.titleEdit.setPlaceholderText("e.g. Snakes of Texas")
        ui.titleEdit.setToolTip("The collection's display name. A short repository name is derived "
                                "from it automatically.")
        form.addRow("Title:", ui.titleEdit)
        ui.descEdit = qt.QLineEdit()
        ui.descEdit.setPlaceholderText("Optional one-line description")
        form.addRow("Description:", ui.descEdit)
        createLayout.addLayout(form)

        # Member picker: choose from the corpus or paste a GitHub URL.
        pickRow = qt.QHBoxLayout()
        ui.repoCombo = qt.QComboBox()
        ui.repoCombo.editable = True
        ui.repoCombo.setToolTip("Pick a repository from the list, or paste a GitHub URL, then Add.")
        ui.addMemberButton = qt.QPushButton("Add")
        pickRow.addWidget(ui.repoCombo, 1)
        pickRow.addWidget(ui.addMemberButton)
        createLayout.addLayout(pickRow)

        ui.membersList = qt.QListWidget()
        ui.membersList.setToolTip("Member repositories in this collection.")
        ui.membersList.setSelectionMode(qt.QAbstractItemView.ExtendedSelection)
        createLayout.addWidget(ui.membersList)

        ui.removeMemberButton = qt.QPushButton("Remove selected")
        createLayout.addWidget(ui.removeMemberButton)

        ui.makePublicCheck = qt.QCheckBox("Make public now (org owners only)")
        ui.makePublicCheck.checked = True
        ui.makePublicCheck.setToolTip(
            "Org owners can publish immediately. For other members the collection is created "
            "private and an owner publishes it.")
        createLayout.addWidget(ui.makePublicCheck)

        ui.createButton = qt.QPushButton("Create Collection")
        createLayout.addWidget(ui.createButton)

        ui.createStatus = qt.QLabel("")
        ui.createStatus.setWordWrap(True)
        createLayout.addWidget(ui.createStatus)

        # --- Existing Collections ---
        existingLayout = ui.existingCollapsibleButton.layout()
        ui.existingList = qt.QListWidget()
        ui.existingList.setToolTip("Existing collections (read-only). Click Refresh to load.")
        existingLayout.addWidget(ui.existingList)

        # Display-text -> nameWithOwner map for the corpus combo.
        self._collectionCorpus = {}

        # --- Connections ---
        ui.refreshButton.connect("clicked()", self.onCollectionsRefresh)
        ui.addMemberButton.connect("clicked()", self.onCollectionAddMember)
        ui.removeMemberButton.connect("clicked()", self.onCollectionRemoveMember)
        ui.createButton.connect("clicked()", self.onCreateCollection)
        ui.existingList.connect("itemDoubleClicked(QListWidgetItem*)",
                                self.onExistingCollectionDoubleClicked)

    # --- Handlers ---

    def onCollectionsRefresh(self):
        """Load the dataset corpus (for the picker) and the existing collections list."""
        ui = self.collectionsUI
        ui.createStatus.text = "Loading repositories from RepoClerk..."
        slicer.app.processEvents()
        try:
            corpus = self.logic.datasetRepoCorpus()
            collections = self.logic.collectionRepos()
        except Exception as e:
            ui.createStatus.text = f"Could not load repository data: {e}"
            logging.warning(f"Collections refresh failed: {e}")
            return

        ui.repoCombo.clear()
        self._collectionCorpus = {}
        for nwo in sorted(corpus):
            species = corpus[nwo].get("species") or ""
            display = f"{nwo}   ({species})" if species else nwo
            self._collectionCorpus[display] = nwo
            ui.repoCombo.addItem(display)
        ui.repoCombo.setCurrentText("")

        ui.existingList.clear()
        for c in collections:
            curator = f" — curated by @{c['curator']}" if c.get("curator") else ""
            item = qt.QListWidgetItem(
                f"{c['title']}  ({len(c.get('memberRefs', []))} members){curator}  [{c['nameWithOwner']}]")
            item.setData(qt.Qt.UserRole, c["nameWithOwner"])
            item.setToolTip(f"Double-click to open https://github.com/{c['nameWithOwner']}")
            ui.existingList.addItem(item)
        if not collections:
            ui.existingList.addItem("No collections yet.")

        ui.createStatus.text = f"Loaded {len(corpus)} repositories, {len(collections)} collections."

    def _resolveComboToNwo(self, text):
        text = (text or "").strip()
        if text in self._collectionCorpus:
            return self._collectionCorpus[text]
        return self.logic.normalizeRepoRef(text)

    def onCollectionAddMember(self):
        ui = self.collectionsUI
        nwo = self._resolveComboToNwo(ui.repoCombo.currentText)
        if not nwo:
            ui.createStatus.text = "Enter a repository as owner/name or a GitHub URL."
            return
        existing = [ui.membersList.item(i).text() for i in range(ui.membersList.count)]
        if nwo in existing:
            ui.createStatus.text = f"{nwo} is already in the collection."
            return
        ui.membersList.addItem(nwo)
        ui.repoCombo.setCurrentText("")
        ui.createStatus.text = ""

    def onExistingCollectionDoubleClicked(self, item):
        """Open the collection's GitHub repository page in the browser."""
        nwo = item.data(qt.Qt.UserRole)
        if nwo:
            qt.QDesktopServices.openUrl(qt.QUrl(f"https://github.com/{nwo}"))

    def onCollectionRemoveMember(self):
        ui = self.collectionsUI
        for item in ui.membersList.selectedItems():
            ui.membersList.takeItem(ui.membersList.row(item))

    def onCreateCollection(self):
        ui = self.collectionsUI
        title = ui.titleEdit.text.strip()
        description = ui.descEdit.text.strip()
        members = [ui.membersList.item(i).text() for i in range(ui.membersList.count)]
        makePublic = ui.makePublicCheck.checked

        ui.createButton.enabled = False
        ui.createStatus.text = "Creating collection..."
        qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
        slicer.app.processEvents()
        try:
            nwo = self.logic.createCollection(title, description, members, makePublic=makePublic)
        except Exception as e:
            ui.createStatus.text = f"Failed to create collection: {e}"
            logging.error(f"createCollection failed: {e}")
            ui.createButton.enabled = True
            return
        finally:
            qt.QApplication.restoreOverrideCursor()

        vis = "public" if makePublic else "private"
        ui.createStatus.text = (
            f"Created {nwo} ({vis}). RepoClerk will render it shortly. "
            "If it was created private, an org owner can publish it.")
        ui.titleEdit.text = ""
        ui.descEdit.text = ""
        ui.membersList.clear()
        ui.createButton.enabled = True
        self.onCollectionsRefresh()
