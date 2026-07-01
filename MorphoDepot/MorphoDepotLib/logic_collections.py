"""MorphoDepotLogic CollectionsMixin — create and list curated "repo of repos" collections.

A *collection* is a MorphoDepot org repository tagged ``md-collection`` whose **description** holds
the title (PR-proof repo metadata — only repo-admins can change it) and whose README lists the
member dataset repositories.  RepoClerk reads the description for the title and parses the README
for members, rendering a gallery + screenshot slide deck.  The repo name itself is an opaque
``collection-<hash>`` — it carries no meaning, so it never needs to be a (lossy, collision-prone)
truncation of a human title.  See SlicerMorph/SlicerMorphoDepot#180 (this tab) and
MorphoDepot/RepoClerk#411 (rendering).

The whole flow runs with the member's OWN ``gh`` (they create private in-org repos and are repo
admin of their own creation) — no App/Administration privilege, consistent with the admin-free
posture.  Governance is deliberately simple: one ``CURATOR`` (the creator) for attribution; anyone
else contributes via a standard fork-and-PR.
"""
import base64
import difflib
import json
import logging
import re
import secrets
import unicodedata

COLLECTION_TOPIC = "md-collection"
DISCOVERY_TOPIC = "morphodepot"

# Filler words ignored when fuzzily comparing collection titles for near-duplicates.
_TITLE_STOPWORDS = {"the", "of", "a", "an", "and", "or", "for", "in", "on", "to", "with",
                    "repo", "repos", "repository", "repositories"}


def _normalize_title_tokens(title):
    """Lowercase, strip punctuation/diacritics/stopwords and crudely de-pluralize a title into a
    set of significant word tokens, so word order and trivial variations don't matter."""
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii").lower()
    tokens = set()
    for w in re.findall(r"[a-z0-9]+", text):
        if w in _TITLE_STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        tokens.add(w)
    return tokens


def _title_similarity(a, b):
    """0..1 similarity between two titles — the max of token-set (Jaccard) and sequence-ratio
    similarity over the normalized tokens, so reordered words and small edits both score high."""
    ta, tb = _normalize_title_tokens(a), _normalize_title_tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    seq = difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jaccard, seq)


