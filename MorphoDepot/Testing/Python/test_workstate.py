#!/usr/bin/env python3
"""Unit tests for MorphoDepotLib/logic_workstate.py — the live per-user work-state queries.

Run: python3 MorphoDepot/Testing/Python/test_workstate.py   (no Slicer, no deps, no network)

logic_workstate.py imports only json/logging/re, so it loads outside Slicer and its filtering and
shaping can be tested against recorded GitHub responses.  The recorded shapes below are trimmed
from real responses taken 2026-08-07 against the muratmaga account.

The bugs these guard against are the ones the 2026-08-07 classroom outage was made of:

  * a repo published minutes ago must not be filtered out (so the MorphoDepot test is the repo's
    live topic list, never a cached set of known repos)
  * the org and personal tiers must come back from the same query with no branch between them,
    because a tier-conditional path is what let personal repos rot while org repos worked
  * a failed query must not look like an empty work list
"""
import importlib.util
import json
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2] / "MorphoDepotLib" / "logic_workstate.py"
_spec = importlib.util.spec_from_file_location("logic_workstate", _LIB)
workstate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workstate)


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    assert cond, name


def _captureWarnings(fn):
    """Run fn() and return the warning messages it logged, so 'it warns' is assertable."""
    captured = []
    original = workstate.logging.warning
    workstate.logging.warning = lambda message, *a, **k: captured.append(str(message))
    try:
        fn()
    finally:
        workstate.logging.warning = original
    return captured


class FakeLogic(workstate.WorkStateMixin):
    """A WorkStateMixin with the two GitHub call sites stubbed.

    `graphqlPages` is consumed in order, one per _graphql() call, so pagination and batching are
    observable.  Every query string sent is recorded in `queries` so the tests can assert on what
    was actually asked of GitHub, not only on what came back.
    """

    def __init__(self, restResponse=None, graphqlPages=None, curated=None, curatedError=None):
        self.restResponse = restResponse if restResponse is not None else []
        self.graphqlPages = list(graphqlPages or [])
        self.curated = curated or []
        self.curatedError = curatedError
        self.queries = []
        self.restCommands = []

    def ghJSON(self, command):
        self.restCommands.append(command)
        return self.restResponse

    def gh(self, command):
        query = next(part[len("query="):] for part in command if part.startswith("query="))
        self.queries.append(query)
        if not self.graphqlPages:
            raise AssertionError("more GraphQL calls than recorded pages")
        return json.dumps(self.graphqlPages.pop(0))

    def administratedRepoList(self):
        if self.curatedError:
            raise self.curatedError
        return self.curated


# --- recorded shapes ---------------------------------------------------------------------

def restIssue(number, title, fullName, topics, isPR=False):
    entry = {"number": number, "title": title,
             "html_url": f"https://github.com/{fullName}/issues/{number}",
             "repository": {"full_name": fullName, "topics": topics}}
    if isPR:
        entry["pull_request"] = {"url": "..."}
    return entry


def prNode(number, title, author="student", draft=False, closing=(), repo=None):
    node = {"number": number, "title": title, "isDraft": draft,
            "url": f"https://github.com/x/pull/{number}",
            "author": {"login": author} if author else None,
            "closingIssuesReferences": {
                "nodes": [{"number": n, "title": t,
                           "repository": {"owner": {"login": o}}} for n, t, o in closing]}}
    if repo is not None:
        node["repository"] = repoNode(*repo)
    return node


def repoNode(nameWithOwner, topics):
    return {"nameWithOwner": nameWithOwner,
            "repositoryTopics": {"nodes": [{"topic": {"name": t}} for t in topics]}}


MD = ["md-rana-clamitans", "morphodepot"]


# --- assigned issues (Annotate) ----------------------------------------------------------

