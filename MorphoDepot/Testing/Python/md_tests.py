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


def _logic_gitpython_refreshed():
    # GitPython is imported with its executable search silenced so a machine without git can
    # still load the module, which means the logic MUST hand it a git for git.Repo() to work.
    import git
    assert H.logic.refreshGitPython(), "refreshGitPython() failed for a resolved git"
    assert git.GIT_OK, "GitPython has no git executable after the logic was built"


def _logic_tool_path_environment():
    # gh runs git BY NAME, so whatever PATH a gh child gets must contain both tools' directories
    # -- otherwise a git picked in the Configure tab but absent from the system PATH gives
    # "unable to find git executable in PATH" on clone.  Empty update == already reachable.
    import os
    update = H.logic.toolPathEnvironmentUpdate()
    childPath = update.get("PATH") or slicer.util.startupEnvironment().get("PATH", "")
    entries = {os.path.normcase(os.path.normpath(entry))
               for entry in childPath.split(os.pathsep) if entry}
    for executablePath in (H.logic.gitExecutablePath, H.logic.ghExecutablePath):
        if not executablePath:
            continue  # nothing configured yet -- checkGitDependencies() is what reports that
        directory = os.path.normcase(os.path.normpath(os.path.dirname(executablePath)))
        assert directory in entries, f"{executablePath} directory missing from the gh child PATH"


def _logic_git_noninteractive_environment():
    # A git child that finds no credential must FAIL rather than try to ask: Slicer has no console
    # for a terminal prompt, and an askpass inherited from whatever launched it is not connected to
    # anything.  Building the logic is what applies this, so it holds for every git child that
    # follows -- the pushes in contribute/release/accession included.
    import os
    for key, value in H.logic.gitNonInteractiveEnvironment.items():
        assert os.environ.get(key) == value, f"{key} was not applied to the git child environment"


def _logic_repository_credentials():
    # The one path here that WRITES configuration.  Two invariants matter, and the second is the
    # one that keeps a shared machine honest: the helper must delegate to gh (so the push follows
    # the active gh account rather than whatever credential the machine had stored for github.com),
    # and it must be preceded by an empty entry, because git ACCUMULATES helpers across config
    # scopes -- without the reset a global osxkeychain/manager entry answers first and wins.
    import os, tempfile, shutil, git
    repoDirectory = tempfile.mkdtemp(prefix="md-credential-test-")
    try:
        repo = git.Repo.init(repoDirectory, initial_branch="main")
        assert H.logic.configureRepositoryCredentials(repo), "configureRepositoryCredentials failed"

        values = repo.git.config("--local", "--get-all", "credential.https://github.com.helper")
        entries = values.split("\n")
        assert entries[0] == "", f"helper list is not reset first: {entries!r}"
        assert len(entries) == 2, f"expected a reset plus one helper, got {entries!r}"
        assert "auth git-credential" in entries[1], f"helper does not delegate to gh: {entries[1]!r}"
        assert H.logic.ghExecutablePath in entries[1], f"helper is not the configured gh: {entries[1]!r}"

        # Written to THIS repository only -- the user's global config is not MorphoDepot's to edit.
        assert repo.git.config("--local", "credential.interactive") == "false"
        localConfig = os.path.join(repoDirectory, ".git", "config")
        assert "auth git-credential" in open(localConfig).read(), "helper did not land in the repo config"
    finally:
        shutil.rmtree(repoDirectory, ignore_errors=True)


def _logic_missing_credential_detection():
    # The three ways git reports "nobody could give me a credential", across platforms and across
    # whether prompting was disabled.  A real push rejection must NOT be mistaken for one, or the
    # user would be told to fix their sign-in when the actual problem is something else.
    import git
    for stderr in ("fatal: could not read Username for 'https://github.com': Device not configured",
                   "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
                   "error: failed to execute prompt script (exit code 1)"):
        error = git.exc.GitCommandError(["git", "push"], 128, stderr)
        assert H.logic.isMissingCredentialError(error), f"not recognized: {stderr}"
    rejected = git.exc.GitCommandError(
        ["git", "push"], 1, "! [rejected] main -> main (non-fast-forward)")
    assert not H.logic.isMissingCredentialError(rejected), "a push rejection was read as a sign-in failure"


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
    terminologyNode = slicer.util.getNode("GenericAnatomyColors")
    assert terminologyNode is not None, "GenericAnatomyColors not in scene (positive case can't run)"
    assert not H.w._colorTableNotTerminology(terminologyNode), "File terminology table wrongly flagged"
    # a user-built table reports type 'UserDefined' (NOT 'User') and must not be flagged
    ud = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLColorTableNode", "stress-ud")
    ud.SetTypeToUser()
    ud.SetNumberOfColors(2)
    ud.SetColor(1, "A", 1.0, 0.0, 0.0, 1.0)
    try:
        assert ud.GetTypeAsString() == "UserDefined", f"unexpected user type {ud.GetTypeAsString()!r}"
        assert not H.w._colorTableNotTerminology(ud), "UserDefined table wrongly flagged"
    finally:
        slicer.mrmlScene.RemoveNode(ud)


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


