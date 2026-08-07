"""MorphoDepotLogic WorkStateMixin — live per-user issues and pull requests.

**Work state is read from GitHub, not from the RepoClerk journal.**

RepoClerk exists because *discovery* is a fleet-scale question: "which repos are there, and what
species/modality/spacing do they have" has to be computed once, centrally, and shared, or every
client re-crawls the whole fleet.  Work state is not that question.  "What am I assigned?" and
"what am I reviewing?" are bounded by the repos *one person* works on, which does not grow when
the fleet grows.  Measured across the logins appearing in journals: median 1 repo, mean 2.0.

Answering a per-user question from a fleet-scale cache is what caused the 2026-08-07 classroom
outage: five issues were created and assigned, no journal refresh fired, and the Annotate tab
showed an empty list that was indistinguishable from "you have no work".  The queries below
cannot go stale, because there is nothing between them and GitHub.

Measured cost, 2026-08-07 (limits: REST 5,000/hour, GraphQL 5,000 points/hour):

    assigned issues          1 REST call, both tiers, topics inline
    my open PRs              1 GraphQL request, 2 points
    repos I curate           1 GraphQL request per 100 owned repos, 1 point each
    PRs in those repos       1 GraphQL request per 50 repos, ~2 points

None of these scale with fleet size.  The rejected alternative -- one query listing every repo's
issues and PRs and filtering client-side, which is what `ghTopicData()` did -- costs 202 points
against the 30-per-minute search endpoint at 80 repos, and grows linearly with the fleet.

See MorphoDepot/RepoClerk `design/index-vs-work-state.md` for the full argument, and
`design/near-realtime-ingestion.md` for the incident.

What still comes from the journal, and why: the *set* of repos a curator is responsible for is
not answerable from GitHub alone.  An org repo is owned by the MorphoDepot org, not by its
curator, so no owner-keyed query finds it; the CURATOR file is a committed file, hence index
data, hence journaled.  `curatedRepositories()` is a union of a live owner query and that
journaled set -- see its docstring.
"""
import json
import logging
import re


# Repos carry this topic once published; it is what separates a MorphoDepot dataset from the
# rest of a user's GitHub account in the per-user queries below.
MORPHODEPOT_TOPIC = "morphodepot"

# GraphQL aliases are built from these, so anything that is not a plain GitHub name is dropped
# rather than interpolated into a query string.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# One request per this many repos in the batched pull-request query.  50 keeps the request well
# inside GitHub's node limit while keeping the request count at ceil(curated / 50).
_PR_BATCH = 50

# Open PRs fetched per repo, and repos/PRs fetched per page.  A MorphoDepot repo with more than
# 50 simultaneously open PRs does not occur -- one per issue under review -- but the cap is
# explicit so the query cost is bounded rather than open-ended.
_PRS_PER_REPO = 50
_PAGE = 100
_MAX_PAGES = 20  # backstop so a pathological account cannot loop forever


def splitNameWithOwner(nameWithOwner):
    """("owner", "name") for a well-formed `owner/name`, or None.

    The batched pull-request query interpolates these straight into GraphQL string literals, so
    anything that is not a plain GitHub name is rejected here rather than escaped there.
    """
    parts = (nameWithOwner or "").split("/")
    if len(parts) != 2:
        return None
    if not (_SAFE_NAME.match(parts[0]) and _SAFE_NAME.match(parts[1])):
        return None
    return parts[0], parts[1]