def test_assigned_issues_span_both_tiers_from_one_query():
    """The property that makes the outage structurally impossible: no tier branch exists.

    `GET /issues?filter=assigned` returns personal and MorphoDepot-org issues in the same
    response.  Nothing downstream inspects the owner, so there is no path on which one tier can
    be fresh and the other stale.
    """
    logic = FakeLogic(restResponse=[
        restIssue(2, "Segment the premaxilla", "muratmaga/rana-clamitans-full-body", MD),
        restIssue(3, "Tongue segmentation", "MorphoDepot/mus-musculus-E15",
                  ["md-mus-musculus", "morphodepot"]),
    ])
    issues = logic.issueList()
    owners = {i["repository"]["nameWithOwner"].split("/")[0] for i in issues}
    check("personal and org issues arrive together", owners == {"muratmaga", "MorphoDepot"})
    check("one REST call for both tiers", len(logic.restCommands) == 1)
    check("the call is paginated", "--paginate" in logic.restCommands[0])


def test_non_morphodepot_repos_are_dropped():
    # The endpoint is account-wide: it returns every issue assigned to the user anywhere on
    # GitHub.  Recorded live -- MorphoCloudAnalytics issues really do come back in this response.
    logic = FakeLogic(restResponse=[
        restIssue(6, "Monthly SU snapshot", "MorphoCloud/MorphoCloudAnalytics", []),
        restIssue(2, "Segment the premaxilla", "muratmaga/rana-clamitans-full-body", MD),
    ])
    issues = logic.issueList()
    check("only morphodepot-topic repos survive",
          [i["repository"]["nameWithOwner"] for i in issues] == ["muratmaga/rana-clamitans-full-body"])


def test_the_topic_test_is_live_not_a_known_repo_set():
    """A repo published two minutes ago carries the topic and has no journal entry yet.

    Filtering against the journal's repo list instead would reproduce the original bug at one
    remove: the issue exists, the assignment exists, and the tab is still empty because the index
    has not caught up.  The topic comes back inline on the same response, so there is no reason
    to consult anything else.
    """
    logic = FakeLogic(restResponse=[
        restIssue(1, "Segment the skull", "instructor/published-90-seconds-ago", ["morphodepot"]),
    ])
    check("a repo with no journal entry still lists", len(logic.issueList()) == 1)


def test_pull_requests_are_not_listed_as_issues():
    # /issues returns PRs too; the Annotate issue list is work to start, not work submitted.
    logic = FakeLogic(restResponse=[
        restIssue(2, "Segment the premaxilla", "muratmaga/rana-clamitans-full-body", MD),
        restIssue(8, "issue-2", "muratmaga/rana-clamitans-full-body", MD, isPR=True),
    ])
    check("the PR entry is dropped", [i["number"] for i in logic.issueList()] == [2])


def test_issue_shape_is_what_the_tab_consumes():
    # updateIssueList() reads title, repository.nameWithOwner and number; loadIssue() reads
    # repository.name.  A shape change here is a silent KeyError in the UI.
    logic = FakeLogic(restResponse=[
        restIssue(2, "Segment the premaxilla", "muratmaga/rana-clamitans-full-body", MD)])
    issue = logic.issueList()[0]
    check("number/title/repository.name/nameWithOwner all present",
          (issue["number"], issue["title"], issue["repository"]["name"],
           issue["repository"]["nameWithOwner"])
          == (2, "Segment the premaxilla", "rana-clamitans-full-body",
              "muratmaga/rana-clamitans-full-body"))


def test_malformed_entries_do_not_break_the_list():
    logic = FakeLogic(restResponse=[
        {"number": 9, "title": "no repository key"},
        restIssue(2, "Segment the premaxilla", "muratmaga/rana-clamitans-full-body", MD),
    ])
    check("a junk entry is skipped rather than raising", len(logic.issueList()) == 1)


def test_an_entry_with_no_number_is_skipped_not_fatal():
    """Every other field has a sensible empty value; a number does not.

    loadIssue() builds the branch name and the fork checkout from it, so an entry without one
    cannot be acted on.  The point is that it takes the rest of the list down with it if it
    raises -- one malformed row must not empty the Annotate tab.
    """
    entry = restIssue(2, "Segment the premaxilla", "muratmaga/rana-clamitans-full-body", MD)
    del entry["number"]
    logic = FakeLogic(restResponse=[
        entry,
        restIssue(3, "Segment the skull", "muratmaga/rana-clamitans-full-body", MD),
    ])
    check("the numberless entry is skipped and the rest survive",
          [i["number"] for i in logic.issueList()] == [3])


