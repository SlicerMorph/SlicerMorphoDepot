# MorphoDepot workflow rules

These are the things the extension must never break. Every change is checked against them twice:
by the person (or Claude session) writing the change, using `CLAUDE.md`, and separately by the
automated Claude review on every pull request, which reads this file **from `main`**, so a pull
request cannot loosen the rules that judge it.

Changing this file needs the maintainer's (muratmaga) explicit approval in the pull request.

---

## Part A: what must stay true

### R1. A segmenter needs no access to the repository

Anyone with a GitHub account can work on a **public** MorphoDepot repository without being an
organization member, a collaborator, or on any team. Their only access is public read. For that
person:

- an issue assigned to them **must** appear in their Annotate list;
- loading it **must** fork the repository and bring them the data;
- submitting **must** open a pull request back to the original repository.

This is the core of the MorphoDepot workflow, not an edge case.

*Incident:* #216 (2026-08-16) built the Annotate list from `GET /issues?filter=assigned`, which
silently leaves out repositories the user has no access relationship to. Every assignment in
someone else's repository disappeared for about three months. Fixed in #242.

*Check:* live, signed in as an account whose only access to the test repository is public read.

### R2. Organizational and Personal repositories both work

Most people who create repositories are not organization members; they get Personal
repositories. Members get Organizational repositories. A change may not serve one kind and quietly
break the other. Any code that finds, loads, submits or reviews work handles both through the same
path wherever possible.

*Check:* exercise the change on one Organizational and one Personal repository.

### R3. Work published minutes ago must show up

GitHub search and the RepoClerk journal both lag behind GitHub, by minutes to hours. Neither may be
the **only** source for a person's own work (assigned issues, their pull requests, pull requests
waiting for their review).

*Incident:* 2026-08-07 classroom outage: five issues were created and assigned, the journal had
not refreshed, and the Annotate list was empty.

*Check:* live, with an issue or pull request created moments before.

### R4. A failure is an error, never an empty list

If a query fails, the person must see that it failed. An empty list means "you have nothing to
do", so a failure must never be shown as one, and a partial answer must never be shown as complete.

*Incident:* the 2026-08-07 outage and #212.

*Check:* a unit test that makes the query fail and asserts an error is raised or shown.

### R5. Reviewers see the work on the repositories they curate

The reviewer of an Organizational repository is the login in its `CURATOR` file; the reviewer of
a Personal repository is its owner. A reviewer must see every open pull request on the
repositories they curate, whichever kind of repository it is.

*Check:* live, as the curator of an Organizational and of a Personal repository.

### R6. Publishing is one-way; nothing is deleted automatically

A repository goes from private to public, never back. Discard is allowed only while a repository
is still private. No automated job deletes repositories.

### R7. Words people read

The only two words for repository kinds are **Organizational** and **Personal**. The hidden
`repoType` answer strings in `accession_schema.py` must **not** be renamed:
`widget_create.py` routes a repository by `startswith("Archival")`, so a rename silently sends
every repository to the creator's personal account.

### R8. The packaged extension must load

`CMakeLists.txt` must list every `MorphoDepotLib/*.py`. A missing entry ships a module that fails
to import in the Extensions Manager build while working fine from a source checkout.

---

## Part B: how changes are made and checked

### P1. Verify outside behaviour live; never assume it

Any claim about how something outside this code behaves (what a GitHub endpoint returns or leaves
out, what search covers, what a permission allows, what `gh` or Slicer does) must be checked
against the official documentation **and** a live call, made as the account the rule is about
(for R1, an account with public read only). The pull request records the call and its result.

Unit tests that feed the code recorded or invented responses check the code's handling of those
responses. They **do not** show that GitHub behaves that way, and are never accepted as evidence
for it. #216 had a 438-line test suite built on the same wrong belief as its code.

Code comments and docstrings must not state outside behaviour that has not been checked this way.

### P2. Say which rules a change touches, and show the check

A pull request that changes how people find, load, submit, review, create, publish or release
work must list the rules from Part A it touches and give the live check for each (see the pull
request template). "Not touched" needs a one-line reason.

These code paths always touch at least R1–R5:

- `MorphoDepotLib/logic_workstate.py`: Annotate and Review lists
- `MorphoDepotLib/logic_github.py`: issue/PR lookups (`issuePR`, …)
- `MorphoDepotLib/logic_repo.py`: loading an issue, forking, cloning (`loadIssue`, …)
- `MorphoDepotLib/logic_contribute.py`: committing, `requestReview`, `approvePR`
- `MorphoDepotLib/widget_annotate.py`, `widget_review.py`
- anything that changes which GitHub account, token, permission or endpoint is used

Changes to `widget_create.py`, `logic_controlplane.py`, `logic_objectstore.py` and
`logic_release.py` touch at least R2, R6 and R7.

### P3. Check the rules first, not just the bug

Before changing a workflow path, read Part A and ask of each rule: does this change keep it true?
Fixing the problem in front of you is not enough. #216 fixed the 2026-08-07 outage and broke R1.
