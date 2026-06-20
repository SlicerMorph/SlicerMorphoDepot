# MorphoDepot — Workflow-Test Net + Refactor Plan

The spec for two pieces of work, run back-to-back: (1) a **UI-driven workflow test net**,
then (2) a **structural refactor** of the 7k-line `MorphoDepot.py` performed behind that net.
This document is the executable plan; it is meant to be followed step by step.

## Why this order

- The tests are **UI-driven and black-box** (they click the real widgets and assert on
  observable outcomes), so they are *decoupled from internal structure*: written once against
  the stable UI, they survive the refactor unchanged and prove it preserved behavior.
- A 7k-line decision tree cannot be re-verified by hand. Refactoring it without a net leaks
  silent regressions into exactly the paths you can't manually reach. So: **net first, refactor
  behind it.** The suite staying green is the gate on every refactor commit — that is what makes
  the refactor safe to run hands-off.

## Scope (this phase)

- **Test net = workflow / happy-path only.** Well-behaved users, valid inputs, walking *every
  branch* of the decision tree. Decision-tree-complete, **not** input-validity-complete.
- **Refactor = relocation, not redesign.** Per-domain mixin split + two extracted I/O clients.
- **Explicitly deferred to after the refactor** (see Part E): bad-input / stress / validation
  testing (continuous color tables, empty or non-binary segmentations, malformed names, etc.).

---

## Part A — The workflow test net

### A.1 Harness (`MorphoDepot/Testing/Python/`)

- **Driver:** Slicer MCP, **signal-level** actuation — emit `button.clicked()`, set field text on
  the actual widget, select nodes in the real selector. Never call `onPublish()`/logic directly:
  the bugs that bite are UI-wiring bugs, which only surface through the real widgets.
  `testingMode` is **off** so production wiring runs.
- **Personas (`gh` accounts):** `muratmaga` (owner), `amm554` (member), `SlicerMorph` (outsider).
  Cases are **grouped by persona**; switch the active account at group boundaries and clear the
  extension's whoami / topic caches on switch.
- **State determinism (hardest part):** an idempotent `setup_state.py` creates/resets the
  `mdtest-*` repos to known preconditions; teardown returns them clean. Cases use isolated
  throwaway repos so they don't cross-contaminate.
- **Control-plane = live, behind a configurable base.** The harness sets `controlPlaneBase` to the
  live `join.morphodepot.org`; switching to a sandbox/fake service later is a one-line change.
- **App test-mode** (small addition to `morphodepot-intake`, gated by a test secret and limited to
  `mdtest-*` repos): for test requests, **redirect the review email to a sink** and **return the
  approve id** in the response. Keeps the gate logic 100% real (store → approve → execute → DOI)
  while making it drivable and quiet. Only the few *full-cycle* release/publish cases need it; most
  cases just assert the extension requested correctly and parked the repo as "pending".
- **Fixtures:** valid only, this phase — a proper terminology color table, a real baseline
  segmentation, a small source volume, etc.
- **Assertions:** observe **UI state** (dialog presence/text, button-enabled, status label,
  selector contents) *and* **side effects** (repo created/visibility, file committed, PR opened,
  tag cut) via `gh`.

### A.2 Coverage — decision tree, valid inputs

One data-driven case per branch: `(id, persona, precondition, UI steps, expected observable)`.

| Tab | Happy-path branches to cover |
|---|---|
| **Create** | archival (member) · short-term (anyone) · archival blocked for confirmed non-member · auto-assign on → workflow file present · F4 name prefill + "available" check · stage privately · reopen-for-edit (type selector hidden) · redistribution gate enables Create |
| **Publish** | with baseline → baseline-credit dialog → Done → `CONTRIBUTORS.json` written → **review gate** returns pending, repo stays staged · no-baseline → gate, no credit dialog · dup-volume note shown · Cancel aborts |
| **Annotate** | load assigned issue → segment → Commit (draft PR created) → Request review (PR flips ready) · screenshots add/caption/count |
| **Review** | list shows in-org contributor PR · load/clone PR · Approve → approve+squash-merge · Request-changes with message · self-authored Approve (skips GitHub approve, merges) · self-authored Request-changes (posts comment, drafts) · stale-panel re-click → clean "already merged" message |
| **Release** | list = archival-only · load · announce upcoming · Make Release (Option C): candidate branch pushed, gate pending, *(test-mode approve → pre-release archive + main fast-forward + tag + DOI + candidate deleted)* · contributor grid populated from merged `issue-N` PRs · close-open-items prompt · UI disarms after submit |
| **Search** | fetch cache → search → results → open / preview repo |
| **Configure** | git user/email, git/gh paths, admin mode, refresh-label updates |

### A.3 Phases

- **T1 — Harness milestone.** Build the harness, the `setup_state.py`, the App test-mode, and **one
  smoke case per tab**. This proves the whole loop end-to-end and de-risks everything after it.
- **T2 — Workflow matrix.** Fill in every branch in A.2. **T1 + T2 is the net the refactor runs
  against.**

---

## Part B — The refactor

### B.1 Slicer constraints that shape it