# --- authored pull requests (Annotate, segmenter) ----------------------------------------

def _viewerPRPage(nodes, hasNext=False, cursor=None):
    return {"data": {"viewer": {"pullRequests": {
        "pageInfo": {"hasNextPage": hasNext, "endCursor": cursor}, "nodes": nodes}}}}


def test_authored_prs_filter_by_topic():
    logic = FakeLogic(graphqlPages=[_viewerPRPage([
        prNode(8, "issue-2", repo=("muratmaga/rana-clamitans-full-body", MD)),
        prNode(5, "docs typo", repo=("MorphoCloud/docs", [])),
    ])])
    prs = logic.prList(role="segmenter")
    check("only morphodepot PRs are listed", [p["number"] for p in prs] == [8])


def test_authored_prs_paginate():
    logic = FakeLogic(graphqlPages=[
        _viewerPRPage([prNode(1, "issue-1", repo=("o/r", ["morphodepot"]))], hasNext=True, cursor="c1"),
        _viewerPRPage([prNode(2, "issue-2", repo=("o/r", ["morphodepot"]))]),
    ])
    prs = logic.prList(role="segmenter")
    check("both pages are collected", [p["number"] for p in prs] == [1, 2])
    check("the second request carries the cursor", len(logic.queries) == 2)


def test_hitting_the_page_backstop_is_reported():
    """A truncated list must not look like a complete one.

    _MAX_PAGES is a runaway guard, not a supported limit.  If it is ever reached the Review or
    Annotate tab shows fewer items than the user has, and a short list that says nothing about
    being short is the exact failure this module was written to remove.
    """
    page = _viewerPRPage([prNode(1, "issue-1", repo=("o/r", ["morphodepot"]))],
                         hasNext=True, cursor="c")
    logic = FakeLogic(graphqlPages=[page] * workstate._MAX_PAGES)
    warnings = _captureWarnings(lambda: logic.prList(role="segmenter"))
    check("the loop stops at the backstop", len(logic.queries) == workstate._MAX_PAGES)
    check("and says so", any("Stopped after" in w for w in warnings))


def test_a_repo_at_the_per_repo_pr_cap_is_reported():
    nodes = [prNode(n, f"issue-{n}") for n in range(workstate._PRS_PER_REPO)]
    logic = FakeLogic(graphqlPages=[_batchPage({"r0": ("o/busy", nodes)})])
    warnings = _captureWarnings(lambda: logic.openPullRequestsForRepositories(["o/busy"]))
    check("a repo at the un-paginated PR cap warns",
          any("truncated" in w for w in warnings))


def test_pr_shape_is_what_the_tabs_consume():
    # updateAnnotatePRList()/updateReviewPRList() read title, issueTitles, isDraft and
    # repository.nameWithOwner; onPRSelectionChanged reads author.login.
    logic = FakeLogic(graphqlPages=[_viewerPRPage([
        prNode(8, "issue-2", author="student", draft=True,
               closing=((2, "Segment the premaxilla", "muratmaga"),),
               repo=("muratmaga/rana-clamitans-full-body", MD))])])
    pr = logic.prList(role="segmenter")[0]
    check("issueTitles come from the closing-issue references",
          pr["issueTitles"] == ["Segment the premaxilla"])
    check("draft status is carried", pr["isDraft"] is True)
    check("author login is carried", pr["author"]["login"] == "student")
    check("repository is split into name and nameWithOwner",
          pr["repository"] == {"name": "rana-clamitans-full-body",
                               "nameWithOwner": "muratmaga/rana-clamitans-full-body"})


