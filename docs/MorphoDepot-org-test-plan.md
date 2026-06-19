# MorphoDepot — Org Features Test Plan

Exercises every org-related feature added in the last development cycle (repo-type fork,
auto-assign, redistribution gate, baseline credit, reviewer flow, release flow, DOI). Walk it
top-to-bottom; each test says which **account** and **repo** to use and what **should** happen.

> Generated for hands-on verification. Companion: `MorphoDepot-org-review-findings.md` (static
> code-review findings + issues found while dry-running this plan).

## Accounts (gh, already signed in — `gh auth switch` to change active)

| Role | Login | In MorphoDepot org? |
|------|-------|---------------------|
| Owner / curator | `muratmaga` | Owner |
| Member (no privileges) | `amm554` | Member |
| Outsider | `SlicerMorph` | **Not** a member |

## Test repositories (created by the overnight run, `mdtest-*` prefix under MorphoDepot)

| Repo | Type | Baseline? | Purpose |
|------|------|-----------|---------|
| `MorphoDepot/mdtest-archival-baseline` | Archival | Yes | Publish baseline-credit gate, release, DOI |
| `MorphoDepot/mdtest-archival-nobaseline` | Archival | No | Archival without a baseline segmentation |
| `MorphoDepot/mdtest-review` | Archival | Yes | Reviewer flow (curator sees contributor PRs) |
| `mdtest-shortterm` (personal) | Short-term | n/a | Personal short-term path |

> If a repo was consumed/closed during the dry run it is **recreated clean** before you start —
> see the findings report's "Repo state handed back" section.

---

## 1. Repository-type fork (Q0) — the first, irreversible choice

| # | Account | Steps | Expected |
|---|---------|-------|----------|
|1.1| muratmaga | Open Create tab | **Archival / Short-term** selector is the *first* control, above the form |
|1.2| muratmaga | Pick **Archival** | Form's hidden repoType set to Archival; destination becomes the org (no "where should this live?" dialog) |
|1.3| muratmaga | Pick **Short-term** | Destination becomes the personal account |
|1.4| muratmaga | Stage a repo, then reopen it for editing | The type selector is **hidden** in edit mode (type cannot change after staging) |
|1.5| SlicerMorph (outsider) | Switch active account, pick **Archival**, try to stage | Blocked with a clear "must be an org member" message (archival is org-only) |
|1.6| amm554 (member) | Pick **Archival**, stage | Allowed (members can create archival) |

## 2. Auto-assign workflow — default-ON, opt-out, ALL types

| # | Account | Steps | Expected |
|---|---------|-------|----------|
|2.1| muratmaga | Open Create tab | "Set the GitHub workflow to auto-assign…" checkbox is **checked by default** and **enabled** |
|2.2| muratmaga | Toggle Archival ↔ Short-term | Checkbox stays checked + enabled (independent of type — you can opt out either way) |
|2.3| muratmaga | Create an archival repo with it left on | The published repo has `.github/workflows/auto-assign.yml` |
|2.4| any | Open a new issue on that repo | The issue is auto-assigned to its creator (check Actions tab ran green) |
|2.5| — | (If a gh login lacks the `workflow` scope) | Checkbox is unchecked + disabled with the "run `gh auth refresh -s workflow`" hint |

## 3. Redistribution-rights acknowledgement — hard gate

| # | Account | Steps | Expected |
|---|---------|-------|----------|
|3.1| muratmaga | On Create, leave Section-6 "I have the right to allow redistribution" **unchecked** | "Create (stage privately)" stays **disabled** |
|3.2| muratmaga | Check it | Create enables |
|3.3| muratmaga | Stage, reopen for edit, **uncheck** it | **Update Repository** becomes disabled; clicking it (if reachable) shows "Redistribution acknowledgement required" |
|3.4| muratmaga | With it unchecked, try **Make Public** | Blocked: "…before publishing." |

## 4. Baseline-credit gate at publish (Make Public)

