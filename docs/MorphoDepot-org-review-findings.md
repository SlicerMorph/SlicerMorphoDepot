# MorphoDepot — Code Review & Test Findings

Overnight review of `MorphoDepot.py` (~6.2k lines), `MorphoDepotContributors.py`, and the intake
App (`control_plane.py`, `mint_doi.py`). Five focused review passes + a dry run of the test plan.

- **FIXED** = corrected and committed in the accompanying PR (compiled + reloaded; verified where notable).
- **REPORTED** = left for your decision (design choice) or too risky to auto-fix without broader testing.

Severity is impact-if-triggered. Line numbers are approximate (the file shifted during fixes).

---

## FIXED in this PR

### HIGH
| ID | Where | Bug | Fix |
|----|-------|-----|-----|
| F-1 | `prList` (~4307/4324/4410) | `pr['author']['login']` is dereferenced unguarded, but `_journalsToTopicData` emits `author=None` for a deleted/ghost GitHub account → one such PR makes the **entire** Annotate/Review list raise `TypeError`. | Compute `prAuthor = (pr.get('author') or {}).get('login')`; build parties/author dict from it. |
| F-2 | `loadFromLocalRepository` (~4469) | `self.ghProgressMethod(...)` — **no such method** (typo for `progressMethod`); a release-load on a repo with no color table raises `AttributeError` and aborts the whole release. | `self.progressMethod("No color table found")`. |
| F-3 | `createRelease` (~5000) | `git.branch(backupName)` is not idempotent; a **retry** after a partial release failure crashes on "branch pre-release-vN already exists" (reset deliberately keeps the branch). | Delete the local backup branch if present, then recreate. |
| F-4 | `mint_doi.py` (~639) | `build_citation(creator, …)` references `creator`, which is **unbound** on the normal CONTRIBUTORS.json path and the `--creator` override — `NameError` *after* the DOI is already minted/published. | Use `creators[0]` (always defined). |

### MEDIUM
| ID | Where | Bug | Fix |
|----|-------|-----|-----|
| F-5 | `onPRSelectionChanged`/`onPRDoubleClicked` (~2003/2180) | Annotate & Review tabs share one `prsByItem` dict; selecting an Annotate PR after a Review refresh reads a stale item → `KeyError`. | `.get(item)` with a None guard in both reads. |
| F-6 | `onPublish` baseline-credit (~1665) | `_curateBaselineCreditViaApi` runs the `PUT CONTRIBUTORS.json`; a gh failure (sha conflict/5xx) escapes `onPublish` uncaught after the confirm. | Wrap in try/except → clean `errorDisplay`, abort publish. |
| F-7 | `mint_doi.py` (~582) | A blank-named author (curator exemption leaking past the in-repo gate) is minted as a Zenodo creator literally named `@handle` — a permanent citation. | Refuse to mint if any author has only a handle/no name. |

### LOW / robustness
| ID | Where | Bug | Fix |
|----|-------|-----|-----|
| F-8 | `whoami` (~4180) | `gh auth status --active`.split()`[7]` — fixed-index parse breaks if gh's wording changes; underpins every role match. | Regex `account\s+(\S+)`, fall back to `gh api user --jq .login`. |
| F-9 | `ghJSON` (~4018) | Docstring promises empty-list-on-error but `json.loads` was unguarded. | try/except → `[]` + warning. |
| F-10 | `gh()` else branch (~3958) | Non-str/list arg falls through to `UnboundLocalError`, masking the real error. | `raise TypeError`. |
| F-11 | `repositoryList` (~4302) | No `--limit` → gh caps at 30; loadIssue's fork-exists check misses real forks for users with >30 repos. | `--limit 1000` + use `ghJSON`. |
| F-12 | `mint_doi.py` (~224/291) | Unguarded `json.loads(rec_txt)` of an onboarding record aborts the mint mid-flight (after a draft deposition may exist). | try/except → MintError (curator) / return person (contributor). |

### Feature work (todo items)
- **Redistribution-rights gate** — was only enforced at *stage*; now a hard gate at **Update** and **Publish** too (`_redistributionAcknowledged()` + disabled Update button when invalid). Verified.
- **App/mint enrichment hardening** — mint now enriches members robustly (F-7/F-12); `enrich_contributors` is reusable. **NOT done:** committing enriched member names into `CONTRIBUTORS.json` at **v2+ release** still needs the App to expose a release/mint enrich endpoint and **be redeployed** (see Deployment below).

---

## REPORTED (your call / needs broader testing)