def test_ghost_author_does_not_raise():
    # author is null for a deleted account; the Review tab already tolerates a None login.
    logic = FakeLogic(graphqlPages=[_viewerPRPage([
        prNode(8, "issue-2", author=None, repo=("o/r", ["morphodepot"]))])])
    check("a deleted author becomes login None",
          logic.prList(role="segmenter")[0]["author"]["login"] is None)


def test_a_pr_with_no_linked_issue_still_lists():
    # Under the old reviewer path a PR whose closing-issue link was missing had no "party" and
    # vanished from the list.  Nothing here depends on the link.
    logic = FakeLogic(graphqlPages=[_viewerPRPage([
        prNode(8, "issue-2", closing=(), repo=("o/r", ["morphodepot"]))])])
    prs = logic.prList(role="segmenter")
    check("an unlinked PR is listed with empty issueTitles",
          len(prs) == 1 and prs[0]["issueTitles"] == [])


# --- curated repositories (Review) -------------------------------------------------------

def _viewerRepoPage(nodes, hasNext=False, cursor=None):
    return {"data": {"viewer": {"repositories": {
        "pageInfo": {"hasNextPage": hasNext, "endCursor": cursor}, "nodes": nodes}}}}


def test_curated_repos_are_a_union_of_owned_and_journaled():
    """Owned repos come live from GitHub; in-org repos come from the journaled CURATOR file.

    Both queries always run and merge -- nothing classifies a repo first and then picks a source.
    The org half has to come from the journal because the org owns those repos, so an
    owner-keyed query cannot reach them, and CURATOR is a committed file, hence index data.
    """
    logic = FakeLogic(
        graphqlPages=[_viewerRepoPage([repoNode("muratmaga/rana-clamitans-full-body", MD),
                                       repoNode("muratmaga/SlicerMorph", [])])],
        curated=[{"nameWithOwner": "MorphoDepot/mus-musculus-E15"}])
    check("owned morphodepot repos and curated org repos merge",
          logic.curatedRepositories() == ["MorphoDepot/mus-musculus-E15",
                                          "muratmaga/rana-clamitans-full-body"])


def test_forks_are_not_requested():
    # A segmenter's fork is not a repo they review: its PRs target the upstream, where the
    # upstream's curator sees them.  Asserted on the query, since the filter is server-side.
    logic = FakeLogic(graphqlPages=[_viewerRepoPage([])])
    logic.curatedRepositories()
    check("the owner query excludes forks", "isFork: false" in logic.queries[0])


def test_a_repo_owned_and_curated_appears_once():
    logic = FakeLogic(graphqlPages=[_viewerRepoPage([repoNode("muratmaga/r", ["morphodepot"])])],
                      curated=[{"nameWithOwner": "muratmaga/r"}])
    check("the union deduplicates", logic.curatedRepositories() == ["muratmaga/r"])


def test_unreachable_journal_still_lists_owned_repos():
    # RepoClerk being down must not empty the Review tab for a personal-tier curator.  In-org PRs
    # are missing in that state, which is why it logs a warning rather than passing silently.
    logic = FakeLogic(graphqlPages=[_viewerRepoPage([repoNode("muratmaga/r", ["morphodepot"])])],
                      curatedError=RuntimeError("RepoClerk clone failed"))
    check("owned repos survive a journal failure", logic.curatedRepositories() == ["muratmaga/r"])


# --- batched pull requests (Review) ------------------------------------------------------

def _batchPage(mapping):
    """mapping: alias -> (nameWithOwner, [prNode, ...]); None value means the repo is gone."""
    data = {}
    for alias, value in mapping.items():
        data[alias] = None if value is None else {
            "nameWithOwner": value[0], "pullRequests": {"nodes": value[1]}}
    return {"data": data}