| # | Account | Repo | Steps | Expected |
|---|---------|------|-------|----------|
|4.1| muratmaga | mdtest-archival-baseline | Click **Make Public** | "Baseline contributors" dialog appears (repo ships a baseline) |
|4.2| muratmaga | " | Inspect the dialog | Narrow window, working **Done / Cancel** buttons, read-only "Lead author (you): …" line, Documentation link, no "ready to release" claim |
|4.3| muratmaga | " | Add an offline contributor with a name, tick Author? | They become a co-author |
|4.4| muratmaga | " | Click **Done** | `CONTRIBUTORS.json` is written to the repo via the API; repo goes public |
|4.5| muratmaga | mdtest-archival-nobaseline | Make Public | No baseline dialog (nothing ships); publishes directly |
|4.6| muratmaga | " | Click **Cancel** in 4.1 | Publish is aborted; repo stays private |

## 5. Member enrichment (App-side — requires deployed App)

| # | Notes |
|---|-------|
|5.1| After an org publish, the App's `enrich_contributors` fills blank member names from the owners-only onboarding records + ORCID (`Family, Given`). **Requires the App change to be deployed** — see findings report (NOT deployed by the overnight run). |
|5.2| The extension's `_enrichMemberPerson` is best-effort dialog pre-fill only (owners get full info, members get GitHub profile name). |

## 6. Reviewer flow (curator reviewing contributor PRs)

| # | Account | Steps | Expected |
|---|---------|-------|----------|
|6.1| amm554 or SlicerMorph | Load assigned issue on mdtest-review, segment, **Commit and Push** | A **draft** PR is created (checkpoint) |
|6.2| same | Click **Request PR review** | PR flips to ready (`gh pr ready`); after RepoClerk catches up it shows "ready for review" |
|6.3| muratmaga | Review tab → **Refresh Github** | The contributor's in-org PR **appears** (curator-visibility fix; org-owned repos used to hide it) |
|6.4| muratmaga | Double-click the PR | Loads/clones it |
|6.5| muratmaga | **Approve** | Approves **and** squash-merges in one click |
|6.6| muratmaga | Click **Approve** again on the now-merged PR (stale panel) | Clean message "No open pull request found… already merged or closed… Refresh", **not** a `TypeError` |
|6.7| muratmaga | Approve a PR **you authored** yourself | Skips the GitHub approve (can't approve your own PR) and merges anyway — no "Can not approve your own pull request" error |

## 7. Release flow (archival-only)

| # | Account | Repo | Steps | Expected |
|---|---------|------|-------|----------|
|7.1| muratmaga | mdtest-archival-baseline | Open Release tab | Only **org/archival** repos you curate are listed (short-term personal repos excluded) |
|7.2| muratmaga | " | Make Release with **no announcement** and nothing open | **Soft non-blocking nudge** ("good practice to announce… solo dataset can simply proceed"), Proceed anyway / Announce first |
|7.3| muratmaga | " | Make Release with **open** issues/PRs and no announcement | Firmer warning (Announce / Proceed / Cancel) |
|7.4| muratmaga | " | At the contributor-confirm dialog | **Contributions table is populated** from merged `issue-N` PRs; contributors appear in the grid (this was empty before the gather fix) |
|7.5| muratmaga | " | Try to release with **no baseline change** since last release | Blocked: "no baseline change → no release" (content-signature check) |
|7.6| muratmaga | " | Complete a release | `CONTRIBUTORS.json` committed in the release snapshot; `pre-release-vN` branch pushed; vN tag created |
|7.7| muratmaga | " | Trigger a failed release, then **retry** | The retry does **not** crash on "branch pre-release-vN already exists" (idempotent backup branch) |

## 8. DOI minting (Zenodo **sandbox** only)

| # | Notes |
|---|-------|
|8.1| Mint runs against `CONTRIBUTORS.json`: curator = creator, others = contributors; species/modality/anatomy → Zenodo keywords (not the citation). |
|8.2| First release = concept DOI + version DOI; later releases = new version under the concept DOI. |
|8.3| DOI badge + citation written into README in a delimited block, regenerated per release. |
|8.4| **Refuses to mint** if any author has only a GitHub handle (no real name) — fill the name first. |
|8.5| Org/archival + private = no DOI (must be public). Personal/short-term = never minted. |

## 9. Contributor-credit dialog details (cross-cutting)

| # | Expected |
|---|----------|
|9.1| Curator/lead-author row is **read-only** (a line, not an editable grid row) and is exempt from the name-required gate. |
|9.2| Status never claims "all contributors named / ready" — only flags real problems (a cited author with no name) or facts (who will be credited by handle). |
|9.3| "Add offline contributor" / "Remove selected row" work; the GitHub column is read-only (filled automatically). |
