# MorphoDepot — UI-Driven Test Plan

Status: **PLAN ONLY — do not execute until explicitly told "go".**
Target under test: [`MorphoDepot/MorphoDepot.py`](../MorphoDepot.py) (~6,600 lines, single tabbed module).
Author/driver model: Claude Code driving 3D Slicer through the **Slicer MCP server**.

---

## 0. Purpose & non-negotiable constraints

The module has grown past the point where its decision trees can be tested by hand.
This plan defines a repeatable, **black-box, UI-event-driven** test suite that a coding
agent (or a human following the same steps) can run against a live Slicer instance.

Hard rules agreed with the maintainer (do not relax without asking):

1. **Drive Slicer through the MCP server only.** Tooling: `mcp__slicer__execute_python`,
   `mcp__slicer__screenshot`, `mcp__slicer__list_nodes`, `mcp__slicer__get_node_properties`,
   `mcp__slicer__load_sample_data`, `mcp__slicer__read_file`, `mcp__slicer__write_file`,
   plus `gh`/`git` via Bash for out-of-band setup and verification.
2. **Signal-level UI actuation is the *only* legal way to perform the action under test.**
   Allowed: locate a real widget and fire its user-facing signal —
   `button.click()`, `lineEdit.setText(...)` (+ `editingFinished`), `comboBox.setCurrentIndex(...)`,
   `qMRMLNodeComboBox.setCurrentNode(...)`, `radio.click()`, `listWidget.itemDoubleClicked(item)`.
   **Forbidden as a way to trigger behavior:** calling slot/handler methods directly
   (`widget.onCreateRepository()`, `widget.onCommit()`, `widget.onMakeRelease()`, …) or calling
   `MorphoDepotLogic` methods to *do* the thing (`logic.createAccessionRepo(...)`,
   `logic.publishStagedRepo(...)`, `logic.loadIssue(...)`, …).
3. **Three real, already-logged-in GitHub accounts**, switched under the hood:
   - `muratmaga` — MorphoDepot **org owner** (also a member)
   - `amm554` — MorphoDepot **org member** (not owner of arbitrary repos)
   - `SlicerMorph` — **outsider** (not a MorphoDepot org member)
4. **Full real environment.** Real org repos, real S3 uploads, real GitHub releases, real
   Zenodo DOI minting, real contact-list email. Clean up afterward. See §4.2 — some effects
   (minted DOIs) are **not reversible**; those scenarios require explicit per-run go-ahead.

> **What this is NOT:** it is not the existing `MorphoDepotTest` self-test (class at
> `MorphoDepot.py:6172`). That test calls handlers and logic methods directly and
> monkeypatches the search layer — exactly the style this plan rejects. It remains a useful
> **coverage reference** (it already walks create→issue→PR→review→release end-to-end), and the
> scenario catalog below mirrors its coverage, but every action here is fired through the GUI.

---

## 1. Test architecture

### 1.1 Roles — who does what

| Actor | Mechanism | Responsibility |
|---|---|---|
| **Driver (agent)** | MCP `execute_python` / `screenshot` | Fires widget signals, reads widget state, captures screenshots |
| **Verifier** | `gh`/`git` via Bash, MCP node reads | Confirms authoritative GitHub state + MRML scene state |
| **Fixture/teardown** | `gh`/`git` via Bash | Seeds long-lived test repos, cleans up afterward |

