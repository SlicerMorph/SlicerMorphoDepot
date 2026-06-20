"""MorphoDepot workflow smoke tests (happy path, valid input). Breadth over
depth: touch a representative interaction in each tab so a refactor that breaks
any tab's wiring shows up. Uses the global `H` (Harness) set up by md_run.py."""


def _create_redistribution_gate():
    H.goTab("Create")
    H.fillValidForm(name="harness-gate-repo", shortTerm=True)
    assert H.w.createUI.createRepository.enabled, "Create disabled after a valid fill"
    H.setQ("redistributionAcknowledgement", [])
    assert not H.w.createUI.createRepository.enabled, "Create stayed enabled with redistribution unchecked"
    H.setQ("redistributionAcknowledgement",
           ["I have the right to allow redistribution of this data."])
    assert H.w.createUI.createRepository.enabled, "Create did not re-enable when re-checked"


def _create_f4_name_suggestion():
    H.goTab("Create")
    H.setQ("subjectType", "Biological specimen")
    H.resetNameField()
    H.setQ("species", "Mus musculus")
    H.setQ("modality", "Micro CT (or synchrotron)")
    H.setQ("imageContents", "Whole specimen")
    H.pump(80)
    name = H.form().questions["githubRepoName"].answer()
    assert name == "mus-musculus-microct-whole", f"F4 prefill was {name!r}"


def _create_repotype_shortterm_personal():
    H.goTab("Create")
    H.w.createUI.shortTermRadio.click()
    H.pump(50)
    assert not H.w.selectedDestinationIsOrganization(), "short-term should be a personal destination"


def _release_make_disabled_initially():
    H.goTab("Release")
    H.pump(50)
    assert not H.w.releaseUI.makeReleaseButton.enabled, \
        "Make Release should be disabled with no baseline/color/repo loaded"


def _review_widgets_present():
    H.goTab("Review")
    H.pump(20)
    cb = H.w.reviewUI.hideDraftsCheckBox
    before = cb.checked
    cb.click(); H.pump(10)
    assert cb.checked != before, "hideDrafts toggle did not flip"
    cb.click()


def _logic_whoami():
    who = H.logic.whoami()
    assert who, "whoami returned empty"


def _logic_mixins_touch():
    # cheap, side-effect-free methods spanning several Logic mixins
    assert H.logic.localRepositoryDirectory(), "RepoMixin.localRepositoryDirectory empty"
    assert H.logic.controlPlaneBase(), "ControlPlaneMixin.controlPlaneBase empty"
    assert H.logic.volumeChecksumIndexURL(), "ObjectStoreMixin.volumeChecksumIndexURL empty"


def _baseline_nochange_helper():
    # Unit-touch of the M6 no-change check: a file compared to itself is 'unchanged'.
    import tempfile, os, shutil
    seg = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "harness-seg")
    seg.GetSegmentation().AddEmptySegment("a")
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "baseline.seg.nrrd")
        assert slicer.util.saveNode(seg, p, {'useCompression': True}), "saveNode failed"
        assert H.w._baselineMatchesCommittedFile(seg, p) is True, "identical file reported as changed"
        seg.GetSegmentation().AddEmptySegment("b")     # now it differs (segment count up)
        assert H.w._baselineMatchesCommittedFile(seg, p) is False, "changed seg reported as unchanged"
    finally:
        slicer.mrmlScene.RemoveNode(seg)
        shutil.rmtree(d, ignore_errors=True)


def _create_repotype_toggle():
    # Toggling the Q0 radios drives _onRepoTypeChanged without error and flips the selection.
    H.goTab("Create")
    H.w.createUI.archivalRadio.click()
    H.pump(120)
    assert H.w.createUI.archivalRadio.checked and not H.w.createUI.shortTermRadio.checked
    H.w.createUI.shortTermRadio.click()
    H.pump(40)
    assert H.w.createUI.shortTermRadio.checked
    assert not H.w.selectedDestinationIsOrganization(), "short-term should be personal"


def _search_tab_touch():
    H.goTab("Search")
    H.pump(20)
    H.w.updateSearchResults({})               # SearchTabMixin method; clears the table safely
    assert H.w.searchUI.resultsTable is not None


def _configure_tab_touch():
    H.goTab("Configure")
    H.pump(20)
    H.w.updateGitConfigInfo()                  # ConfigureTabMixin method
    assert H.w.configureUI.userNameLineEdit is not None


def _annotate_tab_touch():
    H.goTab("Annotate")
    H.pump(20)
    H.w.updateScreenshotCount()                # AnnotateTabMixin method
    assert H.w.annotateUI.commitButton is not None


def _stress_empty_segmentation_guard():
    # Part E: an empty baseline segmentation must be detected (would publish an empty baseline).
    seg = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "stress-empty")
    try:
        assert H.w._segmentationIsEmpty(seg), "empty segmentation not detected"
        seg.GetSegmentation().AddEmptySegment("a")
        assert not H.w._segmentationIsEmpty(seg), "non-empty segmentation flagged as empty"
    finally:
        slicer.mrmlScene.RemoveNode(seg)


def _stress_continuous_colortable_guard():
    # Part E: a continuous/built-in colormap must be flagged; a File/User terminology table must not.
    assert H.w._colorTableNotTerminology(slicer.util.getNode("Rainbow")), "Rainbow (continuous) not flagged"
    assert H.w._colorTableNotTerminology(slicer.util.getNode("Grey")), "Grey (continuous) not flagged"
    assert not H.w._colorTableNotTerminology(slicer.util.getNode("GenericAnatomyColors")), \
        "File terminology table wrongly flagged"


def _stress_invalid_repo_name():
    # Part E: a malformed repo name must keep Create disabled (form regex guard).
    H.goTab("Create")
    H.fillValidForm(name="valid-name", shortTerm=True)
    assert H.w.createUI.createRepository.enabled, "valid name should enable Create"
    f = H.form()
    f.userEditedRepoName = True
    f.questions["githubRepoName"].answerText.text = "bad name!"   # space + '!' -> invalid
    H.pump(60)
    assert not H.w.createUI.createRepository.enabled, "invalid repo name should keep Create disabled"


TESTS = [
    ("stress_empty_segmentation_guard", _stress_empty_segmentation_guard),
    ("stress_continuous_colortable_guard", _stress_continuous_colortable_guard),
    ("stress_invalid_repo_name", _stress_invalid_repo_name),
    ("create_redistribution_gate", _create_redistribution_gate),
    ("create_f4_name_suggestion", _create_f4_name_suggestion),
    ("create_repotype_shortterm_personal", _create_repotype_shortterm_personal),
    ("create_repotype_toggle", _create_repotype_toggle),
    ("release_make_disabled_initially", _release_make_disabled_initially),
    ("review_widgets_present", _review_widgets_present),
    ("search_tab_touch", _search_tab_touch),
    ("configure_tab_touch", _configure_tab_touch),
    ("annotate_tab_touch", _annotate_tab_touch),
    ("logic_whoami", _logic_whoami),
    ("logic_mixins_touch", _logic_mixins_touch),
    ("baseline_nochange_helper", _baseline_nochange_helper),
]