### HIGH
- **R-1 — `loadIssue` decides "fork exists" by repo NAME only** (~4323). If you already own an unrelated repo with the same name, it clones *that* instead of forking the canonical repo — silent wrong-repo. (`--limit` from F-11 helps the >30 case but not the name collision.) **Fix needs:** match full `{me}/{name}` and verify it's a fork, or always `gh repo fork` (idempotent). Left unfixed because changing the clone path risks the working fork flow — wants a dedicated test pass.

### MEDIUM
- **R-2 — in-org PR load is fork-assuming** (`loadPR` ~4410, `ensureUpstreamExists` ~4307). `loadPR` builds the head repo as `{author}/{name}` and `ensureUpstreamExists` points `upstream` at `origin` when the clone isn't a real fork. For an **in-org / self-authored-baseline** PR (author branched in the org repo, not a personal fork) this clones the wrong repo / sets `upstream==origin`, so reviewer actions can 404 or "find no PR". This is the deeper cause of the review-flow friction the curator-visibility fix only partly addressed. **Fix needs:** resolve the real head repo via `gh pr view --json headRepository,headRefName` and set `upstream` to the canonical PR repo explicitly.
- **R-3 — "Request PR review" can no-op on a just-created PR** (`onRequestReview`). It re-finds the PR via the lagging RepoClerk journal; if the journal hasn't re-drained, `issuePR` returns None and `gh pr ready` never runs (PR stays draft, no error). Same lag can let `commitAndPush` create a duplicate PR. **Fix needs:** resolve the PR number from live GitHub right after creating it, not from the journal.
- **R-4 — `_isArchivalRepo` transient gh error skips contributor curation** (~2649). It swallows all exceptions → False → `onMakeRelease` skips `_curateContributorsForRelease`, so a transient network error yields a release with no contributor record. **Fix:** trust `administratedRepoList` (already archival-only) or distinguish "unknown" from "personal".
- **R-5 — Release-tab open issue/PR counts are always zero** (~2201). `morphoRepos()` dicts carry no `issues`/`pullRequests`, so the repo-list label, announcement counts, and tooltip read 0 even with open work (actual announce/close re-query live, so behavior is fine — only the display misleads). **Fix:** populate counts from the journal's `openIssues`/`openPRs`.
- **R-6 — Auto-assign workflow-scope check is one-shot** (`_refreshAutoAssignAvailability`). Probes once per module load; if you grant the scope (or a transient probe fails) the checkbox stays disabled until reload. **Fix:** re-probe on each Create-tab entry / don't cache a negative.
- **R-7 — Search list-criterion exclusion is order-dependent** (`search` ~5920). For multi-value answers (e.g. `anatomicalAreas`) the exclude check runs inside the value loop, so a repo whose first value is unchecked but a later one is checked is wrongly dropped; empty-list answers bypass the filter. **Fix:** decide after scanning all values (`any(...)`).
- **R-8 — Search tooltip assumes dict captions** (~2456). `screenshotCaptions.items()` raises if captions is a **list** (37/70 live journals store a list); only safe today because those repos have `screenshotCount==0`. **Fix:** normalize to dict / branch on type.
- **R-9 — App enrich freezes a heuristic member name on ORCID failure** (`control_plane.enrich_contributors` ~188). If ORCID is unreachable, `_zenodo_name` heuristically reorders the onboarding name (`"Maria de la Cruz"` → `"Cruz, Maria de la"`) and commits it permanently (later runs skip named rows). **Fix:** only write the reordered form when ORCID resolved; otherwise write the name verbatim or leave blank for retry.
- **R-10 — Three-way schema/enrichment divergence** across `MorphoDepotContributors.py`, `control_plane.py`, `mint_doi.py` (duplicated JSON core; `source` field set inconsistently; App *commits* names while mint only enriches Zenodo metadata). Pick one writer. **Fix:** factor the JSON core into a shared module.