1. **Four classes must stay in `MorphoDepot.py`.** Slicer's module factory looks *by name* for
   `MorphoDepot`, `MorphoDepotWidget`, `MorphoDepotLogic`, `MorphoDepotTest`. They stay in the main
   file but become **thin**, inheriting behavior from a sibling package.
2. **Sibling package import.** Bulk code moves to a `MorphoDepotLib/` package in the module dir
   (sibling imports already resolve — cf. existing `MorphoDepotContributors.py`).
3. **The reload trap (must fix first).** Slicer's "Reload" re-imports only the main file;
   submodules stay cached in `sys.modules`, so edits to `MorphoDepotLib/*` would run **stale**.
   Override `onReload` to `importlib.reload` every `MorphoDepotLib.*` submodule first.
4. **Preserve every widget `objectName`.** The UI suite addresses widgets by name; the `.ui` files
   don't move and the Python UI classes keep their names, so this is free if we stay disciplined.

### B.2 Target layout

```
MorphoDepot/
  MorphoDepot.py              # thin: the 4 required classes + onReload submodule-reload
  MorphoDepotLib/
    __init__.py
    widget_create.py   widget_annotate.py  widget_review.py
    widget_release.py  widget_search.py     widget_configure.py
    logic_accession.py logic_release.py     logic_search.py  logic_repoclerk.py
    clients/
      github.py        # GitHubClient        (Phase 2)
      controlplane.py  # ControlPlaneClient  (Phase 2)
    forms.py  accession_form.py  search_form.py  screenshot_dialog.py
  Resources/UI/*.ui            # untouched
  Testing/Python/...           # the harness from Part A
```

### B.3 Technique

- **`MorphoDepotWidget` → per-tab mixins.** `class MorphoDepotWidget(ScriptedLoadableModuleWidget,
  CreateTabMixin, AnnotateTabMixin, ReviewTabMixin, ReleaseTabMixin, SearchTabMixin,
  ConfigureTabMixin)`. Near-mechanical cut-and-paste; `self.*` resolves across mixins via MRO, so
  **no call sites change**.
- **`MorphoDepotLogic` → domain mixins** (`AccessionMixin`, `ReleaseMixin`, `SearchMixin`,
  `RepoClerkMixin`, …), same mechanism; `self.logic.x()` unchanged.
- **Python-built UI → its own modules** (`forms`, `accession_form`, `search_form`,
  `screenshot_dialog`) — plain classes, trivial move (no `.ui` conversion).
- **Phase 2 — extract two I/O leaves into real client classes** (the only true decoupling worth the
  risk now):
  - `ControlPlaneClient(base_url, token_provider)` — its `base_url` **is** the seam that makes the
    live→sandbox/fake App swap a constructor argument (serves the sandbox-later goal + lets the
    harness aim at a fake later).
  - `GitHubClient(token_provider, progress)` — most-used leaf; unit-testable with no Slicer.
  - The Logic *holds* these (`self.controlPlane`, `self.github`) and keeps **thin delegating shims**
    (`def gh(self, c): return self.github.run(c)`) so existing call sites keep working.
  - Everything else stays a mixin.

### B.4 Sequence (each step = one revertable commit; suite must stay green)

- **R0** — scaffold `MorphoDepotLib/` + the `onReload` submodule-reload fix. Move *nothing*. Confirm
  Slicer loads and the suite is green.
- **R1** — relocate the Python-built UI classes (forms / accession_form / search_form /
  screenshot_dialog). Smallest blast radius.
- **R2** — split `MorphoDepotLogic` into domain mixins, one per commit, suite after each.
- **R3** — split `MorphoDepotWidget` into per-tab mixins, one per commit, suite after each.
- **R4** — Phase 2: extract `ControlPlaneClient` + `GitHubClient`, add delegating shims, point the
  harness's control-plane seam at the client.

---

## Part C — Combined order & gating

`T1 → T2` (net) → `R0 → R1 → R2 → R3 → R4`. The suite being green gates **every** refactor commit:
if a move changes any observable behavior, a case goes red and we stop and fix before continuing.
That gate is what makes running the refactor autonomously safe.

## Part D — Risks & mitigations

| Risk | Mitigation |
|---|---|
| Submodule reload trap (stale code after Reload) | `onReload` force-reloads `MorphoDepotLib.*` — done in R0, before any move |
| Widget `objectName` drift breaks the suite | Preserve all names; `.ui` untouched; Python UI keeps names |
| Test state non-determinism (GitHub/App) | Idempotent setup/teardown; isolated `mdtest-*` repos; App test-mode for drivable gate |
| Email volume / undrivable approve on live App | App test-mode: email→sink, approve-id returned, secret-gated, `mdtest-*`-only |
| Mixin split hides residual god-object coupling | Accepted this phase — mixins change *no behavior*; Phase 2 extracts the leaves that matter |

## Part E — Deferred to after the refactor (stress / hardening)

The bad-input & stress matrix, run against the now-refactored code: continuous/scalar color table
where terminology is required · empty segmentation · non-binary / overlapping segmentation ·
byte-identical color copy · baseline identical to committed · invalid/duplicate repo name · App
unreachable / 5xx · stale approve link · interrupted release retry. Several will surface **missing
validation** — that becomes the post-refactor hardening backlog (add guard → pin with a test).