def test_batched_prs_map_back_to_their_repos():
    logic = FakeLogic(graphqlPages=[_batchPage({
        "r0": ("muratmaga/rana-clamitans-full-body", [prNode(8, "issue-2")]),
        "r1": ("MorphoDepot/mus-musculus-E15", [prNode(4, "issue-3")]),
    })])
    prs = logic.openPullRequestsForRepositories(
        ["muratmaga/rana-clamitans-full-body", "MorphoDepot/mus-musculus-E15"])
    check("each PR keeps its own repository",
          [(p["number"], p["repository"]["nameWithOwner"]) for p in prs]
          == [(8, "muratmaga/rana-clamitans-full-body"), (4, "MorphoDepot/mus-musculus-E15")])
    check("one request covers both repos", len(logic.queries) == 1)


def test_a_vanished_repo_is_skipped():
    # A repo deleted, renamed, or made private between the two queries comes back as a null alias.
    logic = FakeLogic(graphqlPages=[_batchPage({"r0": None,
                                                "r1": ("o/live", [prNode(1, "issue-1")])})])
    prs = logic.openPullRequestsForRepositories(["o/gone", "o/live"])
    check("the null alias does not shift the others",
          [p["repository"]["nameWithOwner"] for p in prs] == ["o/live"])


def test_batching_splits_at_the_configured_size():
    names = [f"owner/repo{n}" for n in range(workstate._PR_BATCH + 1)]
    logic = FakeLogic(graphqlPages=[
        _batchPage({f"r{i}": (names[i], []) for i in range(workstate._PR_BATCH)}),
        _batchPage({"r0": (names[-1], [prNode(1, "issue-1")])}),
    ])
    prs = logic.openPullRequestsForRepositories(names)
    check("51 repos take two requests", len(logic.queries) == 2)
    check("aliases restart at r0 in the second batch",
          [p["repository"]["nameWithOwner"] for p in prs] == [names[-1]])


def test_malformed_names_never_reach_the_query():
    # Names are interpolated into GraphQL string literals, so anything that is not a plain
    # GitHub name is dropped rather than escaped.
    logic = FakeLogic(graphqlPages=[_batchPage({"r0": ("o/good", [])})])
    logic.openPullRequestsForRepositories(['o/bad") { x } #', "no-slash", "o/good"])
    check("only the well-formed name is queried",
          logic.queries[0].count("repository(owner:") == 1)
    check('the injection attempt is absent', '") { x }' not in logic.queries[0])


def test_split_name_with_owner():
    check("a normal name splits", workstate.splitNameWithOwner("o/r") == ("o", "r"))
    check("dots and dashes are fine",
          workstate.splitNameWithOwner("Morpho-Depot/mus.musculus_E15")
          == ("Morpho-Depot", "mus.musculus_E15"))
    check("a missing slash is rejected", workstate.splitNameWithOwner("noslash") is None)
    check("an extra slash is rejected", workstate.splitNameWithOwner("a/b/c") is None)
    check("a quote is rejected", workstate.splitNameWithOwner('o/r") {') is None)
    check("empty is rejected", workstate.splitNameWithOwner("") is None)
    check("None is rejected", workstate.splitNameWithOwner(None) is None)


# --- failure is loud ---------------------------------------------------------------------

def test_graphql_errors_raise_rather_than_returning_empty():
    """A GraphQL error arrives with HTTP 200, so gh exits 0 and nothing above would notice.

    An empty list that means "GitHub said no" is indistinguishable from one that means "you have
    no work assigned" -- the ambiguity behind SlicerMorph/SlicerMorphoDepot#212 and much of the
    2026-08-07 investigation.  The tabs wrap these calls in tryWithErrorDisplay, so raising is
    what puts the reason on screen.
    """
    logic = FakeLogic(graphqlPages=[{"errors": [{"message": "API rate limit exceeded"}]}])
    try:
        logic.prList(role="segmenter")
    except RuntimeError as e:
        check("the error message reaches the caller", "rate limit" in str(e))
    else:
        check("a GraphQL error raises", False)


def test_unknown_role_raises():
    check("an unknown role is a programming error, not an empty list",
          _raises(lambda: FakeLogic().prList(role="bystander"), ValueError))


def _raises(fn, exceptionType):
    try:
        fn()
    except exceptionType:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print("\nall work-state tests passed")
