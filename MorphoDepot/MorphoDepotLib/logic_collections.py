"""MorphoDepotLogic CollectionsMixin — create and list curated "repo of repos" collections.

A *collection* is a MorphoDepot org repository tagged ``md-collection`` whose README's first
line is the collection title and whose body lists member dataset repositories.  RepoClerk parses
that README and renders the collection as a gallery + screenshot slide deck.  See
SlicerMorph/SlicerMorphoDepot#180 (this tab) and MorphoDepot/RepoClerk#411 (rendering).

The whole flow runs with the member's OWN ``gh`` (they create private in-org repos and are repo
admin of their own creation) — no App/Administration privilege, consistent with the admin-free
posture.  Governance is deliberately simple: one ``CURATOR`` (the creator) for attribution; anyone
else contributes via a standard fork-and-PR.
"""
import base64
import logging
import re
import unicodedata

COLLECTION_TOPIC = "md-collection"
DISCOVERY_TOPIC = "morphodepot"
SLUG_MAX_LEN = 40


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

    # --- Slug / reference normalization ---

    @staticmethod
    def slugifyTitle(title):
        """Derive a short, URL-safe repo slug from a free-text collection title (diacritics
        stripped, lowercased, non-alphanumerics collapsed to hyphens, length-capped on a word
        boundary)."""
        text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        if len(text) > SLUG_MAX_LEN:
            text = text[:SLUG_MAX_LEN].rsplit("-", 1)[0] or text[:SLUG_MAX_LEN]
        return text or "collection"

    def uniqueCollectionSlug(self, title):
        """A slug for ``title`` that does not collide with an existing repo in the org."""
        base = self.slugifyTitle(title)
        slug, n = base, 2
        while self.repoExists(f"{self.morphoDepotOrg}/{slug}"):
            suffix = f"-{n}"
            slug = (base[:SLUG_MAX_LEN - len(suffix)].rstrip("-") or base) + suffix
            n += 1
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
                  "<!-- Created with the MorphoDepot extension. Add or remove member repositories "
                  "in the list above (one per line); RepoClerk re-renders the gallery on update. -->",
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
        slug = self.uniqueCollectionSlug(title)
        nameWithOwner = f"{self.morphoDepotOrg}/{slug}"

        self.progressMethod(f"Creating collection {nameWithOwner} (public)...")
        # --add-readme initializes a default branch, so the contents API below can write to it
        # (a brand-new empty repo has no branch and the PUT would 404 "branch not found").
        self.gh(["repo", "create", nameWithOwner, "--public", "--disable-wiki",
                 "--add-readme", "--description", title])

        # Grant the creator's {login}-team Write (mirrors dataset creation; best-effort).
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

        try:
            self.notifyRepoClerk(nameWithOwner)
        except Exception as e:
            logging.warning(f"Could not notify RepoClerk for {nameWithOwner}: {e}")

        return nameWithOwner
