"""MorphoDepotLogic AccessionMixin (split from MorphoDepot.py)."""
import os
import re
import sys
import csv
import glob
import json
import time
import math
import locale
import random
import shutil
import logging
import platform
import datetime
import fnmatch
import tempfile
import traceback
import subprocess
from contextlib import contextmanager
import git
import requests
import qt
import ctk
import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from urllib.parse import urlparse


# --------------------------------------------------------------------------------------
# Specimen-accession resolver: turn a pasted public-database record URL into a Darwin Core
# triplet (institutionCode:collectionCode:catalogNumber, e.g. "UWBM:Mamm:82522") for the README
# and MorphoDepotAccession.json.  Best-effort and self-contained: never raises, times every
# network call, and degrades to {resolved: False} (raw URL retained) for anything it cannot
# resolve.  GBIF and iDigBio cost one API call each; the triplet is in the Arctos URL path.
# --------------------------------------------------------------------------------------

_ACCESSION_TIMEOUT = 10
# Colon tokens that mark a scheme/URN wrapper rather than an institution/collection code -- guards
# against uuid/urn/doi identifiers that would otherwise look triplet-shaped.
_NON_INSTITUTION_TOKENS = {"urn", "uuid", "http", "https", "doi", "ark", "hdl", "guid", "catalog", "uri", "url"}
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


def _tripletFromIdentifier(s):
    """Extract a DwC triplet INST:COLL:CAT from an occurrenceID/GUID that may be a bare triplet, a
    URN (URN:catalog:INST:COLL:CAT), or a URL (.../guid/INST:COLL:CAT).  None if no real triplet."""
    if not s or not isinstance(s, str):
        return None
    tail = s.rstrip("/").split("/")[-1]            # URL -> last path segment; URN/triplet unchanged
    tail = tail.split("?", 1)[0].split("#", 1)[0]  # drop any query string / fragment on that segment
    parts = [p for p in tail.split(":") if p != ""]
    if len(parts) < 3:
        return None
    inst, coll, cat = parts[-3], parts[-2], parts[-1]
    if inst.lower() in _NON_INSTITUTION_TOKENS or coll.lower() in _NON_INSTITUTION_TOKENS:
        return None
    if not _CODE_RE.match(inst) or not _CODE_RE.match(coll) or not cat:
        return None
    return f"{inst}:{coll}:{cat}"


def _tripletFromRecord(rec):
    """Priority ladder over a normalized record: occurrenceID (most reliable) -> a catalogNumber
    that already carries the triplet -> assembled from the three DwC fields.  Returns (triplet, via).
    occurrenceID wins because providers overload institution/collection/catalog inconsistently (a
    GBIF record can stuff the whole triplet into catalogNumber and put "Mammalogy" in collectionCode),
    so naive assembly is unreliable; occurrenceID carries a triplet-bearing identifier far more often."""
    triplet = _tripletFromIdentifier(rec.get("occurrenceID"))
    if triplet:
        return triplet, "occurrenceID"
    cat = (rec.get("catalogNumber") or "").strip()
    catTriplet = _tripletFromIdentifier(cat)
    if cat.count(":") >= 2 and catTriplet:
        return catTriplet, "catalogNumber"
    inst = (rec.get("institutionCode") or "").strip()
    coll = (rec.get("collectionCode") or "").strip()
    # Only assemble when inst/coll actually look like codes -- some providers return verbose names
    # ("University of Washington"), which would make a malformed triplet.
    if inst and coll and cat and _CODE_RE.match(inst) and _CODE_RE.match(coll):
        return f"{inst}:{coll}:{cat}", "assembled"
    return None, None


def extractAccession(url):
    """Best-effort resolve of a pasted specimen-record URL.  Returns a dict with keys url, source,
    specimenIdentifier, via, scientificName, resolved (+ error on exception).  Never raises."""
    out = {"url": url, "source": "Unknown", "specimenIdentifier": None,
           "via": None, "scientificName": None, "resolved": False}
    try:
        host = (urlparse(url).hostname or "").lower()   # .hostname drops any :port / userinfo
        if host == "arctos.database.museum" or host.endswith(".arctos.database.museum"):
            out["source"] = "Arctos"
            triplet = _tripletFromIdentifier(url)          # triplet is in the URL path; no network
            out.update(specimenIdentifier=triplet, via="url-path", resolved=bool(triplet))
        elif host == "gbif.org" or host.endswith(".gbif.org"):
            out["source"] = "GBIF"
            match = re.search(r"/occurrence/(\d+)", url)
            if match:
                data = requests.get(f"https://api.gbif.org/v1/occurrence/{match.group(1)}",
                                    timeout=_ACCESSION_TIMEOUT).json()
                rec = {k: data.get(k) for k in ("occurrenceID", "catalogNumber", "institutionCode", "collectionCode")}
                triplet, via = _tripletFromRecord(rec)
                out.update(specimenIdentifier=triplet, via=via,
                           scientificName=data.get("species") or data.get("scientificName"), resolved=bool(triplet))
        elif host == "idigbio.org" or host.endswith(".idigbio.org"):
            out["source"] = "iDigBio"
            uuid = urlparse(url).path.rstrip("/").split("/")[-1]   # .path excludes query/fragment
            data = requests.get(f"https://search.idigbio.org/v2/view/records/{uuid}",
                                timeout=_ACCESSION_TIMEOUT).json()
            record = (data or {}).get("data", {}) or {}
            rec = {"occurrenceID": record.get("dwc:occurrenceID"), "catalogNumber": record.get("dwc:catalogNumber"),
                   "institutionCode": record.get("dwc:institutionCode"), "collectionCode": record.get("dwc:collectionCode")}
            triplet, via = _tripletFromRecord(rec)
            out.update(specimenIdentifier=triplet, via=via, scientificName=record.get("dwc:scientificName"), resolved=bool(triplet))
    except Exception as e:
        out["error"] = repr(e)
        logging.warning(f"Accession extraction failed for {url!r}: {e}")
    return out