class CollectionsMixin:
    # --- Reading existing collections / the dataset corpus from the RepoClerk journals ---

    def _allJournals(self):
        """All RepoClerk journals from the shallow clone (empty list if unavailable)."""
        clonePath = self.refreshRepoClerk()
        if not clonePath:
            return []
        return self.repoClerkJournals(clonePath)

    def collectionRepos(self):
        """Existing collections (journals carrying a ``collection`` block), title-sorted.
        Each entry: {nameWithOwner, title, description, curator, memberRefs}."""
        out = []
        for j in self._allJournals():
            block = j.get("collection")
            if isinstance(block, dict):
                nwo = j.get("nameWithOwner", "")
                out.append({
                    "nameWithOwner": nwo,
                    "title": block.get("title") or nwo.split("/")[-1],
                    "description": block.get("description", ""),
                    "curator": j.get("curator"),
                    "memberRefs": block.get("memberRefs", []),
                })
        out.sort(key=lambda c: (c["title"] or "").lower())
        return out

    def existingCollectionTitles(self):
        """All collection repos in the org, live via gh, as [{nameWithOwner, title}].  Uses the
        repo description (set to the title at creation) so the duplicate check is NOT subject to
        RepoClerk journal lag — a just-created collection is seen immediately."""
        repos = self.ghJSON(["repo", "list", self.morphoDepotOrg, "--topic", COLLECTION_TOPIC,
                            "--limit", "500", "--json", "nameWithOwner,description"]) or []
        out = []
        for r in repos:
            if isinstance(r, dict) and r.get("nameWithOwner"):
                title = (r.get("description") or "").strip() or r["nameWithOwner"].split("/")[-1]
                out.append({"nameWithOwner": r["nameWithOwner"], "title": title})
        return out

    def similarCollections(self, title, threshold=0.6):
        """Existing collections whose title is fuzzily similar to ``title`` (token-set + sequence
        similarity, order-independent, plurals/filler-words folded), most-similar first.  Catches
        e.g. 'Random Collection' vs 'random collections' vs 'random repo collection'."""
        out = []
        for c in self.existingCollectionTitles():
            sim = _title_similarity(title, c["title"])
            if sim >= threshold:
                out.append({**c, "similarity": round(sim, 2)})
        out.sort(key=lambda c: c["similarity"], reverse=True)
        return out

    def datasetRepoCorpus(self):
        """Known dataset repos (journals WITHOUT a collection block), for the member picker.
        Returns a dict {nameWithOwner: {nameWithOwner, species}}."""
        corpus = {}
        for j in self._allJournals():
            if isinstance(j.get("collection"), dict):
                continue
            nwo = j.get("nameWithOwner")
            if not nwo:
                continue
            species = ""
            sp = (j.get("accession") or {}).get("species")
            if isinstance(sp, list) and len(sp) >= 2:
                species = sp[1] or ""
            corpus[nwo] = {"nameWithOwner": nwo, "species": species}
        return corpus

    # --- Repo name / reference normalization ---

    def newCollectionSlug(self):
        """A fresh, unique, opaque repo name for a collection: ``collection-`` + 8 random hex
        chars.  Deliberately meaningless — the title lives authoritatively (and PR-proof) in the
        repo *description* — so the name only needs to be unique and recognizable as a collection,
        never a (lossy, collision-prone) truncation of a human-readable title."""
        while True:
            slug = "collection-" + secrets.token_hex(4)
            if not self.repoExists(f"{self.morphoDepotOrg}/{slug}"):
                return slug

    @staticmethod
    def normalizeRepoRef(ref):
        """Normalize a pasted GitHub URL or ``owner/repo`` string to ``owner/repo`` (''  if not
        parseable)."""
        ref = (ref or "").strip()
        m = re.search(r"github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)", ref)
        if not m:
            m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)", ref)
        if not m:
            return ""
        owner, name = m.group(1), m.group(2)
        if name.endswith(".git"):
            name = name[:-4]
        return f"{owner}/{name.rstrip('.,);:')}"

    def isMorphoDepotRepo(self, nameWithOwner):
        """True only if ``nameWithOwner`` is a MorphoDepot DATASET repo — it exists and carries the
        ``morphodepot`` topic but NOT the ``md-collection`` topic (a collection is not itself a
        valid member).  Checked live via gh (the repo's topics); False on any error or missing
        topic, so a non-MorphoDepot URL or a typo is rejected."""
        try:
            out = self.gh(["api", f"repos/{nameWithOwner}/topics", "--jq", ".names"],
                          quietErrors=True)
        except RuntimeError:
            return False
        try:
            names = json.loads(out) if out else []
        except Exception:
            names = []
        names = names or []
        return DISCOVERY_TOPIC in names and COLLECTION_TOPIC not in names

    # --- Creation ---

    def _renderCollectionReadme(self, title, description, memberNwos):
        """Canonical collection README: title on line 1, optional description, then the member
        repos as GitHub URLs.  Still a plain README, so it can be hand-edited later; RepoClerk's
        tolerant parser harvests the member links regardless."""
        lines = [f"# {title}", ""]
        if description:
            lines += [description, ""]
        lines += ["## Member repositories", ""]
        lines += [f"- https://github.com/{nwo}" for nwo in memberNwos]
        lines += ["",
                  "<!-- Created with the MorphoDepot extension. The collection TITLE is the repo "
                  "description (edit it in repo settings; a pull request cannot change it). Add or "
                  "remove member repositories in the list above; RepoClerk re-renders on update. -->",
                  ""]
        return "\n".join(lines)

    def _putRepoFile(self, nameWithOwner, path, content, message):
        """Create or update a file via the contents API.  If the path already exists (e.g. the
        README auto-created at repo creation), its blob sha is included so the PUT updates rather
        than 422-failing."""
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        args = ["api", "--method", "PUT", f"/repos/{nameWithOwner}/contents/{path}",
                "-f", f"message={message}", "-f", f"content={b64}"]
        try:
            sha = (self.gh(["api", f"/repos/{nameWithOwner}/contents/{path}", "--jq", ".sha"],
                           quietErrors=True) or "").strip()
            if sha:
                args += ["-f", f"sha={sha}"]
        except Exception:
            pass  # file does not exist yet -> plain create
        self.gh(args)

    def createCollection(self, title, description, memberRefs):
        """Create an in-org collection repo: public repo + canonical README + CURATOR + the
        ``morphodepot``/``md-collection`` topics, then notify RepoClerk.  ``memberRefs`` are URLs
        or owner/repo strings; at least two must resolve.  Returns the nameWithOwner.

        Created **public** so it is immediately visible to everyone (and to RepoClerk).  The org
        permits members to create public repositories, so this works for ANY member, not just
        owners — no owner publish step.  An org owner can still delete or unpublish a collection
        after the fact.
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("A collection title is required.")

        members = []
        for ref in memberRefs:
            nwo = self.normalizeRepoRef(ref)
            if nwo and nwo not in members:
                members.append(nwo)
        if len(members) < 2:
            raise ValueError("A collection needs at least two member repositories.")

        me = self.whoami()
        slug = self.newCollectionSlug()
        nameWithOwner = f"{self.morphoDepotOrg}/{slug}"

        self.progressMethod(f"Creating collection {nameWithOwner} (public)...")
        # --add-readme initializes a default branch, so the contents API below can write to it
        # (a brand-new empty repo has no branch and the PUT would 404 "branch not found").
        self.gh(["repo", "create", nameWithOwner, "--public", "--disable-wiki",
                 "--add-readme", "--description", title])

        # Everything after repo creation must succeed for the repo to be a usable, discoverable
        # collection.  If any critical step fails, roll back the orphaned public repo so we don't
        # leave behind an undiscoverable repo (no md-collection topic) with an opaque name the user
        # can't find.  (README/CURATOR/topics are critical; the team grant is best-effort.)
        try:
            teamSlug = f"{me}-team".lower()
            try:
                self.gh(["api", "--method", "PUT",
                         f"/orgs/{self.morphoDepotOrg}/teams/{teamSlug}/repos/{nameWithOwner}",
                         "--field", "permission=push"])
            except Exception as e:
                logging.warning(f"Could not grant {teamSlug} Write on {nameWithOwner}: {e}")

            self._putRepoFile(nameWithOwner, "README.md",
                              self._renderCollectionReadme(title, description, members),
                              "Add collection README")
            self._putRepoFile(nameWithOwner, "CURATOR", me + "\n", "Add CURATOR file")

            self.gh(["repo", "edit", nameWithOwner,
                     "--add-topic", DISCOVERY_TOPIC, "--add-topic", COLLECTION_TOPIC])
        except Exception:
            self.progressMethod(f"Rolling back incomplete collection {nameWithOwner}...")
            try:
                # Best-effort; needs the gh 'delete_repo' scope.
                self.gh(["repo", "delete", nameWithOwner, "--yes"], quietErrors=True)
            except Exception as delErr:
                logging.warning(f"Could not roll back {nameWithOwner} after a failed create "
                                f"(delete it manually; the gh token may lack delete_repo scope): {delErr}")
            raise

        try:
            self.notifyRepoClerk(nameWithOwner)
        except Exception as e:
            logging.warning(f"Could not notify RepoClerk for {nameWithOwner}: {e}")

        return nameWithOwner