class WorkStateMixin:

    # --- queries -------------------------------------------------------------------------

    def _graphql(self, query, **variables):
        """Run a GraphQL query through `gh` and return its `data` object.

        Raises (via gh()) if the request fails, which is deliberate: an empty work list and a
        failed query must not look the same to the user.  That ambiguity is the substance of
        SlicerMorph/SlicerMorphoDepot#212 -- when the journal was unreachable the tabs showed
        nothing at all, with no indication that anything had gone wrong.
        """
        command = ["api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            # `-f` (string) rather than `-F` (typed): every variable here is a String!, and gh's
            # typed form would coerce an all-digit page cursor into an Int and fail the query.
            # An omitted variable defaults to null, which is what "first page" means.
            if value is not None:
                command += ["-f", f"{name}={value}"]
        raw = self.gh(command)
        try:
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Could not parse the GitHub GraphQL response: {e}")
        if payload.get("errors"):
            # A GraphQL error arrives with HTTP 200, so gh exits 0 and nothing above notices.
            messages = "; ".join(e.get("message", "?") for e in payload["errors"])
            raise RuntimeError(f"GitHub GraphQL error: {messages}")
        return payload.get("data") or {}

    def _hasMorphoTopic(self, repositoryNode):
        topics = [n["topic"]["name"]
                  for n in ((repositoryNode.get("repositoryTopics") or {}).get("nodes") or [])]
        return MORPHODEPOT_TOPIC in topics

    def assignedIssues(self):
        """Open issues assigned to the active user on MorphoDepot repos, newest GitHub state.

        `GET /issues?filter=assigned` is a real-time REST endpoint -- not backed by the search
        index that lags for freshly created repos -- and it answers for both tiers in one call:
        personal repos and MorphoDepot-org repos come back together, with no tier distinction
        anywhere in this method.

        The response carries `repository.topics` inline, so the MorphoDepot filter costs no extra
        request.  Filtering on the topic (rather than on the journal's repo list) is what keeps a
        repo published minutes ago from being invisible here.

        The endpoint returns pull requests alongside issues; those carry a `pull_request` key and
        are dropped, since the Annotate tab lists work to start, not work already submitted.
        """
        raw = self.ghJSON(["api", "--paginate",
                           f"/issues?filter=assigned&state=open&per_page={_PAGE}"])
        if not isinstance(raw, list):
            return []
        issues = []
        for entry in raw:
            if not isinstance(entry, dict) or "pull_request" in entry:
                continue
            repository = entry.get("repository") or {}
            if MORPHODEPOT_TOPIC not in (repository.get("topics") or []):
                continue
            nameWithOwner = repository.get("full_name") or ""
            if not nameWithOwner:
                continue
            issues.append({
                "number": entry["number"],
                "title": entry.get("title", ""),
                "url": entry.get("html_url", ""),
                "repository": {"name": nameWithOwner.split("/")[-1],
                               "nameWithOwner": nameWithOwner},
            })
        return issues

    def authoredPullRequests(self):
        """Open pull requests the active user opened, on MorphoDepot repos.

        One request for every PR the user has open anywhere on GitHub, filtered here by topic.
        This is the segmenter's view: the PRs they are responsible for moving along.
        """
        query = """
          query($cursor: String) {
            viewer {
              pullRequests(states: OPEN, first: %d, after: $cursor,
                           orderBy: {field: UPDATED_AT, direction: DESC}) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  number title isDraft url
                  author { login }
                  repository {
                    nameWithOwner
                    repositoryTopics(first: 20) { nodes { topic { name } } }
                  }
                  closingIssuesReferences(first: 10) {
                    nodes { number title repository { owner { login } } }
                  }
                }
              }
            }
          }
        """ % _PAGE
        pullRequests = []
        cursor = None
        for _ in range(_MAX_PAGES):
            data = self._graphql(query, cursor=cursor)
            connection = ((data.get("viewer") or {}).get("pullRequests") or {})
            for node in (connection.get("nodes") or []):
                repository = node.get("repository") or {}
                if not self._hasMorphoTopic(repository):
                    continue
                pullRequests.append(self._prRecord(node, repository.get("nameWithOwner", "")))
            pageInfo = connection.get("pageInfo") or {}
            if not pageInfo.get("hasNextPage"):
                break
            cursor = pageInfo.get("endCursor")
        return pullRequests

    def curatedRepositories(self):
        """`nameWithOwner` for every MorphoDepot repo the active user is the reviewer for.

        A union of two sources, not a tier branch -- both queries always run and their results
        merge:

          * repos the user **owns** that carry the topic, from a live owner-keyed query.  These
            are the personal-tier datasets; GitHub can enumerate them directly because the user
            owns them.
          * repos in the MorphoDepot org whose committed **CURATOR** file names the user, from
            the journal.  An owner-keyed query cannot find these -- the org owns them -- and
            CURATOR is a file in the repo, so it only changes on a push and belongs in the index
            on exactly the same grounds as species or modality.

        Forks are excluded: a segmenter's fork of a dataset is not a repo they review.  Its pull
        requests target the upstream, where the upstream's curator already sees them.
        """
        query = """
          query($cursor: String) {
            viewer {
              repositories(ownerAffiliations: OWNER, isFork: false, first: %d, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  nameWithOwner
                  repositoryTopics(first: 20) { nodes { topic { name } } }
                }
              }
            }
          }
        """ % _PAGE
        owned = []
        cursor = None
        for _ in range(_MAX_PAGES):
            data = self._graphql(query, cursor=cursor)
            connection = ((data.get("viewer") or {}).get("repositories") or {})
            for node in (connection.get("nodes") or []):
                if self._hasMorphoTopic(node) and node.get("nameWithOwner"):
                    owned.append(node["nameWithOwner"])
            pageInfo = connection.get("pageInfo") or {}
            if not pageInfo.get("hasNextPage"):
                break
            cursor = pageInfo.get("endCursor")

        curated = set(owned)
        try:
            for repo in self.administratedRepoList():
                if repo.get("nameWithOwner"):
                    curated.add(repo["nameWithOwner"])
        except Exception as e:
            # The journal half is a supplement, not the whole answer.  If RepoClerk is
            # unreachable the user's own repos still list, so degrade rather than fail --
            # but say so, because in-org PRs will be missing from the list.
            logging.warning(f"Could not read curated org repos from RepoClerk: {e}")
        return sorted(curated)

    def openPullRequestsForRepositories(self, nameWithOwners):
        """Open pull requests across the named repos, batched into as few requests as possible.

        Aliased `repository(...)` fields rather than a search: search is the 30-per-minute
        endpoint and lags behind reality for anything created in the last few seconds, which is
        precisely the moment the Review tab is refreshed after a student clicks Request review.
        """
        pullRequests = []
        targets = []
        for nameWithOwner in nameWithOwners:
            parts = splitNameWithOwner(nameWithOwner)
            if parts is None:
                logging.warning(f"Skipping malformed repository name {nameWithOwner!r}")
                continue
            targets.append(parts)
        for start in range(0, len(targets), _PR_BATCH):
            batch = targets[start:start + _PR_BATCH]
            fields = "\n".join(
                f'    r{index}: repository(owner: "{owner}", name: "{name}") {{ ...prs }}'
                for index, (owner, name) in enumerate(batch))
            query = """
              query {
              %s
              }
              fragment prs on Repository {
                nameWithOwner
                pullRequests(states: OPEN, first: %d, orderBy: {field: UPDATED_AT, direction: DESC}) {
                  nodes {
                    number title isDraft url
                    author { login }
                    closingIssuesReferences(first: 10) {
                      nodes { number title repository { owner { login } } }
                    }
                  }
                }
              }
            """ % (fields, _PRS_PER_REPO)
            data = self._graphql(query)
            for index, (owner, name) in enumerate(batch):
                repository = data.get(f"r{index}")
                if not repository:
                    continue  # deleted, renamed, or no longer visible to this user
                nameWithOwner = repository.get("nameWithOwner") or f"{owner}/{name}"
                for node in ((repository.get("pullRequests") or {}).get("nodes") or []):
                    pullRequests.append(self._prRecord(node, nameWithOwner))
        return pullRequests

    # --- shaping -------------------------------------------------------------------------

    def _prRecord(self, node, nameWithOwner):
        """The dict shape the Annotate and Review tabs consume, from a GraphQL PR node."""
        closing = ((node.get("closingIssuesReferences") or {}).get("nodes") or [])
        return {
            "number": node["number"],
            "title": node.get("title", ""),
            "url": node.get("url", ""),
            "isDraft": bool(node.get("isDraft")),
            # None for a deleted or ghost account, which the Review tab already tolerates.
            "author": {"login": (node.get("author") or {}).get("login")},
            "issueTitles": [issue.get("title", "") for issue in closing],
            "repository": {"name": nameWithOwner.split("/")[-1], "nameWithOwner": nameWithOwner},
        }

    # --- the two entry points the tabs call ----------------------------------------------

    def issueList(self):
        """Issues assigned to the active user — the Annotate tab's list."""
        return self.assignedIssues()

    def prList(self, role="segmenter"):
        """Open pull requests relevant to the active user in `role`.

        `segmenter` — the PRs they opened; `reviewer` — the PRs open on the repos they curate.
        The reviewer half no longer infers responsibility from a PR's closing-issue owner: it
        starts from the repo set (see `curatedRepositories`) and lists what is open there, which
        is both cheaper and correct for a PR whose issue link is missing or malformed.
        """
        if role == "segmenter":
            return self.authoredPullRequests()
        if role == "reviewer":
            return self.openPullRequestsForRepositories(self.curatedRepositories())
        raise ValueError(f"Unknown role {role}")