def _version_change_filter():
    # Issue #203: only commits touching the installed module should raise a notice.
    logic = H.w.logic
    assert logic.moduleFilesChanged(["MorphoDepot/MorphoDepotLib/logic_repo.py"], 1), \
        "a module file should count as a change"
    assert not logic.moduleFilesChanged([".github/workflows/claude.yml", "README.md"], 2), \
        "docs and workflow changes should not raise an update notice"
    assert not logic.moduleFilesChanged(["MorphoDepot/Testing/Python/md_tests.py"], 1), \
        "Testing/ is stripped by the extension build and should not count"
    assert logic.moduleFilesChanged(["README.md"], 300), \
        "a truncated file list should be treated as a change rather than under-reported"


def _version_installed_shape():
    # Whatever the install shape, detection must return a usable record and never claim
    # it can write somewhere it cannot.
    logic = H.w.logic
    installed = logic.installedVersion()
    assert installed["shape"] in (
        "extension", "clone", "devclone", "buildtree", "unknown"), f"odd shape {installed['shape']!r}"
    assert installed["moduleDirectory"], "the module directory should always resolve"
    if installed["canUpdate"]:
        assert installed["sha"], "an updatable install must know its revision"
        assert logic._directoryIsWritable(
            installed["repositoryRoot"] or installed["moduleDirectory"]), \
            "canUpdate was set for a directory that is not writable"
    else:
        assert installed["blockedReason"], "a non-updatable install should explain why"


def _version_banner_hidden_when_current():
    # The banner is a notification: it must stay out of the way unless there is news.
    H.w._versionInstalled = {"shape": "extension", "sha": "abc1234", "date": "2026-07-18",
                             "canUpdate": True, "blockedReason": ""}
    H.w._versionStatus = {"available": False, "latestSha": "abc1234", "latestDate": "2026-07-18",
                          "aheadBy": 0, "compareUrl": "", "error": ""}
    H.w._refreshVersionDisplay()
    H.pump(20)
    assert not H.w.versionBanner.visible, "banner should be hidden when up to date"

    H.w._versionStatus = {"available": True, "latestSha": "def5678", "latestDate": "2026-07-27",
                          "aheadBy": 12, "compareUrl": "https://example.invalid/compare", "error": ""}
    H.w._refreshVersionDisplay()
    H.pump(20)
    assert H.w.versionBanner.visible, "banner should appear when an update is available"
    assert "def5678" in H.w.versionBannerLabel.text, "banner should name the available version"

    # Dismissing hides that version only, so the next release speaks up again.
    H.w.onVersionDismiss()
    H.pump(20)
    assert not H.w.versionBanner.visible, "dismiss should hide the banner"
    H.w._refreshVersionDisplay()
    H.pump(20)
    assert not H.w.versionBanner.visible, "a dismissed version should stay dismissed"
    H.w._versionStatus["latestSha"] = "999aaaa"
    H.w._refreshVersionDisplay()
    H.pump(20)
    assert H.w.versionBanner.visible, "a newer version should not stay dismissed"
    H.w.onVersionDismiss()
    H.w._versionInstalled = None
    H.w._versionStatus = None


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
    ("logic_gitpython_refreshed", _logic_gitpython_refreshed),
    ("logic_tool_path_environment", _logic_tool_path_environment),
    ("logic_git_noninteractive_environment", _logic_git_noninteractive_environment),
    ("logic_repository_credentials", _logic_repository_credentials),
    ("logic_missing_credential_detection", _logic_missing_credential_detection),
    ("baseline_nochange_helper", _baseline_nochange_helper),
    ("version_change_filter", _version_change_filter),
    ("version_installed_shape", _version_installed_shape),
    ("version_banner_hidden_when_current", _version_banner_hidden_when_current),
]
