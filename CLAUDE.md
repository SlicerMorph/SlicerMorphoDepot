# SlicerMorphoDepot: instructions for Claude sessions

## Before you change anything

Read [WORKFLOW_RULES.md](WORKFLOW_RULES.md). It lists what the extension must never break (Part A)
and how changes are made and checked (Part B). These rules override convenience, speed and your
own memory of how GitHub behaves.

The automated review on every pull request checks the same rules independently and reads them from
`main`. Do not rely on it to catch what you skipped.

## While implementing

1. **Name the rules first.** Before writing code, list which rules in Part A your change touches.
   For each, write down how it stays true. If you cannot say, stop and ask.
2. **Verify outside behaviour live (P1).** Before relying on what a GitHub endpoint, search query,
   permission, `gh` command or Slicer API does, read its official documentation and make a live
   call as the account the rule is about. For R1 that is an account with public read only on the
   test repository, not an owner, member or collaborator. Never write such a claim into a code
   comment or docstring until you have done this.
3. **Mocked tests are not evidence about GitHub.** Keep unit tests for the code's own logic, but
   do not present them as showing how GitHub behaves.
4. **Check R1–R5 live** whenever you touch the files listed under P2, before opening the pull
   request. If you cannot run the live check (for example Slicer or the test accounts are not
   available), say so plainly in the pull request. Do not describe the change as verified.

## The pull request

Fill in every section of the pull request template, including the live-check evidence for each
rule touched. Do not edit `WORKFLOW_RULES.md`, `CLAUDE.md` or `.github/workflows/claude-code-review.yml`
as a side effect of other work; changes to them are their own pull request, approved by the
maintainer.

## Repository facts

- Development happens on `main`; `5.10` is the stable branch.
- `MorphoDepot/CMakeLists.txt` must list every `MorphoDepotLib/*.py` (R8).
- Unit tests that run without Slicer: every `MorphoDepot/Testing/Python/test_*.py`, e.g.
  `for t in MorphoDepot/Testing/Python/test_*.py; do python3 "$t" || break; done`.
  Passing them is not a live check (P1).