`gh`/`logic` calls are allowed for **setup and verification only** — never to perform the
step that is itself under test. (E.g. it's fine to verify a PR exists with `gh pr list`; it is
**not** fine to create that PR with `gh pr create` when the test is "segmenter commits via the
Annotate tab".)

### 1.2 The "legal action" rule, made concrete

Every action in the catalog (§6) is expressed as a `(widget, signal)` pair. The driver
resolves the widget off the live widget object and fires the signal. Reference map of
addressable widgets (from `setup()` / `Connections`, `MorphoDepot.py:160–693`):

```
w = slicer.modules.MorphoDepotWidget
# Configure
w.configureUI.repoDirectory / .gitPath / .ghPath / .reloadButton ("Apply Changes")
w.configureUI.userNameLineEdit / .userEmailLineEdit / .adminModeCheckBox
# Create
w.createUI.archivalRadio / .shortTermRadio
w.createUI.inputSelector (qMRMLNodeComboBox) / .colorSelector / .segmentationSelector
w.createUI.accessionForm.questions[<key>].optionButtons[<label>] / .answerText
w.createUI.autoAssignCheckBox / .autoAssignHelpButton
w.createUI.createRepository ("Create (stage privately)") / .saveEditsButton
w.createUI.stagedReposList / .refreshStagedReposButton
w.createUI.goLiveEmail / .destinationQuestion / .publishButton ("Make Public") / .discardButton
w.createUI.takeScreenshotButton / .reviewScreenshotsButton / .openRepository / .clearForm
# Annotate (segmenter)
w.annotateUI.refreshButton / .issueList / .prList / .messageTitle / .commitButton / .reviewButton / .openPRPageButton
# Review (owner)
w.reviewUI.refreshButton / .prList / .hideDraftsCheckBox / .requestChangesButton / .approveButton
# Release (owner)
w.releaseUI.refreshButton / .repoList / .newBaselineSelector / .newColorSelector
w.releaseUI.releaseCommentsEdit / .makeReleaseButton / .openReleasePageButton
w.releaseUI.announcementDeadline / .announcementMessageEdit / .announceButton
# Search
w.searchUI.refreshButton / .searchForm / .resultsTable / .saveSearchResultsButton
# Tabs
w.tabWidget  (switch tabs with w.tabWidget.setCurrentIndex(i) — a user clicking the tab)
```

To "double-click an issue/PR/repo" like a user, select the row and emit the item signal:
```python
lw = w.annotateUI.issueList
item = lw.item(row)
lw.setCurrentItem(item)
lw.itemDoubleClicked(item)     # fires the connected slot via the real signal
```

### 1.3 `testingMode` stays OFF — and Developer mode stays OFF by default

`widget.testingMode` is **not a user-facing toggle**. It is initialized `False` in
`__init__` (`MorphoDepot.py:143`) and is flipped to `True` in exactly one place — the
`MorphoDepotTest` self-test (`:6190`, reset in `:6201`). This suite **never sets it True**, so
it stays `False` throughout. That matters because `testingMode` does **two** things
(`grep testingMode`):
- auto-skips ~18 modal confirm dialogs, **and**
- **changes real behavior**: `onCreateRepository` (`:1368`) reroutes creation into the throwaway
  `morphoDepotTestingOrg` with a *release-asset* payload and **no App/S3**.

So testingMode=True would both hide the dialogs we want to test *and* bypass the real archival →
org → S3 → DOI path — defeating a full-real-environment run. We keep it `False` and answer the
live modals like a user (§1.4).

**Developer mode is OFF by default**, because that is the faithful end-user configuration. The
testing affordances are *only* exposed under `Developer/DeveloperMode`: the Configure **Testing**
collapsible with the Creator/Annotator fields (`:265`) and the **Fill Form for Testing** button
(`:346`). With Developer mode OFF these are hidden, so the accession form is filled field-by-field
through real widget signals — exactly what an end user does.

> Developer mode may be turned **ON** only as a *convenience* to use the **Fill Form for Testing**
> shortcut while iterating. Turning it on does **not** change `testingMode` (still `False`) and
> does not alter any tested code path — but it is a deviation from the pristine end-user state, so
> the canonical P0 pass of the create flow (B1) is run at least once with Developer mode **OFF**
> and the form filled manually.

### 1.4 Modal dialogs — the central hazard

With `testingMode` off, these block on a modal `exec_()` and must be answered like a user.
Inventory (line refs in `MorphoDepot.py`):

| Where | Line | Dialog | Default test response |
|---|---|---|---|
| Missing terminology on color table | 1302 | OK/Cancel | OK (fill defaults) — or pre-fix the table (see §2) |
| Create confirmation (accession summary) | 1383→1748/1804 | custom OK/Cancel | OK to proceed; **Cancel** path is its own scenario |
| Duplicate volume checksum | 1635/1638 | OK/Cancel | scenario-specific |
| Publish repository | 1646 | OK/Cancel | OK |
| Discard staged repo | 1712/1720 | OK/Cancel | OK |
| Load issue / PR / repo (scene clear) | 2041 / 2204 / 2267 | OK/Cancel | OK |
| Make release | 2645 / 2667 | OK/Cancel | OK |
| Release failed | 2854 | OK/Cancel | observe only |
| Announce upcoming release | 2876 / 2970 | OK/Cancel | OK |
| Preview repository | 2980 | OK/Cancel | OK |
| `errorDisplay` / `messageBox` (e.g. archival-requires-membership 1373; commit-failed 2138) | several | single OK | OK + assert text |
| **Native file dialog** (Save search CSV) | 2577 | OS save dialog | special case (§6 S7) |

**Strategy — arm an in-Slicer dialog auto-responder, then fire the action.**
This is faithful: it does nothing but wait for whatever top-level modal appears and press one
of *its* buttons — exactly what a human does — without calling any module handler. It also
solves the deadlock: `execute_python` returns immediately; the timer answers the modal from
inside Slicer's (nested) event loop.

```python
# Installed once per session; armed before each dialog-bearing action.
def _md_arm_dialog(button="ok", capture_path=None, timeout_ms=8000):
    """Wait for the top modal widget, screenshot+record its text, then click OK/Cancel.
    Pure UI: it presses the dialog's own button; it never calls a module slot."""
    import qt, time
    state = {"text": None, "clicked": None}
    slicer.modules._mdDialogState = state
    deadline = time.time() + timeout_ms/1000.0
    def poll():
        dlg = slicer.app.activeModalWidget() or qt.QApplication.activeModalWidget()
        if dlg is None:
            if time.time() < deadline:
                qt.QTimer.singleShot(100, poll)
            return
        state["text"] = dlg.windowTitle + " | " + (getattr(dlg, "text", lambda: "")() if hasattr(dlg, "text") else dlg.findChild(qt.QLabel).text if dlg.findChild(qt.QLabel) else "")
        if capture_path:
            dlg.grab().save(capture_path)
        # Find the right button by role.
        bb = dlg.findChild(qt.QDialogButtonBox)
        target = None
        if bb:
            role = qt.QDialogButtonBox.Ok if button == "ok" else qt.QDialogButtonBox.Cancel
            target = bb.button(role) or (bb.button(qt.QDialogButtonBox.Yes) if button=="ok" else bb.button(qt.QDialogButtonBox.No))
        if target is None:  # QMessageBox path
            for b in dlg.findChildren(qt.QPushButton):
                if (button=="ok" and b.text.lower() in ("ok","yes")) or (button=="cancel" and b.text.lower() in ("cancel","no")):
                    target = b; break
        if target: target.click()
        state["clicked"] = button
    qt.QTimer.singleShot(100, poll)
```

> **#1 risk to validate in the smoke test:** whether the Slicer MCP server can still service
> `execute_python` *while a modal `exec_()` runs*. Nested Qt event loops normally keep
> processing queued/timer/socket events, so the armed timer above should fire regardless —
> but this MUST be proven on day one. Fallback if the server is starved during a modal: rely
> entirely on the pre-armed timer (no second MCP round-trip needed while blocked), which is why
> the responder is *armed before* the action rather than handled after.

### 1.5 Persona / account switching protocol

Identity is derived from the **active `gh` account**, and several reads are **cached per
module-load** — `userIsOrgMember` caches `_orgMemberCache` (`:3828`, comment literally says
"reload the module after switching gh accounts"); `whoami` reads `gh auth status --active`
(`:4218`); git `user.name`/`user.email` are read from git config.

**Switch routine (between personas):**
1. `gh auth switch --user <login>` (Bash). *(This is allowed — it is environment setup, not the
   action under test. Note: the existing self-test does the equivalent via `logic.gh([...])`.)*
2. In the Configure tab, set `user.name` / `user.email` to match the persona if a commit will
   happen (drive `userNameLineEdit.setText` / `userEmailLineEdit.setText`).
3. **Click "Apply Changes"** (`configureUI.reloadButton` → `onReload`) to reload the module and
   drop cached identity/membership. Re-resolve `w = slicer.modules.MorphoDepotWidget` afterward
   (the widget object is rebuilt).
4. Verify: read `w.logic.whoami()` and (for org-gated steps) confirm the control-plane `/me`
   membership via the UI's behavior, not by calling the gated method to perform an action.

**State isolation:** the local repo dir is a single setting, but a segmenter clones a *fork*
and an owner clones *upstream*; mixing accounts in one dir invites collisions. Use a **distinct
`repoDirectory` per persona** (set via the Configure `repoDirectory` widget on switch), e.g.
`…/md-uitest/<login>/`.

### 1.6 Verification channels (in priority order)

1. **Authoritative GitHub state** via `gh` (Bash) under the *appropriate* account: repo exists,
   visibility, topics (`morphodepot` added / `morphodepot-staging` removed), issues, PR state &
   draft flag, reviews, merges, releases, release assets, tags.
2. **MRML scene state** via `mcp__slicer__list_nodes` / `get_node_properties`: segmentation and
   volume nodes loaded after a load action; new segment present after an edit.
3. **Widget state** via `execute_python` *reads* (reading is always allowed): label text
   (`stagingStatusLabel`, `announcementStateLabel`, repo-row text/tooltip), `enabled` flags
   (`publishButton.enabled`, `commitButton.enabled`, `makeReleaseButton.enabled`), list contents.
4. **Screenshots** (`mcp__slicer__screenshot`) at every assertion point and for every dialog —
   the human-auditable trail.

### 1.7 Naming & teardown

- Repo names: `uitest-<persona>-<species>-<runid>` (runid from Bash timestamp passed in, since
  scripts can't read the clock). Mirrors the self-test's `test-…` convention.
- S3 keys are content-addressed by sha256 — re-running with the same volume is idempotent.
- Teardown (Bash, end of run, in `finally` spirit): `gh repo delete <nwo> --yes` for every repo
  created this run (needs `delete_repo` scope; if absent, print the Danger-Zone URL like
  `_cleanupTestRepo` does, `:6203`). Close any leftover issues/PRs. **DOIs cannot be deleted** —
  see §4.2.

---

## 2. Pre-flight checklist (run once, before any scenario)

- [ ] **Target the correct Slicer: the build on the Desktop** (`~/Desktop/Slicer*.app`), **not**
      the stable release in `/Applications`. Every scenario runs against the Desktop build.
- [ ] **Developer mode OFF before the run — verify and, if needed, restart.** It is OFF by default
      but is currently ON on this machine. So: check the setting, set it OFF, and **restart Slicer**.
      The MCP server is launched from Slicer's startup script, so restarting Slicer automatically
      relaunches MCP. After restart, confirm the `mcp__slicer__*` tools resolve in-session.
- [ ] 3D Slicer (Desktop build) is running with the **MorphoDepot** module loaded and MCP attached.
- [ ] **Developer mode OFF** (`Developer/DeveloperMode`) — this is the faithful end-user config;
      `testingMode` stays `False`. Turn it ON *only* if a scenario explicitly opts into the
      *Fill Form for Testing* convenience (it then reveals the Configure *Testing* collapsible and
      the *Fill Form* button; it does **not** flip `testingMode`). B1 is run at least once with it OFF.
- [ ] `gh auth status` shows all three accounts logged in; note which is active.
- [ ] Dependencies green: `checkPythonDependencies`, `checkGitDependencies`, valid
      `localRepositoryDirectory` (else `checkModuleEnabled` blocks with a modal, `:82–125`).
- [ ] Control plane reachable (`https://join.morphodepot.org`) for membership + S3 signing.
- [ ] A real **source volume** available (MRHead via `mcp__slicer__load_sample_data`) and a
      **color table with complete terminology** (or plan to OK the terminology dialog — §1.4).
- [ ] Per-persona repo directories created.
- [ ] Install the harness helpers (§5) into the running Slicer once.

---

## 3. Persona × capability matrix (the heart of the 3-account test)

| Capability | `muratmaga` (owner) | `amm554` (member) | `SlicerMorph` (outsider) |
|---|---|---|---|
| Create **archival** repo (org, S3, DOI) | ✅ | ✅ | ❌ → `errorDisplay` "Archival requires membership" (`:1372`) |
| Create **short-term** repo (personal acct) | ✅ | ✅ | ✅ |
| See **org** as publish destination | ✅ | ✅ | ❌ (personal only; `populateOwnerSelector`) |
| Auto-assign workflow checkbox enabled | scope-gated (`hasWorkflowScope` `:4061`) | scope-gated | scope-gated |
| Segment an assigned issue → commit → PR (Annotate) | ✅ | ✅ | ✅ via fork on public repo |
| Approve + **merge** a PR (Review) | ✅ on owned/org repos | only where granted | ❌ |
| Cut a **release** + mint DOI (Release) | ✅ on org repos | only where admin | ❌ |
| Admin tab | ✅ (admin mode) | per role | per role |

Each ✅/❌ above is an assertion target. The outsider's ❌ rows are first-class scenarios, not
afterthoughts — they exercise the permission guards that are easy to break.

---

## 4. Known functional constraints that shape the tests

### 4.1 Search-index lag — **the biggest practical constraint**

Annotate/Review/Release/Search refresh buttons resolve repos/issues/PRs through GitHub's
**topic search index** (`topic:MorphoDepot`) and `issue list --search`, which **lag by minutes**
for freshly created repos/issues/PRs. The existing self-test only passes by **monkeypatching**
`morphoRepos`, `issueList`, `prList`, and `closedIssuesSinceLastRelease` to hit direct REST
(`:6353, :6415, :6497–6554`). **A UI-driven test cannot monkeypatch** — clicking *Refresh* runs
the real, lagging path, so a just-created repo's issues/PRs may simply **not appear**.

Mitigation (simple, confirmed by maintainer): **insert a fixed ~60 s settle delay** after any
create / assign / commit / merge, before clicking the relevant *Refresh* and asserting. RepoClerk
now updates fast — typically **20–40 s** — so a 60 s wait clears the lag in the large majority of
cases. As a backstop, the module exposes `notifyRepoClerk` / `hasRepoClerkUpdatePending` /
`_waitForRepoClerkUpdate` and hidden per-tab `repoClerkStatusLabel`s; if a list is still empty
after 60 s, poll those once or twice more before failing.

Long-lived pre-seeded fixture repos remain an **optional** optimization (avoids recreating
archival/org repos every run), but with the 60 s settle delay, fresh-create flows are viable for
every group.

> Treat "list empty immediately after create" as *expected lag*, not a bug: always settle ~60 s
> (or poll RepoClerk) before a list-dependent assertion.

### 4.2 Irreversible / outward side effects

- **Zenodo DOI minting** (Release, archival repos): the extension mints against **Zenodo
  sandbox**, so these are **sandbox DOIs — not real, citable DOIs**. Safe to create freely; no
  human checkpoint needed. (Sandbox records can be removed from the sandbox account if desired;
  they never resolve as production DOIs.)
- **S3 uploads** (`uploadSourceVolumeToObjectStore`, `:3702`): content-addressed, public-read.
  Re-runs are idempotent; old test keys accumulate — periodic bucket sweep, not per-run.
- **Contact-list email** at publish time: real submission. Use a maintainer-owned address.
- **Org pollution**: archival creates land in the real MorphoDepot org. Teardown deletes repos,
  but the org membership/transfer events are visible. Name everything `uitest-…`.

---

## 5. Harness helpers (install once per session, in-Slicer)

These are *test scaffolding the driver injects via `execute_python`*, not edits to the module.
They only **read** state and **actuate widgets/dialogs** — never call module slots/logic to
perform an action.

- `_md_arm_dialog(button, capture_path, timeout_ms)` — §1.4 (modal responder).
- `_md_widget(path)` — resolve a dotted widget path off `slicer.modules.MorphoDepotWidget`.
- `_md_fire(path, signal, *args)` — fire a named signal/click on a resolved widget.
- `_md_list_rows(listpath)` / `_md_dblclick(listpath, predicate)` — read a QListWidget and
  double-click the row matching a predicate (by text), via the real `itemDoubleClicked` signal.
- `_md_state()` — dump a snapshot dict: `whoami`, active tab, key button `.enabled` flags, key
  label texts, segmentation/volume node counts — for fast assertions + logging.
- `_md_switch_persona(login, name, email, repodir)` — performs §1.5 steps 2–3 (the `gh auth
  switch` itself is a Bash call by the driver), then clicks **Apply Changes** and returns.

Each scenario step = (optionally arm dialog) → fire signal → `processEvents` → screenshot →
read state → assert (incl. `gh` verification under the right account).

---

## 6. Scenario catalog (core-first)

Legend: **P** = persona/account · **Pre** = preconditions · **Act** = UI actions (signal-level)
· **Dlg** = dialogs to answer · **Exp** = expected outcome · **Vfy** = verification · **Td** = teardown.
Priority **P0** = must pass first, **P1**/**P2** = later passes. (`testingMode` is `False` in every
scenario — see §1.3.)

### Group A — Configure & gating

- **A1 (P0) Identity & gates per account.** P: each of the three.
  Act: switch persona (§1.5); read Configure fields; toggle `adminModeCheckBox`.
  Exp: `whoami()` matches; git name/email reflect inputs; Admin tab appears/disappears with the
  checkbox; with a bad repoDir, `checkModuleEnabled` raises the directory modal (Dlg: OK).
  Vfy: `_md_state`, screenshot.

### Group B — Create / Publish (fresh repos)

- **B1 (P0) Owner creates ARCHIVAL repo end-to-end.** P: `muratmaga`.
  Pre: MRHead loaded; color table with terminology; **Developer mode OFF** (canonical run).
  Act: Create tab → `archivalRadio.click()` → set `inputSelector`/`colorSelector` → fill
  `accessionForm` field-by-field (radios via `optionButtons[...].click()`, text via
  `answerText.text=`; the *Fill Form for Testing* shortcut is unavailable with Developer mode OFF
  and is only an optional convenience in a separate `amm554` iteration) → `createRepository.click()`
  (Dlg: terminology? → OK; create
  confirmation → **OK**) → in Go-live zone set `goLiveEmail`, pick org destination →
  `publishButton.click()` (Dlg: Publish → **OK**).
  Exp: repo staged private then made public in the **MorphoDepot org**; topics flip
  (`morphodepot` added, `morphodepot-staging` removed); source volume uploaded to **S3**.
  Vfy (gh as muratmaga): `gh repo view`, `--json visibility,owner`, topic list, release/asset or
  S3 checksum index; `stagingStatusLabel` text. Td: `gh repo delete`.

- **B2 (P1) Member creates archival.** P: `amm554`. Same as B1; assert org membership lets it through.

- **B3 Non-member tries to create an ARCHIVAL repo** — the "user didn't understand archival
  (org-only) vs short-term (personal)" case. P: `SlicerMorph` (a confirmed non-member). Maintainer
  acceptance criteria: **(1) it must not be possible to actually create one, and (2) the message
  must make clear this is a membership *requirement*, not a software failure.** Split into three:

  - **B3a (P0) Proactive prevention — Archival not selectable for a confirmed non-member.**
    Pre: Create tab freshly entered as `SlicerMorph`.
    Exp (TARGET behavior): on Create-tab entry a membership check runs (mirroring
    `_refreshAutoAssignAvailability`, `:1063`); since `userIsOrgMember()` (`:3828`) is False, the
    **`archivalRadio` is disabled**, `shortTermRadio` is preselected, and an inline note/tooltip
    names the membership requirement + join link. Vfy: `archivalRadio.enabled is False`, tooltip
    text, short-term checked.
    > GAP: today nothing checks membership on tab entry, so the radio is enabled and this assertion
    > **fails against current code** — it documents the prevention the maintainer wants. Until it
    > lands, B3b is the only enforced guard.

  - **B3b (P0) Backstop — submit-time block (current behavior, keep even after B3a).**
    Act: select `archivalRadio.click()` → fill the **entire** accession form → `createRepository.click()`.
    Exp: the membership guard (`:1371`) aborts creation; **no repo created** on the personal account
    or in the org. Vfy: dialog captured by `_md_arm_dialog`; assert the text (a) names the
    membership requirement, (b) gives the join path, (c) offers Short-term as the alternative;
    `gh repo list SlicerMorph` and `gh repo list MorphoDepot` show nothing new.
    > QUALITY assertions tied to requirement 2: flag that the dialog uses `errorDisplay` (error/✕
    > styling) rather than a warning/info "Membership required"; that the join URL is plain text,
    > not an actionable button (cf. the `QMessageBox`+"Open Documentation" pattern at `:99`); and
    > that the copy does not re-explain archival vs short-term for a user who didn't grasp it.

  - **B3c (P1) Verification failure must NOT masquerade as "not a member."**
    Pre: make the control-plane `/me` check fail (point `MorphoDepot/controlPlaneBase` at an
    unreachable host) so `userIsOrgMember()` returns False by *error*, not by *fact*.
    Exp (TARGET behavior): the message distinguishes "couldn't verify membership right now
    (temporary, not your account)" from "you are not a member," and still offers Short-term.
    > GAP: `userIsOrgMember()` swallows the exception and returns False (`:3840-3843`), so a real
    > member hitting a network blip is wrongly told to "go join the org" — a software/infra problem
    > mislabeled as a membership problem, the exact failure mode requirement 2 forbids. Documents
    > the correctness gap.

- **B4 (P1) Short-term repo on personal account.** P: `SlicerMorph` (and/or `amm554`).
  Act: `shortTermRadio.click()` → fill → create → publish to **personal** account.
  Exp: repo on personal account, public; no org transfer; release-asset payload (no S3 org tier).

- **B5 (P1) Staged-repo recovery / resume.** P: `muratmaga`.
  Act: create+stage but **do not** publish → click **Apply Changes** (reload) → Create tab →
  `refreshStagedReposButton.click()` → `stagedReposList` double-click the staged repo →
  edit a field → `saveEditsButton.click()` ("Update Repository (staged)") → then `publishButton`
  OR `discardButton` (Dlg: Discard → OK).
  Exp: staged repo reloads into the form; edits persist; publish/discard behave correctly.
  Vfy: list contents pre/post; repo private→public or deleted.

- **B6 (P2) Edge branches:** duplicate-volume checksum confirm (`:1635` — seed a second repo with
  the same volume; Dlg appears); missing-terminology dialog Cancel path; create-confirmation
  **Cancel** path (Exp: aborted, no repo); auto-assign checkbox disabled when token lacks
  `workflow` scope (`hasWorkflowScope` False).

### Group C — Annotate (segmenter)  *(use fixture repos — §4.1)*

- **C1 (P0) Segmenter completes an assigned issue.** P: `amm554` (member) and `SlicerMorph`
  (outsider via fork).
  Pre: fixture repo with an open issue **assigned to this persona** (pre-seeded & indexed).
  Act: Annotate tab → `refreshButton.click()` (poll for issue to appear) → `issueList`
  double-click the issue (Dlg: "Close scene and load issue?" → OK) → make a segmentation edit
  (add a segment via the Segment Editor / `AddEmptySegment` on the loaded node — this is editing
  test data, not driving the module) → set `messageTitle.text` → `commitButton.click()` →
  `reviewButton.click()` (request review / mark PR ready).
  Exp: branch `issue-<n>` pushed; **draft PR** created, then marked ready.
  Vfy (gh): `gh pr list --json number,title,isDraft,author`; node counts via MCP. Note: the
  segmenter's own PR list may lag — poll.

- **C2 (P2) Annotate guards:** `commitButton`/`reviewButton` start disabled (`:521`); enable only
  after an issue is loaded + message entered (`onCommitMessageChanged`). Commit-conflict path
  surfaces the `messageBox` at `:2138`.

### Group D — Review (owner)  *(fixture repos)*

- **D1 (P0) Owner requests changes, then approves+merges.** P: `muratmaga`.
  Pre: open ready PR from C1 on a fixture repo the owner controls.
  Act: Review tab → `refreshButton.click()` (poll) → `prList` double-click (Dlg: load PR → OK) →
  `requestChangesButton.click()` → (segmenter addresses, D-loop with C1) → `approveButton.click()`.
  Exp: review submitted (CHANGES_REQUESTED then APPROVED); PR merged (squash) & issue closed.
  Vfy (gh): `gh pr view --json reviews,state,merged`; `hideDraftsCheckBox` toggles draft rows.

- **D2 (P0) Member/outsider CANNOT approve+merge others' PRs.** P: `amm554` or `SlicerMorph` on a
  repo they don't own. Exp: approve/merge fails or is not offered; assert the guard.

### Group E — Release (owner)  *(fixture repos; DOI checkpoint §4.2)*

- **E1 (P1) Owner cuts a release.** P: `muratmaga`.
  Pre: org repo with ≥1 merged PR (closed issue) and ≥1 still-open issue (for cleanup test).
  Act: Release tab → `refreshButton.click()` (poll) → `repoList` double-click the repo (Dlg: load
  → OK) → `newBaselineSelector.setCurrentNode(...)` + `newColorSelector.setCurrentNode(...)`
  (enables `makeReleaseButton` via `updateMakeReleaseEnabled`) → verify auto change-log in
  `releaseCommentsEdit` → `makeReleaseButton.click()` (Dlg: Make release vN → OK; cleanup confirm → OK).
  Exp: tagged release `vN`; baseline asset; change-log lists closed issues; lingering open issue
  closed by cleanup; **DOI minted** (sandbox unless cleared for prod).
  Vfy (gh): `gh release list/view`, assets, tag; issue states; row text shows
  `(N open issue, M open PRs)` + tooltip (`:6573`). Td: `gh release delete`, repo delete; DOI stays.

- **E2 (P1) Pre-release announcement.** P: `muratmaga`.
  Act: set `announcementDeadline`/`announcementMessageEdit` → `announceButton.click()` (Dlg: OK).
  Exp: announcement comment lands on each open issue (assert marker via `gh issue view`);
  `announcementStateLabel`/header reflect existing announcement.

- **E3 (P2) Release guards:** `makeReleaseButton` disabled until baseline+color chosen (`:2289`);
  non-owner cannot release; release-failure dialog path (`:2854`).

### Group F — Search (all personas, mostly read-only)

- **F1 (P1)** Search tab → `refreshButton.click()` ("Load Searchable Repository Data", poll for
  RepoClerk) → adjust `searchForm` criteria → results table populates → `resultsTable` double-click
  a row (Dlg: preview → OK) loads a preview scene → `saveSearchResultsButton.click()` opens the
  **native** save dialog. **Special case:** native OS file dialogs aren't Qt-button-clickable from
  `execute_python`; either (a) pre-set the path by overriding the static
  `QFileDialog.getSaveFileName` for that one call, or (b) assert up to the dialog opening and
  document the manual step. Vfy: row count, CSV contents (if path forced).

### Group G — Admin

- **G1 (P2)** Admin mode on (`muratmaga`) → Admin tab present; basic visibility/enablement checks
  (tab is a placeholder scroll area today, `:200–204`).

---

## 7. Execution order & session flow

1. **Pre-flight (§2)** — target the **Desktop** Slicer build, ensure **Developer mode OFF** and
   **restart Slicer** (which relaunches MCP), confirm MCP tools resolve → install helpers (§5) →
   smoke-validate the modal responder (§8).
2. **Fixtures:** seed long-lived indexed test repos for Groups C/D/E/F (§4.1), with issues
   assigned to `amm554`/`SlicerMorph`. Allow index to settle (minutes) before list-dependent runs.
3. **P0 pass:** A1 → B1 → B3 → C1 → D1/D2 → (E1 if DOI cleared). Stop & report.
4. **P1 pass:** B2, B4, B5, E1/E2, F1.
5. **P2 pass:** B6, C2, D-edge, E3, G1.
6. **Teardown (§1.7)** + DOI note.

Between every persona-spanning step, run `_md_switch_persona` (§1.5) and re-resolve the widget.

---

## 8. Open risks / validate in the smoke test (before trusting any result)

1. **Modal vs MCP (blocker).** Prove `execute_python` still runs while a `confirmOkCancelDisplay`
   modal is open, OR prove the pre-armed timer (§1.4) dismisses it without a second round-trip.
   Test with a trivial `confirmOkCancelDisplay` before B1.
2. **Reload re-resolves the widget.** After "Apply Changes", confirm `slicer.modules.
   MorphoDepotWidget` is the *new* instance and caches are cleared (membership re-checked).
3. **Settle timing (§4.1).** A ~60 s delay should clear RepoClerk lag (now ~20–40 s); confirm on
   the first create/assign and add a RepoClerk poll only if a list is still empty after 60 s.
4. **`gh auth switch` visibility to the running module.** Confirm a switch made via Bash is seen
   by the in-Slicer `gh` calls (same `gh` config/keyring) after reload — they share the host
   `gh`, so it should, but verify `whoami()` flips.
5. **DOI environment.** Confirmed **Zenodo sandbox** — DOIs are sandbox-only, not real; no checkpoint needed.
6. **Native file dialog** handling for F1 (§6).
7. **Cross-account local dir collisions** — verify per-persona `repoDirectory` prevents fork vs
   upstream clone clashes.

---

## Appendix — coverage cross-reference to the existing self-test

`MorphoDepotTest.test_MorphoDepot1` (`:6236`) already walks: create→publish (B1), issue
create/assign (fixture seeding), annotate commit→ready PR (C1), review request-changes/approve/
merge (D1), address-feedback loop (C1↔D1), release with change-log + cleanup (E1), pre-release
announcement (E2). This plan reproduces that coverage **through the GUI** and adds the
permission-matrix scenarios (B3, D2, archival/short-term split) that the handler-driven test
does not exercise.