class AccessionMixin:
    def _resolveSpeciesString(self, accessionData, fallbackSpecies=""):
        """The species string for the README/topic.  Uses the directly-entered species field, which
        the Create form already validates against the GBIF backbone taxonomy; on the edit path
        `fallbackSpecies` (the species already recorded for the repo) covers a blank entry so a
        re-save never blanks it.  The accessioned-specimen URL is provenance only and is NOT parsed
        for species (see extractAccession / _injectAccessionFields)."""
        return accessionData.get('species', ['', ''])[1] or fallbackSpecies or "Unknown species"

    def _accessionURL(self, accessionData):
        """The raw accessioned-specimen record URL from accessionData (stored under the legacy
        ``iDigBioURL`` key as a ``[label, answer]`` pair).  '' when absent."""
        value = accessionData.get('iDigBioURL')
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return (value[1] or "").strip()
        return value.strip() if isinstance(value, str) else ""

    def _injectAccessionFields(self, accessionData):
        """Resolve the accessioned-specimen record (best-effort) and stamp the derived provenance
        fields (``specimenIdentifier`` / ``accessionSource`` / ``accessionResolved``) into
        accessionData in place, for MorphoDepotAccession.json and the README.  Returns the full
        extraction dict.  Never raises and never blocks staging: an absent/unresolvable URL simply
        yields ``accessionResolved = False`` with the raw URL retained."""
        url = self._accessionURL(accessionData)
        info = extractAccession(url) if url else {}
        accessionData['specimenIdentifier'] = info.get('specimenIdentifier')
        accessionData['accessionSource'] = info.get('source', 'Unknown')
        accessionData['accessionResolved'] = bool(info.get('resolved'))
        return info

    def _writeLicense(self, repoDir, accessionData):
        """Write LICENSE.txt for the chosen Creative Commons license."""
        if accessionData["license"][1].startswith("CC BY-NC"):
            licenseURL = "https://creativecommons.org/licenses/by-nc/4.0/legalcode.txt"
        else:
            licenseURL = "https://creativecommons.org/licenses/by/4.0/legalcode.txt"
        response = requests.get(licenseURL, timeout=15)
        response.raise_for_status()  # never commit a 4xx/5xx error page as the repo's LICENSE.txt
        with open(os.path.join(repoDir, "LICENSE.txt"), "w") as fp:
            fp.write(response.content.decode('ascii', errors="ignore"))

    def _renderReadme(self, accessionData, speciesString, screenshotItems=None):
        """Build README.md text from accession data.  screenshotItems is an ordered list of
        (filename, caption) for images under the screenshots/ directory."""
        # The accessioned-specimen provenance line: the resolved Darwin Core triplet linked to its
        # public record when we have both, else the bare record URL, else nothing.
        specimenId = accessionData.get('specimenIdentifier')
        accessionURL = self._accessionURL(accessionData)
        if specimenId and accessionURL:
            specimenLine = f"* Accessioned specimen: {specimenId} ([record]({accessionURL}))\n"
        elif accessionURL:
            specimenLine = f"* Accessioned specimen record: {accessionURL}\n"
        else:
            specimenLine = ""
        readme_content = f"""
## MorphoDepot Repository
Repository for segmentation of a specimen scan.  See [this JSON file](MorphoDepotAccession.json) for specimen details.
* Species: {speciesString}
{specimenLine}* Modality: {accessionData['modality'][1]}
* Contrast: {accessionData['contrastEnhancement'][1]}
* Dimensions: {accessionData['scanDimensions']}
* Spacing (mm): {accessionData['scanSpacing']}
"""
        if screenshotItems:
            readme_content += "\n\n## Screenshots\n"
            for filename, caption in screenshotItems:
                readme_content += f"\n![{caption or filename}](screenshots/{filename})\n"
                if caption:
                    readme_content += f"_{caption}_\n"
        return readme_content

    def _readScreenshotCaptions(self, repoDir):
        """Read an existing repo's screenshots/captions.json into an ordered list of
        (filename, caption) so the README can be regenerated without the live screenshots."""
        captionsPath = os.path.join(repoDir, "screenshots", "captions.json")
        if not os.path.exists(captionsPath):
            return []
        try:
            with open(captionsPath) as fp:
                captions = json.load(fp)
        except Exception:
            return []
        return [(name, captions[name]) for name in sorted(captions.keys())]

    def _speciesFromReadme(self, repoDir):
        """Extract the already-recorded species from a repo's committed README (the
        '* Species: ...' line).  Used as the fallback when re-resolving species on edit so a
        transient iDigBio outage cannot blank a previously-resolved species."""
        readmePath = os.path.join(repoDir, "README.md")
        if not os.path.exists(readmePath):
            return ""
        try:
            with open(readmePath) as fp:
                for line in fp:
                    if line.strip().startswith("* Species:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    # Optional, opt-in at Create (gated on the `workflow` token scope): a GitHub Actions
    # workflow that assigns each newly opened issue back to whoever opened it.  Uses the
    # auto-provisioned GITHUB_TOKEN — no secrets to configure.  Verified to assign even
    # non-collaborator authors on public repos.
    autoAssignWorkflow = """name: Auto-assign issue to creator

# When a new issue is opened, assign it to the person who opened it.
# Uses the built-in GITHUB_TOKEN (auto-provisioned per run) — no secrets to configure.
on:
  issues:
    types: [opened]

jobs:
  assign:
    runs-on: ubuntu-latest
    permissions:
      issues: write
    steps:
      - name: Assign the new issue to its author
        uses: actions/github-script@v7
        with:
          script: |
            const author = context.payload.issue.user.login;
            const res = await github.rest.issues.addAssignees({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.payload.issue.number,
              assignees: [author],
            });
            const assigned = (res.data.assignees || []).map(a => a.login);
            if (!assigned.includes(author)) {
              core.warning(`'${author}' was not assigned (likely not an assignable user on this repo).`);
            }
"""

    def _stageRepoFiles(self, repoDir, sourceVolume, colorTable, accessionData, sourceSegmentation=None, screenshots=None, useOrg=False, targetOwner=None, enableAutoAssign=False):
        """Build the repository content on disk: save every file (including the CURATOR
        file), `git init`, and make the initial commit.  No GitHub interaction beyond the
        non-GitHub license/iDigBio lookups.  `enableAutoAssign` additionally writes a GitHub
        Actions workflow that assigns new issues back to their creator (opt-in, scope-gated in
        the UI).  Returns a build context dict consumed by provisionStagedRepo()."""

        # The CURATOR is the person creating the repo and is responsible for reviewing its
        # segmentation PRs.  It is always the creator, regardless of where the repo ends up.
        curator = self.whoami()
        repoName = os.path.basename(repoDir.rstrip(os.sep))

        os.makedirs(repoDir)

        # save data
        repoFileNames = []
        sourceFileName = sourceVolume.GetName()
        sourceFilePath = os.path.join(repoDir, sourceFileName) + ".nrrd"
        slicer.util.saveNode(sourceVolume, sourceFilePath, properties={'useCompression': True})

        # Size cap depends on tier: members upload to S3 via multipart (10 GiB cap); non-members
        # store the volume as a GitHub release asset (2 GiB limit). See docs/ObjectStorage-model.md.
        sourceBytes = os.path.getsize(sourceFilePath)
        sizeGB = sourceBytes / 2**30
        if useOrg:
            if sourceBytes > 10 * 2**30:
                raise ValueError(
                    f"This volume ({sizeGB:.1f} GB) exceeds the 10 GB limit; volumes that large are "
                    "not currently supported. Crop or resample the volume.")
        elif sourceBytes > 2 * 2**30:
            if self.userIsOrgMember():
                raise ValueError(
                    f"This volume ({sizeGB:.1f} GB) exceeds the 2 GB limit for personal "
                    "repositories. Create it under MorphoDepot instead — the org supports up to 10 GB.")
            raise ValueError(
                f"This volume is {sizeGB:.1f} GB. Personal repositories cap files at 2 GB (a GitHub "
                "restriction). To publish volumes up to 10 GB, join the MorphoDepot organization and "
                "create your repository there.")

        # calculate and save checksum
        checksum = slicer.util.computeChecksum('SHA256', sourceFilePath)
        checksumFilePath = os.path.join(repoDir, "source_volume_checksum")
        with open(checksumFilePath, "w") as fp:
            fp.write(f"SHA256:{checksum}")

        colorTableName = colorTable.GetName()
        slicer.util.saveNode(colorTable, os.path.join(repoDir, colorTableName) + ".csv")
        repoFileNames.append(f"{colorTableName}.csv")

        # Resolve the accessioned-specimen record (best-effort) so the derived provenance fields
        # land in MorphoDepotAccession.json and the README.  Never blocks staging.
        self._injectAccessionFields(accessionData)

        # write accessionData file
        accessionData['fileFormatVersion'] = self.accessionFileFormatVersion
        fp = open(os.path.join(repoDir, "MorphoDepotAccession.json"), "w")
        fp.write(json.dumps(accessionData, indent=4))
        fp.close()

        # write license file
        self._writeLicense(repoDir, accessionData)

        speciesString = self._resolveSpeciesString(accessionData)
        speciesTopicString = speciesString.lower().replace(" ", "-")

        # write readme file
        screenshotItems = None
        if screenshots:
            screenshotItems = [(f"screenshot-{i+1}.png", ss['caption']) for i, ss in enumerate(screenshots)]
        readme_content = self._renderReadme(accessionData, speciesString, screenshotItems)
        fp = open(os.path.join(repoDir, "README.md"), "w")
        fp.write(readme_content)
        fp.close()

        # write CURATOR file: the GitHub handle of the person responsible for curating this
        # repository and reviewing its segmentation PRs.  Always the creator, regardless of
        # whether the repo eventually lives under a personal account or an organization.
        with open(os.path.join(repoDir, "CURATOR"), "w") as fp:
            fp.write(f"{curator}\n")

        # create initial repo
        repo = git.Repo.init(repoDir, initial_branch='main')

        repoFileNames += [
            "README.md",
            "LICENSE.txt",
            "MorphoDepotAccession.json",
            "source_volume_checksum",
            "CURATOR",
        ]
        if sourceSegmentation:
            segmentationName = "baseline" # keyword used to detect segmentation to import when startin new issue
            slicer.util.saveNode(sourceSegmentation, os.path.join(repoDir, segmentationName) + ".seg.nrrd")
            repoFileNames.append(f"{segmentationName}.seg.nrrd")

        if screenshots:
            # Copy screenshots
            screenshotsDir = os.path.join(repoDir, "screenshots")
            os.makedirs(screenshotsDir, exist_ok=True)
            for i, screenshotInfo in enumerate(screenshots):
                newScreenshotName = f"screenshot-{i+1}.png"
                newScreenshotPath = os.path.join(screenshotsDir, newScreenshotName)
                shutil.copy(screenshotInfo['path'], newScreenshotPath)
                repoFileNames.append(os.path.join("screenshots", newScreenshotName))

            # Save captions to a file
            captions = {f"screenshot-{i+1}.png": ss['caption'] for i, ss in enumerate(screenshots)}
            captionsPath = os.path.join(screenshotsDir, "captions.json")
            with open(captionsPath, "w") as f:
                json.dump(captions, f, indent=2)
            repoFileNames.append(os.path.join("screenshots", "captions.json"))
        # Optional auto-assign workflow (opt-in at Create, only when the token has `workflow`
        # scope — gated in the UI).  Committed like any other file so it survives the staged
        # flow's history rewrites and ships with the published repo.
        if enableAutoAssign:
            workflowDir = os.path.join(repoDir, ".github", "workflows")
            os.makedirs(workflowDir, exist_ok=True)
            with open(os.path.join(workflowDir, "auto-assign.yml"), "w") as fp:
                fp.write(self.autoAssignWorkflow)
            repoFileNames.append(os.path.join(".github", "workflows", "auto-assign.yml"))

        repoFilePaths = [os.path.join(repoDir, fileName) for fileName in repoFileNames]
        repo.index.add(repoFilePaths)
        repo.index.commit("Initial commit")

        # Best-effort duplicate-volume check while the staging progress is already up, so the
        # network round-trip is hidden; surfaced passively in the Go-live status and at publish.
        # Skipped for the developer self-test (targetOwner) — test volumes are not real data.
        duplicateRepos = [] if targetOwner else self.duplicateVolumeRepos(checksum)

        return {
            "repoDir": repoDir,
            "repo": repo,
            "curator": curator,
            "repoName": repoName,
            "sourceFilePath": sourceFilePath,
            "sourceFileName": sourceFileName,
            "checksum": checksum,
            "duplicateRepos": duplicateRepos,
            "useOrg": useOrg,
            "targetOwner": targetOwner,
            "speciesTopicString": speciesTopicString,
            "species": speciesString,
            "committedFiles": repoFileNames,
        }

    def createAccessionRepo(self, sourceVolume, colorTable, accessionData, sourceSegmentation=None, screenshots=None, useOrg=None, targetOwner=None, enableAutoAssign=False):
        """Stage a new accession repository: build it locally, then provision it.  `useOrg`
        chooses the destination — True = born in the MorphoDepot org (members only; S3, 10 GB,
        governed); False = the creator's personal account (GitHub release asset, 2 GB, fully
        theirs).  When unspecified, defaults to the org for members.  `targetOwner` (set only by
        the developer self-test) overrides routing: provision directly into that org via the
        creator's own gh rights, non-member style (release asset, no App/S3).  Returns the staged
        nameWithOwner."""
        repoName = accessionData['githubRepoName'][1].split("/")[-1]
        if useOrg is None:
            useOrg = self.userIsOrgMember()
        if targetOwner:
            useOrg = False  # targetOwner forces the direct-to-org (non-member-style) path

        # Fail fast on a GitHub name collision (the authoritative check) BEFORE building
        # anything locally, so we never half-build for a name that is already taken.
        curator = self.whoami()
        # Collision-check the namespace the repo will actually land in.
        if targetOwner:
            target, where = f"{targetOwner}/{repoName}", f"the {targetOwner} organization"
        elif useOrg:
            target, where = f"{self.morphoDepotOrg}/{repoName}", f"the {self.morphoDepotOrg} organization"
        else:
            target, where = f"{curator}/{repoName}", f"your account ({curator})"
        if self.repoExists(target):
            raise ValueError(
                f"A repository named '{repoName}' already exists in {where}. "
                "If it is a repo you staged earlier but never published, reopen the MorphoDepot "
                "module to resume or discard it, or delete it on GitHub; otherwise choose a "
                "different name.")

        # Local clones are disposable working copies.  Clear any stale leftover (e.g. from a
        # previous interrupted attempt) so it never blocks a fresh build with the same name.
        base = os.path.abspath(self.localRepositoryDirectory())
        repoDir = os.path.abspath(os.path.join(base, repoName))
        # Safety (review S2): a crafted name ('.', '..', or separators) could make repoDir resolve to
        # the working directory itself or its parent -- rmtree would then wipe unrelated clones/caches.
        # Refuse any repoDir that is not a *strict* child of the working directory.  NOTE: the
        # `repoDir == base` arm is load-bearing -- commonpath([base, base]) == base, so the '.' case
        # would slip past the second check alone; do not "simplify" it away.
        if repoDir == base or os.path.commonpath([base, repoDir]) != base:
            raise ValueError(f"Invalid repository name {repoName!r} -- must be a plain repository name.")
        if os.path.exists(repoDir):
            self.progressMethod(f"Removing stale local directory {repoDir}")
            shutil.rmtree(repoDir, ignore_errors=True)

        buildContext = self._stageRepoFiles(repoDir, sourceVolume, colorTable, accessionData,
                                            sourceSegmentation, screenshots, useOrg=useOrg,
                                            targetOwner=targetOwner, enableAutoAssign=enableAutoAssign)
        return self.provisionStagedRepo(buildContext)

    def _provisionStagedRepoInOrg(self, buildContext):
        """Member tier: born IN-ORG (private, staged topic, {login}-team Write).  We upload the
        source volume to S3 FIRST, then create the repo and push the built content (which already
        carries the source_volume pointer) -- so a failed upload never leaves a half-provisioned
        staged repo on GitHub.  No personal account, no transfer.  Returns MorphoDepot/<name>."""
        repoDir = buildContext["repoDir"]
        repo = buildContext["repo"]
        curator = buildContext["curator"]
        repoName = buildContext["repoName"]
        sourceFilePath = buildContext["sourceFilePath"]
        sourceFileName = buildContext["sourceFileName"]
        checksum = buildContext["checksum"]

        nameWithOwner = f"{self.morphoDepotOrg}/{repoName}"

        # ORDERING (org tier): upload the source volume to S3 and bake its URL into the committed
        # content BEFORE creating anything on GitHub.  The S3 upload is the fallible external step
        # (its sign endpoint runs on a flaky host and large multipart uploads can fail), so doing it
        # FIRST means a failed upload leaves NO half-provisioned staged repo behind -- nothing exists
        # on GitHub until the volume is safely stored.  The upload is repo-independent (its key is
        # content-addressed {creator}/{repo}/{filename} + checksum), so it needs no GitHub repo and a
        # retry reuses the same object.  The name-collision preflight already ran in
        # createAccessionRepo before the local build, so we never upload against a taken name.  (A
        # create failure AFTER a good upload leaves only an orphaned, dedupable S3 object -- far
        # milder than an orphaned repo, and GC-able.)
        self.progressMethod(f"Uploading source volume for {nameWithOwner} to the object store...")
        publicURL = self.uploadSourceVolumeToObjectStore(
            sourceFilePath, checksum, curator, repoName, f"{sourceFileName}.nrrd")
        with open(os.path.join(repoDir, "source_volume"), "w") as fp:
            fp.write(publicURL)
        repo.index.add([f"{repoDir}/source_volume"])
        repo.index.commit("Add source volume url")

        # Only now touch GitHub.  Member-driven create (#20): the member creates the repo in-org with
        # their OWN gh -- they have create-private rights and become its admin -- instead of asking the
        # App. This is what lets the App drop its Administration permission. The member (admin) retains
        # push regardless; the {login}-team grant for other collaborators can be added later without the App.
        self.progressMethod(f"Creating {nameWithOwner} (private)...")
        self.gh(["repo", "create", nameWithOwner, "--private", "--disable-wiki"])
        cloneURL = f"https://github.com/{nameWithOwner}.git"
        # Mark it staged with the member's own gh (members own their topics now; the App does not).
        self.gh(["repo", "edit", nameWithOwner, "--add-topic", self.stagingTopic])

        # Grant the member's {login}-team Write, so the repo is reachable THROUGH the team.  The Create
        # tab's unpublished list queries `affiliation=organization_member` (team-based); without this
        # grant the member reaches the repo only as its direct-collaborator creator and it never shows.
        # The member is repo-admin (creator) and the team's maintainer, so their OWN gh performs this --
        # no App Administration is involved.  GitHub gates this on admin of THIS repo, so a member can
        # only ever team-grant repos they created (verified: a read-only repo returns 403).  Best-effort:
        # a failure here only delays the repo appearing in the list; staging itself still succeeds.
        teamSlug = f"{curator}-team".lower()
        try:
            self.gh(["api", "--method", "PUT",
                     f"/orgs/{self.morphoDepotOrg}/teams/{teamSlug}/repos/{nameWithOwner}",
                     "--field", "permission=push"])
        except Exception as e:
            logging.warning(f"Could not grant {teamSlug} Write on {nameWithOwner}: {e}")

        # Push the locally built content -- which already includes the source_volume pointer -- to the
        # empty in-org repo in a single push (member has Write via team).
        self.localRepo = repo
        try:
            repo.create_remote("origin", cloneURL)
        except Exception:
            repo.remote(name="origin").set_url(cloneURL)
        branchName = repo.active_branch.name
        lastError = None
        for delay in (0, 2, 4, 8, 16):
            if delay:
                self.progressMethod(f"Push retry in {delay}s...")
                time.sleep(delay)
            try:
                repo.git.push("--set-upstream", "origin", branchName)
                lastError = None
                break
            except Exception as pushError:
                lastError = pushError
        if lastError is not None:
            raise RuntimeError(f"Push to {nameWithOwner} failed: {lastError}")

        owner, name = nameWithOwner.split("/", 1)
        try:
            self.gh(["api", "--method", "PUT", f"/repos/{owner}/{name}/subscription",
                 "--field", "subscribed=true", "--field", "ignored=false"])
        except Exception as e:
            logging.warning(f"Could not subscribe to {nameWithOwner}: {e}")

        # v1 release (version anchor, no asset).  The source volume already lives in S3 and its
        # pointer was pushed with the initial content above, so nothing is uploaded here.
        self.gh(["release", "create", "--repo", nameWithOwner, "v1", "--notes", "Initial release"])

        self.stagingContext = {
            "repoDir": repoDir,
            "personalNameWithOwner": nameWithOwner,
            "repoName": repoName,
            "curator": curator,
            "speciesTopicString": buildContext["speciesTopicString"],
            "checksum": checksum,
            "duplicateRepos": buildContext.get("duplicateRepos", []),
            "isMember": True,
        }
        self.localRepo = None
        if os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        return nameWithOwner

    def provisionStagedRepo(self, buildContext):
        """Provision the staged repo.  Members: born in-org via the App control plane
        (_provisionStagedRepoInOrg).  Non-members: PRIVATE on the creator's personal account
        (the path below).  The developer self-test (`targetOwner`) uses this same non-member path
        but creates directly in the testing org via the creator's own gh rights.  Records staging
        state and returns the nameWithOwner."""
        if buildContext.get("useOrg"):
            return self._provisionStagedRepoInOrg(buildContext)
        repoDir = buildContext["repoDir"]
        repo = buildContext["repo"]
        curator = buildContext["curator"]
        repoName = buildContext["repoName"]
        sourceFilePath = buildContext["sourceFilePath"]
        sourceFileName = buildContext["sourceFileName"]
        checksum = buildContext["checksum"]

        # The GitHub name collision was already checked in createAccessionRepo (before build).
        # Owner is the creator's account by default, or an explicit targetOwner (testing org).
        owner = buildContext.get("targetOwner") or curator
        personalTarget = f"{owner}/{repoName}"

        # Create PRIVATE: the staging state is invisible (private + no topic) until go-live.
        try:
            self.gh(["repo", "create", personalTarget, "--disable-wiki", "--private",
                     "--source", repoDir, "--push"])
        except RuntimeError as e:
            # gh repo create --push can race with GitHub provisioning the new repo for
            # git-over-HTTPS access; the create succeeds but the immediate push fails with
            # "Repository not found". gh has already added the origin remote, so retry the
            # push from the local clone with the branch specified (no upstream is set yet).
            if "Repository not found" not in str(e):
                raise
            branchName = repo.active_branch.name
            lastError = None
            for delay in (2, 4, 8, 16):
                self.progressMethod(f"Initial push raced with repo provisioning; retrying in {delay}s...")
                time.sleep(delay)
                try:
                    repo.git.push("--set-upstream", "origin", branchName)
                    lastError = None
                    break
                except Exception as pushError:
                    lastError = pushError
            if lastError is not None:
                raise RuntimeError(f"Initial push retry failed after multiple attempts: {lastError}")

        self.localRepo = repo
        repoNameWithOwner = self.nameWithOwner("origin")

        self.gh(["repo", "edit", repoNameWithOwner, "--enable-projects=false", "--enable-discussions=false"])

        # Tag the repo as staged-but-unpublished.  This topic is the durable, queryable record
        # of staging state (no client-side marker); publish removes it.
        self.gh(["repo", "edit", repoNameWithOwner, "--add-topic", self.stagingTopic])

        # subscribe to all notifications for the new repository
        # gh repo watch was removed in newer gh CLI versions; use the API directly
        owner, name = repoNameWithOwner.split("/", 1)
        self.gh(["api", "--method", "PUT", f"/repos/{owner}/{name}/subscription",
                 "--field", "subscribed=true", "--field", "ignored=false"])

        # Non-member tier: create the v1 release and upload the source volume AS A RELEASE ASSET
        # (the volume lives on GitHub, capped at 2 GB). Members use S3 instead — see
        # _provisionStagedRepoInOrg and docs/org-design.md §1.0.
        commandList = ["release", "create", "--repo", repoNameWithOwner, "v1"]
        commandList += ["--notes", "Initial release"]
        self.gh(commandList)
        self.gh(["release", "upload", "--repo", repoNameWithOwner, "v1",
                 f"{sourceFilePath}#{sourceFileName}.nrrd"], timeout=None)  # large upload: no timeout

        # write source volume pointer: an owner-relative path resolved against the repo's current
        # owner at read time (resolveVolumeURL).
        fp = open(os.path.join(repoDir, "source_volume"), "w")
        fp.write(f"releases/download/v1/{sourceFileName}.nrrd")
        fp.close()

        repo.index.add([f"{repoDir}/source_volume"])
        repo.index.commit("Add source file url file")
        repo.remote(name="origin").push()

        self.stagingContext = {
            "repoDir": repoDir,
            "personalNameWithOwner": repoNameWithOwner,
            "repoName": repoName,
            "curator": curator,
            "speciesTopicString": buildContext["speciesTopicString"],
            "checksum": checksum,
            "duplicateRepos": buildContext.get("duplicateRepos", []),
        }

        # The local working copy is disposable now that the repo is fully on GitHub — remove it
        # (and the multi-GB source-volume file it holds) immediately.  Editing later re-clones
        # via the reopen path.
        self.localRepo = None
        if os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        return repoNameWithOwner

    def _publishStagedRepoInOrg(self, ctx):
        """Member tier (#20): submit the staged org repo to the App's review gate.  The App validates
        (#19): on a hard-check failure it AUTO-BOUNCES (returns changes_requested) — the repo stays
        private + staged and we surface the failures; otherwise it requests review (pending_review).
        Once a reviewer approves, the App mints the DOI but does NOT flip; the member's repeat publish
        then gets {status: approved, flip: True}, and the MEMBER flips visibility + sets the discovery
        topics here with their OWN gh (they are admin of their repo).  Returns a dict (changesRequested
        / pending) or the public nameWithOwner string once flipped."""
        repoName = ctx["repoName"]
        species = ctx.get("speciesTopicString")
        topics = ["morphodepot"] + ([f"md-{species}"] if species else [])
        finalNameWithOwner = ctx["personalNameWithOwner"]
        repoDir = ctx.get("repoDir")
        self.progressMethod(f"Submitting {finalNameWithOwner} for review...")
        resp = self.controlPlaneRequest("repos/publish", {"repo": repoName, "topics": topics}) or {}
        status = resp.get("status")

        if status == "changes_requested":
            # #19 auto-bounce: automated validation found blocking issues.  The repo stays private +
            # staged; surface the failures so the curator can fix and re-submit (the local clone is
            # kept — reopen/edit is unchanged).
            return {"changesRequested": True, "nameWithOwner": finalNameWithOwner,
                    "validation": resp.get("validation"), "issueUrl": resp.get("issue_url")}

        if status == "pending_review":
            # Review requested.  The repo is still private and still carries morphodepot-staging, so it
            # remains in the unpublished list; drop only the transient local clone (reopen re-clones).
            self.localRepo = None
            if repoDir and os.path.exists(repoDir):
                shutil.rmtree(repoDir, ignore_errors=True)
            self.stagingContext = None
            return {"pending": True, "nameWithOwner": finalNameWithOwner,
                    "reviewSentTo": resp.get("review_sent_to")}

        if status == "approved" and resp.get("flip"):
            # Member-driven go-live (#20): the reviewer approved and the App minted the DOI WITHOUT
            # flipping.  The MEMBER now flips the repo public and swaps the staging topic for the
            # discovery topics, both with their own gh — so the App never needs Administration.
            self.progressMethod(f"Approved -- publishing {finalNameWithOwner}...")
            self.setRepoVisibility(finalNameWithOwner, public=True)
            self.addMorphoTopics(finalNameWithOwner, species)
            self.ghTopicClearCache()
            self.localRepo = None
            if repoDir and os.path.exists(repoDir):
                shutil.rmtree(repoDir, ignore_errors=True)
            try:
                self.notifyRepoClerk(finalNameWithOwner)
            except Exception as e:
                logging.warning(f"Could not notify RepoClerk of publish: {e}")
            self.stagingContext = None
            return finalNameWithOwner

        # Backstop: a non-gated immediate publish (legacy App-flips mode) already made it public.
        self.ghTopicClearCache()
        self.localRepo = None
        if repoDir and os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        try:
            self.notifyRepoClerk(finalNameWithOwner)
        except Exception as e:
            logging.warning(f"Could not notify RepoClerk of publish: {e}")
        self.stagingContext = None
        return finalNameWithOwner

    def publishStagedRepo(self):
        """Publish the staged repo IN PLACE — it already lives at its final location (members:
        in the MorphoDepot org via the App; non-members: their personal account; the developer
        self-test: the testing org).  Members go through the App's one-way private->public
        (_publishStagedRepoInOrg); everyone else flips public + adds discoverability topics
        directly.  No transfer.  Returns the final nameWithOwner."""
        ctx = getattr(self, "stagingContext", None)
        if not ctx:
            raise RuntimeError("No staged repository to publish.")
        if ctx.get("isMember"):
            return self._publishStagedRepoInOrg(ctx)
        personal = ctx["personalNameWithOwner"]
        speciesTopicString = ctx["speciesTopicString"]

        # Flip public, then add the discoverability topic(s) and drop the staging topic — the
        # moment the repo becomes discoverable and leaves the unpublished list.
        self.setRepoVisibility(personal, public=True)
        self.addMorphoTopics(personal, speciesTopicString)
        self.ghTopicClearCache()

        # The local working copy is no longer needed once published — remove it.
        repoDir = ctx.get("repoDir")
        self.localRepo = None
        if repoDir and os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        # Now public + discoverable — nudge RepoClerk to journal it (best-effort, see above).
        try:
            self.notifyRepoClerk(personal)
        except Exception as e:
            logging.warning(f"Could not notify RepoClerk of publish: {e}")
        self.stagingContext = None
        return personal

    def discardStagedRepo(self):
        """Abandon the staged repo.  Deleting a repository needs the `delete_repo` token
        scope, which we deliberately do not request — so instead of calling the API, we hand
        the user off to the repo's GitHub Settings page (Danger Zone) to delete it from the
        web, and clean up our own side: remove the local clone and the in-memory staging
        state.  Returns the repo's settings URL for the caller to open.  Note: if the user
        does not actually delete it, the repo keeps its `morphodepot-staging` topic and so
        correctly stays in the unpublished list."""
        ctx = getattr(self, "stagingContext", None)
        if not ctx:
            return None
        personal = ctx["personalNameWithOwner"]
        repoDir = ctx.get("repoDir")
        if ctx.get("isMember"):
            # Member tier: ask the App to free the S3 object.  Administration-free cutover (#20): the
            # App cannot delete the repo, so it returns ``member_must_discard`` and the member marks it
            # ``morphodepot-discarded`` with their OWN gh (repo-admin) -- it leaves the unpublished list
            # and a cleanup job removes it.  (Legacy: the App deletes the repo + volume outright.)
            self.progressMethod(f"Discarding {personal}...")
            resp = self.controlPlaneRequest("repos/discard", {"repo": ctx["repoName"]}) or {}
            if resp.get("member_must_discard"):
                try:
                    self.gh(["repo", "edit", personal, "--add-topic", "morphodepot-discarded",
                             "--remove-topic", self.stagingTopic])
                except Exception as e:
                    logging.warning(f"Could not mark {personal} discarded: {e}")
            self.localRepo = None
            if repoDir and os.path.exists(repoDir):
                shutil.rmtree(repoDir, ignore_errors=True)
            self.stagingContext = None
            return None    # member's view is cleared; the marked repo is removed by the cleanup job
        self.localRepo = None
        if repoDir and os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        self.stagingContext = None
        return f"https://github.com/{personal}/settings"

    # --- Staged-repo recovery via the `morphodepot-staging` topic.  GitHub is the source of
    # truth: a staged-but-unpublished repo is simply one carrying this topic.  No durable
    # client state, so recovery works from any machine and survives a /tmp flush. ---

    stagingTopic = "morphodepot-staging"

    def listStagedRepos(self):
        """Return the active user's repositories that are staged but not yet published,
        identified by the `morphodepot-staging` topic.  Uses the repo LIST endpoint (topics
        are reflected immediately, unlike the search index which lags for fresh repos), so it
        is reliable right after staging and from any machine.  Returns marker-shaped dicts."""
        me = self.whoami()
        # Staged repos can live in the user's personal account (personal-tier staging) or the
        # MorphoDepot org (member-tier — owned by the org, reachable via the {handle}-team); both
        # paths tag the repo `morphodepot-staging`.  Query exactly those two scopes rather than
        # `/user/repos?affiliation=owner,organization_member --paginate`, which walks every repo of
        # every org the user belongs to just to keep the few staged ones — bounded here regardless
        # of how many repos live in the user's other orgs.
        repos = []
        for endpoint in ("/user/repos?affiliation=owner&per_page=100",
                         f"/orgs/{self.morphoDepotOrg}/repos?per_page=100"):
            try:
                repos.extend(self.ghJSON(["api", endpoint, "--paginate"]) or [])
            except Exception as e:
                logging.warning(f"listStagedRepos: could not list {endpoint}: {e}")
        staged = []
        seen = set()
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            owner = (repo.get("owner") or {}).get("login")
            if owner not in (me, self.morphoDepotOrg):
                continue
            if self.stagingTopic not in (repo.get("topics") or []):
                continue
            name = repo.get("name")
            nameWithOwner = repo.get("full_name") or f"{owner}/{name}"
            if nameWithOwner in seen:  # defensive de-dup (a repo can't be in both lists)
                continue
            seen.add(nameWithOwner)
            staged.append({
                "nameWithOwner": nameWithOwner,
                "repoName": name,
                "curator": me,
                "repoDir": os.path.join(self.localRepositoryDirectory(), name),
                "summary": nameWithOwner,
            })
        return staged

    def _fetchAccessionData(self, nameWithOwner):
        """Fetch and decode a repo's MorphoDepotAccession.json, or None if it has none.
        Used to re-derive the species topic when resuming a repo from the unpublished list."""
        try:
            data = self.ghJSON(["api", f"/repos/{nameWithOwner}/contents/MorphoDepotAccession.json"])
        except Exception:
            return None
        if not isinstance(data, dict) or "content" not in data:
            return None
        try:
            import base64
            decoded = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
            return json.loads(decoded)
        except Exception:
            return None

    def resumeStagedRepo(self, stagedRepo):
        """Resume a repo chosen from the unpublished list: CLONE it fresh (so its accession
        form can be pre-filled and edits applied), rebuild the in-memory staging context, and
        return the loaded accession data.  Cloning is cheap — the source volume is a release
        asset, not a git-tracked file — and gives a working tree for saveStagedRepoEdits()."""
        nameWithOwner = stagedRepo.get("nameWithOwner")
        repoName = stagedRepo.get("repoName")
        base = os.path.abspath(self.localRepositoryDirectory())
        repoDir = os.path.abspath(os.path.join(base, repoName or ""))
        # Same strict-child guard as stageRepo (review S2): repoName here comes from the GitHub repo
        # listing (GitHub itself rejects ./..), but guard for consistency before the rmtree.
        if repoDir == base or os.path.commonpath([base, repoDir]) != base:
            raise ValueError(f"Invalid repository name {repoName!r} -- must be a plain repository name.")
        if os.path.exists(repoDir):
            shutil.rmtree(repoDir, ignore_errors=True)
        self.gh(["repo", "clone", nameWithOwner, repoDir])
        self.localRepo = git.Repo(repoDir)

        accession = {}
        accessionPath = os.path.join(repoDir, "MorphoDepotAccession.json")
        if os.path.exists(accessionPath):
            try:
                with open(accessionPath) as fp:
                    accession = json.load(fp)
            except Exception as e:
                logging.warning(f"Could not read accession data for {nameWithOwner}: {e}")

        species = ""
        try:
            species = (accession.get("species") or ["", ""])[1] or ""
        except Exception:
            species = ""
        # Carry the committed checksum so the duplicate-volume warning works on reopen too
        # (the clone persists here, unlike a fresh stage whose dir is deleted after provision).
        checksum = None
        checksumPath = os.path.join(repoDir, "source_volume_checksum")
        if os.path.exists(checksumPath):
            try:
                with open(checksumPath) as fp:
                    checksum = fp.read().strip()
            except Exception as e:
                logging.warning(f"Could not read source_volume_checksum for {nameWithOwner}: {e}")
        self.stagingContext = {
            "repoDir": repoDir,
            "personalNameWithOwner": nameWithOwner,
            "repoName": repoName,
            "curator": stagedRepo.get("curator"),
            "speciesTopicString": species.lower().replace(" ", "-") if species else "",
            "checksum": checksum,
            "duplicateRepos": self.duplicateVolumeRepos(checksum, exclude=nameWithOwner),
            "isMember": nameWithOwner.split("/", 1)[0] == self.morphoDepotOrg,
        }
        return accession

    def saveStagedRepoEdits(self, accessionData, colorTable=None, sourceSegmentation=None, screenshots=None):
        """Apply edits to the currently-resumed staged repo (clone in stagingContext): rewrite
        the metadata-derived files from `accessionData`, optionally replace the color table
        and/or baseline segmentation, regenerate the screenshots from `screenshots` (None =
        leave as-is), then — only if something actually changed — rewrite the repo's `main` as
        a single clean commit and force-push it.  The source volume and its release asset are
        NEVER touched (out of scope by design).  Recomputes the staging context's species topic
        for the subsequent publish.  Returns True if a change was pushed, False if the repo was
        already up to date."""
        ctx = getattr(self, "stagingContext", None)
        if not ctx:
            raise RuntimeError("No staged repository is open to edit.")
        repoDir = ctx.get("repoDir")
        repo = self.localRepo
        if not (repoDir and repo and os.path.exists(repoDir)):
            raise RuntimeError("No local working copy is available to apply edits.")

        # Capture the already-recorded species (from the committed README, before we rewrite
        # it) so a transient iDigBio outage during re-resolution can't blank it.
        priorSpecies = self._speciesFromReadme(repoDir)

        # Regenerate the metadata-derived files from the edited data.  Re-resolve the accession
        # record in case the specimen URL was edited (best-effort; never blocks the re-save).
        self._injectAccessionFields(accessionData)
        accessionData['fileFormatVersion'] = self.accessionFileFormatVersion
        with open(os.path.join(repoDir, "MorphoDepotAccession.json"), "w") as fp:
            fp.write(json.dumps(accessionData, indent=4))

        speciesString = self._resolveSpeciesString(accessionData, fallbackSpecies=priorSpecies)
        ctx["speciesTopicString"] = speciesString.lower().replace(" ", "-") if speciesString else ""

        self._writeLicense(repoDir, accessionData)

        # Replace the color table only if a new one was supplied (else keep the committed CSV).
        if colorTable is not None:
            for existing in os.listdir(repoDir):
                if existing.endswith(".csv"):
                    os.remove(os.path.join(repoDir, existing))
            slicer.util.saveNode(colorTable, os.path.join(repoDir, colorTable.GetName()) + ".csv")

        # Replace the baseline segmentation only if a new one was supplied.
        if sourceSegmentation is not None:
            slicer.util.saveNode(sourceSegmentation, os.path.join(repoDir, "baseline.seg.nrrd"))

        # Screenshots: when a set is supplied, fully regenerate screenshots/ + captions.json
        # from it (add/remove/recaption); when None, leave the committed screenshots untouched.
        screenshotsDir = os.path.join(repoDir, "screenshots")
        if screenshots is not None:
            if os.path.exists(screenshotsDir):
                shutil.rmtree(screenshotsDir)
            screenshotItems = []
            if screenshots:
                os.makedirs(screenshotsDir, exist_ok=True)
                captions = {}
                for i, ss in enumerate(screenshots):
                    name = f"screenshot-{i+1}.png"
                    shutil.copy(ss["path"], os.path.join(screenshotsDir, name))
                    captions[name] = ss.get("caption", "")
                    screenshotItems.append((name, ss.get("caption", "")))
                with open(os.path.join(screenshotsDir, "captions.json"), "w") as f:
                    json.dump(captions, f, indent=2)
        else:
            screenshotItems = self._readScreenshotCaptions(repoDir)

        # Regenerate the README (screenshot section reflects the current screenshot set).
        readme = self._renderReadme(accessionData, speciesString, screenshotItems)
        with open(os.path.join(repoDir, "README.md"), "w") as fp:
            fp.write(readme)

        # Push only if the working tree actually changed.
        repo.git.add("-A")
        if not repo.git.diff("--cached", "--name-only").strip():
            return False

        # Reset history to a single clean commit ("as if created correctly") and force-push.
        repo.git.checkout("--orphan", "_morphodepot_clean")
        repo.git.add("-A")
        repo.git.commit("-m", "MorphoDepot accession")
        repo.git.branch("-M", "main")
        repo.git.push("--force", "origin", "main")
        return True

    #
    # Search
    #
