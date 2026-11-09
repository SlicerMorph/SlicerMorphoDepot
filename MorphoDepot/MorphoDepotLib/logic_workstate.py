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

    assigned issues          1 REST call + 1 GraphQL search, both tiers, topics inline
                             (the search was added 2026-11-09: the REST call alone misses
                             repos the segmenter can only read -- see assignedIssues)
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

    def _paginate(self, query, connectionKey, what):
        """Every node of `viewer.<connectionKey>`, following pages until GitHub says stop.

        `_MAX_PAGES` is a backstop against an unbounded loop, not a supported limit, so hitting
        it is reported rather than passed over: a truncated answer here is a Review tab that
        quietly lists fewer pull requests than the user actually has, with nothing on screen to
        say so.  A silently short list is the failure mode this whole module exists to remove.
        """
        nodes = []
        cursor = None
        for _ in range(_MAX_PAGES):
            data = self._graphql(query, cursor=cursor)
            connection = ((data.get("viewer") or {}).get(connectionKey) or {})
            nodes.extend(connection.get("nodes") or [])
            pageInfo = connection.get("pageInfo") or {}
            if not pageInfo.get("hasNextPage"):
                return nodes
            cursor = pageInfo.get("endCursor")
        logging.warning(
            f"Stopped after {_MAX_PAGES} pages of {what} ({len(nodes)} fetched); "
            "the list shown is incomplete.")
        return nodes

    def _hasMorphoTopic(self, repositoryNode):
        topics = [n["topic"]["name"]
                  for n in ((repositoryNode.get("repositoryTopics") or {}).get("nodes") or [])]
        return MORPHODEPOT_TOPIC in topics

    def assignedIssues(self):
        """Open issues assigned to the active user on MorphoDepot repos, newest GitHub state.

        The MorphoDepot workflow requires NO access to the dataset repo: a segmenter is assigned
        an issue on a public repo they have only read access to, and the extension forks it and
        brings them the data.  Every issue assigned to the user must therefore be found whether
        or not they are a member, collaborator, or on a team with access to the repo.

        No single GitHub query does both of the things this list needs, so two always run and
        their results merge (no tier branch anywhere):

          * `GET /issues?filter=assigned` -- real time, so a repo published minutes ago lists
            immediately.  But it only covers repos the user has an access relationship to
            (owner, collaborator, team, org base permission).  It silently omits an issue on a
            public repo the user can merely read -- the normal segmenter case.  Verified live
            2026-11-09: amm554, assigned MorphoDepot/DeepTrenchA#5 with public read only, got 0
            results here.
          * GraphQL `search` for `is:issue is:open assignee:@me` -- covers every repo the user can
            see, including read-only public ones, but is backed by the search index, which can
            lag for a freshly created repo.

        Both carry the repo's topics inline, so the MorphoDepot filter costs no extra request.
        Filtering on the topic (rather than on the journal's repo list) is what keeps a repo
        published minutes ago from being invisible here.

        A failure of either query raises: a list missing one source's issues would look exactly
        like "you have no more work", which is the failure this module exists to prevent.
        """
        issues = []
        seen = set()
        for issue in self._assignedIssuesRest() + self._assignedIssuesSearch():
            key = (issue["repository"]["nameWithOwner"].lower(), issue["number"])
            if key in seen:
                continue
            seen.add(key)
            issues.append(issue)
        return issues

    def _issueRecord(self, number, title, url, nameWithOwner):
        """The dict shape the Annotate tab consumes."""
        return {
            "number": number,
            "title": title,
            "url": url,
            "repository": {"name": nameWithOwner.split("/")[-1],
                           "nameWithOwner": nameWithOwner},
        }

    def _assignedIssuesSearch(self):
        """Assigned open issues from the search index, which includes read-only public repos.

        `type: ISSUE_ADVANCED` is GitHub's current issue search (the legacy `ISSUE` type is being
        retired); `is:issue` keeps pull requests out.
        """
        query = """
          query($q: String!, $cursor: String) {
            search(type: ISSUE_ADVANCED, query: $q, first: %d, after: $cursor) {
              pageInfo { hasNextPage endCursor }
              nodes {
                ... on Issue {
                  number title url
                  repository {
                    nameWithOwner
                    repositoryTopics(first: 20) { nodes { topic { name } } }
                  }
                }
              }
            }
          }
        """ % _PAGE
        nodes = []
        cursor = None
        for _ in range(_MAX_PAGES):
            data = self._graphql(query, q="is:issue is:open assignee:@me archived:false",
                                 cursor=cursor)
            connection = data.get("search") or {}
            nodes.extend(connection.get("nodes") or [])
            pageInfo = connection.get("pageInfo") or {}
            if not pageInfo.get("hasNextPage"):
                break
            cursor = pageInfo.get("endCursor")
        else:
            logging.warning(
                f"Stopped after {_MAX_PAGES} pages of your assigned issues ({len(nodes)} "
                "fetched); the list shown is incomplete.")
        issues = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            repository = node.get("repository") or {}
            if not self._hasMorphoTopic(repository):
                continue
            nameWithOwner = repository.get("nameWithOwner") or ""
            if not nameWithOwner or node.get("number") is None:
                continue
            issues.append(self._issueRecord(node["number"], node.get("title", ""),
                                            node.get("url", ""), nameWithOwner))
        return issues

    def _assignedIssuesRest(self):
        """Assigned open issues from `GET /issues`: real time, but only repos the user has access to.

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
            if not nameWithOwner or entry.get("number") is None:
                # Every other field has a sensible empty value; a number does not -- loadIssue()
                # builds the branch name and the fork checkout from it.  Skip rather than emit an
                # entry that would fail later, and rather than KeyError out of the whole list.
                continue
            issues.append(self._issueRecord(entry["number"], entry.get("title", ""),
                                            entry.get("html_url", ""), nameWithOwner))
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
        for node in self._paginate(query, "pullRequests", "your open pull requests"):
            repository = node.get("repository") or {}
            if not self._hasMorphoTopic(repository):
                continue
            pullRequests.append(self._prRecord(node, repository.get("nameWithOwner", "")))
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
        curated = set()
        for node in self._paginate(query, "repositories", "the repositories you own"):
            if self._hasMorphoTopic(node) and node.get("nameWithOwner"):
                curated.add(node["nameWithOwner"])
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
                nodes = ((repository.get("pullRequests") or {}).get("nodes") or [])
                if len(nodes) >= _PRS_PER_REPO:
                    # Not paginated: _PRS_PER_REPO is set well above what a MorphoDepot repo
                    # sees (one open PR per issue under review).  If it is ever reached the
                    # list is short by an unknown amount, which must not pass unremarked.
                    logging.warning(
                        f"{nameWithOwner} has at least {_PRS_PER_REPO} open pull requests; "
                        "the Review list is truncated.")
                for node in nodes:
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