### LOW
- **R-11** — `testingMode` self-test never drives the archival/org/App/credit path (forces personal/testing-org), so regressions there pass green. (Test-infra gap.)
- **R-12** — Q0 → hidden-form sync (`_onRepoTypeChanged`) does `except Exception: pass`; if the option strings ever drift, repoType stays empty and Create is silently dead. Log instead of swallow.
- **R-13** — Dead `selectedDestination*` code + `populateOwnerSelector` gh round-trips on every Create visit, now that destination is fixed by the Q0 fork. Cleanup.
- **R-14** — Baseline-credit dialog attributes lead author to the **CURATOR-file handle**, not the operator, despite "Your information…" wording. Decide curator-vs-operator.
- **R-15** — `from_people_table` reads the Author bit cell via `.ToInt()` without the None guard used for string cells (ragged/just-inserted row risk). `to_contributions_table`/`issues_for`/`render_markdown` bracket-index `c['issue']`/`c['by']` (KeyError on a hand-edited row). ModifiedEvent→syncFromTable could duplicate the curator key if an inserted row gets the curator's handle. Add `.get`/guards.
- **R-16** — `enrich_contributors` failures are swallowed by `publish_repo`'s `try/except: pass` with no logging → a persistently-failing enrichment is invisible (repo goes public with handle-only rows). Add telemetry.

---

## Deployment / wiring still required (cannot be done from here)
1. **Deploy** the intake App (`control_plane.py`) to `join.morphodepot.org` so the publish-time `enrich_contributors` actually runs.
2. **Wire** an App release/mint endpoint that calls `enrich_contributors` (and the mint) so v2+ releases backfill member names into the committed `CONTRIBUTORS.json`. The mint prototype enriches the **DOI metadata** today, but does not commit names back.
3. The DOI mint is still a **prototype** (`experiments/doi/mint_doi.py`), run manually against the Zenodo **sandbox**. Production minting + a `morphodepot.org` Zenodo account are outstanding.

---

## Dry-run results (exercised against the `mdtest-*` repos)

| Check | Result |
|-------|--------|
| **Curator PR visibility** (the owner-vs-curator fix) | **PASS** — `prList(role="reviewer")` for `muratmaga` returns `mdtest-review` PR #4 (amm554's in-org PR for issue-3). Before the fix the curator saw nothing for org-owned repos. |
| **Release-contributions gather** | **PASS** — `_gatherMergedContributions` on `mdtest-release` returns `[(issue 1, amm554), (issue 2, muratmaga)]`, curator separated from the editable grid. |
| **`whoami` robustness** | **PASS** — regex parse returns `muratmaga` (was `split()[7]`). |
| **`prList` null-author guard** | **PASS** — no crash; lists build cleanly (both PRs merged → empty, correct). |
| **Redistribution acknowledgement helper** | **PASS** — `False` when unticked, `True` when ticked; Update/Publish hard-gated. |
| **Release announcement nudge** | **PASS** — soft non-blocking dialog shown when nothing is open (solo dataset), `Proceed anyway` default. |
| **Self-approve skip / already-merged guard** | **PASS** (verified earlier on test-bat-repo): self-authored PR skips the GitHub approve and merges; re-approving a merged PR gives a clean message, not a `TypeError`. |
| **Auto-assign workflow present** | **PASS** — `.github/workflows/auto-assign.yml` is on both `mdtest-*` repos; (live issue-assignment was confirmed earlier on test-bat-repo). |

### What the dry run could NOT cover (manual, in-extension)
- **Create → stage → Make-Public** flow (Sections 1–5 of the test plan): the staging context lives in
  Slicer's memory, so a gh-created repo cannot be "published" through the extension. Create a **fresh**
  repo via the extension to test these (the redistribution gate, baseline-credit dialog, Q0 fork,
  member-only gating, auto-assign default).
- **App member enrichment** at the publish flip / at release: needs the App deployed (see Deployment).
- **Live DOI mint**: sandbox-only and manual; the prototype fixes (F-4/F-7/F-12) make it correct but
  it was not run as part of this dry run.

## Repo state handed back (ready for you to walk the test plan)
- **`MorphoDepot/mdtest-release`** — archival, public, baseline present, **2 merged `issue-N` PRs**
  (issue-1 by amm554, issue-2 by muratmaga), `CONTRIBUTORS.json` with empty `contributions` (the
  release gather fills it). No release yet → load it in the **Release tab** to exercise the announcement
  nudge, the (now populated) contributions table, the baseline content-signature, and a v1 release/DOI.
- **`MorphoDepot/mdtest-review`** — archival, public, baseline present, **3 open issues**: #1 (amm554),
  #2 (SlicerMorph, outsider), #3 (amm554) which already has a **ready open PR #4** for you to review and
  **Approve** (tests curator visibility + approve-and-merge). Use #1/#2 to drive the segmenter flow
  yourself (load issue → segment → Commit and Push → Request PR review).
- Both repos are `mdtest-*` prefixed and **safe to delete**. `test-bat-repo` was left as-is (it carries
  your earlier interactive testing).
