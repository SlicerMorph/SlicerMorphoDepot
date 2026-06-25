"""MorphoDepotLogic ReleaseMixin (split from MorphoDepot.py)."""
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


class ReleaseMixin:
    def getReleases(self):
        """Get list of releases for the current repository (latest first)."""
        if not self.localRepo:
            return None
        originNameWithOwner = self.nameWithOwner("origin")
        return self.ghJSON(["release", "list", "--repo", originNameWithOwner,
                            "--json", "name,tagName,publishedAt"])

    def closedIssuesSinceLastRelease(self, nameWithOwner):
        """Return a list of {number,title} for issues closed since the last published release.
        If there is no prior release, returns all closed issues for the repo.  Pre-release
        ANNOUNCEMENT issues are excluded: they carry the invisible release-announce marker and are
        not contributions -- retiring an announcement closes it, which would otherwise make it show
        up as a 'change in this release' in the next cycle (the bug this filters out)."""
        releases = self.ghJSON(["release", "list", "--repo", nameWithOwner,
                               "--json", "tagName,publishedAt"]) or []
        sinceDate = releases[0].get('publishedAt') if releases else None
        if sinceDate:
            cmd = ["issue", "list", "--repo", nameWithOwner,
                   "--json", "number,title,body",
                   "--search", f"is:issue is:closed closed:>{sinceDate}"]
        else:
            cmd = ["issue", "list", "--repo", nameWithOwner, "--state", "closed",
                   "--json", "number,title,body"]
        issues = self.ghJSON(cmd) or []
        return [{"number": i["number"], "title": i["title"]} for i in issues
                if self.releaseAnnounceMarkerName not in (i.get("body") or "")]

    def nextReleaseTag(self):
        """Compute the next vN tag based on existing releases. Returns None if no repo loaded."""
        if not self.localRepo:
            return None
        releases = self.getReleases() or []
        nextVersion = 1
        tagNames = [r['tagName'] for r in releases if r['tagName'].startswith('v')]
        versions = [int(t[1:]) for t in tagNames if t[1:].isdigit()]
        if versions:
            nextVersion = max(versions) + 1
        return f"v{nextVersion}"

    def previousReleaseTag(self):
        """Latest existing release tag, or None if no releases yet."""
        if not self.localRepo:
            return None
        releases = self.getReleases() or []
        tagNames = [r['tagName'] for r in releases if r['tagName'].startswith('v')]
        versions = [int(t[1:]) for t in tagNames if t[1:].isdigit()]
        if not versions:
            return None
        return f"v{max(versions)}"

    def existingScreenshotCount(self):
        """Highest existing screenshot-N.png index in the loaded repo's screenshots/ directory."""
        if not self.localRepo:
            return 0
        screenshotsDir = os.path.join(self.localRepo.working_dir, "screenshots")
        if not os.path.exists(screenshotsDir):
            return 0
        highest = 0
        for entry in os.listdir(screenshotsDir):
            match = re.match(r"^screenshot-(\d+)\.png$", entry)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest

    def releaseSnapshotPlan(self, newTag, baselineNode, colorTableNode, screenshots):
        """Build a description of what prepareReleaseSnapshot will do, for the confirmation UI."""
        if not self.localRepo:
            return None
        repoDir = self.localRepo.working_dir
        previousTag = self.previousReleaseTag()
        issueSegFiles = sorted(os.path.basename(p) for p in glob.glob(os.path.join(repoDir, "issue-*.seg.nrrd")))
        startIndex = self.existingScreenshotCount() + 1
        return {
            'newTag': newTag,
            'previousTag': previousTag,
            'baselineName': baselineNode.GetName() if baselineNode else None,
            'colorTableName': colorTableNode.GetName() if colorTableNode else None,
            'newScreenshotNames': [f"screenshot-{startIndex + i}.png" for i in range(len(screenshots or []))],
            'issueSegFiles': issueSegFiles,
            'archivedReadme': f"README-{previousTag}.md" if previousTag else None,
        }

    def generateReleaseReadme(self, newTag, newScreenshotEntries):
        """Build a new README.md body for the given release. Reuses metadata from
        MorphoDepotAccession.json. Links each previous release to its tag's tree on
        GitHub so the reader sees the repository at that release stage. Lists only
        screenshots from this release flow (older screenshots stay in their archived READMEs)."""
        repoDir = self.localRepo.working_dir
        accessionPath = os.path.join(repoDir, "MorphoDepotAccession.json")
        accession = {}
        if os.path.exists(accessionPath):
            try:
                with open(accessionPath) as f:
                    accession = json.load(f)
            except Exception as e:
                logging.warning(f"Could not parse MorphoDepotAccession.json: {e}")

        def field(key, default=""):
            v = accession.get(key)
            if isinstance(v, list) and len(v) >= 2:
                return v[1] or default
            return v or default

        species = field('species', "Unknown species")
        modality = field('modality', "Unknown")
        contrast = field('contrastEnhancement', "Unknown")
        scanDimensions = accession.get('scanDimensions', "Unknown")
        scanSpacing = accession.get('scanSpacing', "Unknown")

        try:
            originNameWithOwner = self.nameWithOwner("origin")
        except Exception:
            originNameWithOwner = None

        lines = [f"# Release {newTag}", ""]

        previousReadmes = sorted(
            glob.glob(os.path.join(repoDir, "README-v*.md")),
            key=lambda p: int(os.path.basename(p).replace("README-v", "").replace(".md", "")),
        )
        versions = []
        for p in previousReadmes:
            nStr = os.path.basename(p).replace("README-v", "").replace(".md", "")
            if nStr.isdigit():
                versions.append(int(nStr))
        newTagN = int(newTag[1:]) if newTag.startswith('v') and newTag[1:].isdigit() else None
        if versions:
            lines.append("## Previous releases")
            # Each previous version vN links to the pre-release-v(N+1) branch's README.md:
            # that branch captured main right before v(N+1) was prepped, i.e. the README as
            # it was while vN was the most recent release (and reflects any edits made to
            # README.md between vN and v(N+1)).
            for i, v in enumerate(versions):
                nextN = versions[i + 1] if i + 1 < len(versions) else newTagN
                if originNameWithOwner and nextN is not None:
                    url = f"https://github.com/{originNameWithOwner}/blob/pre-release-v{nextN}/README.md"
                    lines.append(f"- [v{v}]({url})")
                else:
                    lines.append(f"- [v{v}](README-v{v}.md)")
            lines.append("")

        lines.append("## MorphoDepot Repository")
        lines.append("Repository for segmentation of a specimen scan. See [this JSON file](MorphoDepotAccession.json) for specimen details.")
        lines.append(f"* Species: {species}")
        lines.append(f"* Modality: {modality}")
        lines.append(f"* Contrast: {contrast}")
        lines.append(f"* Dimensions: {scanDimensions}")
        lines.append(f"* Spacing (mm): {scanSpacing}")

        if newScreenshotEntries:
            lines.append("")
            lines.append(f"## Screenshots for {newTag}")
            for name, caption in newScreenshotEntries:
                altText = caption or name
                lines.append(f"![{altText}](screenshots/{name})")
                if caption:
                    lines.append(f"_{caption}_")

        # Contributors section, generated from CONTRIBUTORS.json (written just before this during the
        # release; org-design Sec.9.6). The DOI block is added separately by the App at mint time.
        try:
            import MorphoDepotLib.contributors as MDC
            contribPath = os.path.join(repoDir, "CONTRIBUTORS.json")
            if os.path.exists(contribPath):
                creditMarkdown = MDC.render_markdown(MDC.load(contribPath))
                if creditMarkdown:
                    lines.append("")
                    lines.append(creditMarkdown.rstrip())
        except Exception as e:
            logging.warning(f"Could not render contributors section: {e}")

        return "\n".join(lines) + "\n"

    def prepareReleaseSnapshot(self, newTag, baselineNode, colorTableNode, screenshots, candidateBranch=None):
        """Stage the working tree for a release tag: write baseline.seg.nrrd from the picked
        segmentation, overwrite the repo's color table with the picked one, rotate README.md
        to README-{previousTag}.md and generate a fresh README.md, append new screenshots
        with sequential numbering (and update screenshots/captions.json), drop issue-*.seg.nrrd
        from the working tree (still in git history), and commit.

        If `candidateBranch` is given (org/gated, Option C), the release commit is force-pushed to
        that branch and local main + working tree are reset back to the pre-release state -- main is
        NOT modified (the App archives main and fast-forwards it to the candidate on approval).
        Otherwise (personal) the commit is pushed straight to origin/main as before."""
        if not self.localRepo:
            return None
        repoDir = self.localRepo.working_dir
        previousTag = self.previousReleaseTag()
        baseSha = self.localRepo.head.commit.hexsha  # pre-release main, to restore in candidate mode

        # Baseline segmentation
        baselinePath = os.path.join(repoDir, "baseline.seg.nrrd")
        if not slicer.util.saveNode(baselineNode, baselinePath, properties={'useCompression': True}):
            raise RuntimeError(f"Failed to save baseline segmentation to {baselinePath}")

        # Color table — overwrite the existing repo color file (.csv preferred over .ctbl).
        csvPaths = glob.glob(f"{repoDir}/*.csv")
        ctblPaths = glob.glob(f"{repoDir}/*.ctbl")
        if csvPaths:
            colorTablePath = csvPaths[0]
        elif ctblPaths:
            colorTablePath = ctblPaths[0]
        else:
            colorTablePath = os.path.join(repoDir, f"{colorTableNode.GetName()}.csv")
        if not slicer.util.saveNode(colorTableNode, colorTablePath):
            raise RuntimeError(f"Failed to save color table to {colorTablePath}")

        # New screenshots — continue sequential numbering from existing files.
        newScreenshotEntries = []
        if screenshots:
            screenshotsDir = os.path.join(repoDir, "screenshots")
            os.makedirs(screenshotsDir, exist_ok=True)
            startIndex = self.existingScreenshotCount() + 1
            captionsPath = os.path.join(screenshotsDir, "captions.json")
            captions = {}
            if os.path.exists(captionsPath):
                try:
                    with open(captionsPath) as f:
                        captions = json.load(f)
                except Exception:
                    captions = {}
            for i, ss in enumerate(screenshots):
                name = f"screenshot-{startIndex + i}.png"
                shutil.copy(ss['path'], os.path.join(screenshotsDir, name))
                captions[name] = ss.get('caption', '')
                newScreenshotEntries.append((name, ss.get('caption', '')))
            with open(captionsPath, "w") as f:
                json.dump(captions, f, indent=2)

        # README rotation + fresh README for the new tag
        readmePath = os.path.join(repoDir, "README.md")
        if previousTag and os.path.exists(readmePath):
            shutil.move(readmePath, os.path.join(repoDir, f"README-{previousTag}.md"))
        with open(readmePath, "w") as f:
            f.write(self.generateReleaseReadme(newTag, newScreenshotEntries))

        # Drop per-issue segmentations from the working tree (kept in history)
        for path in glob.glob(os.path.join(repoDir, "issue-*.seg.nrrd")):
            os.remove(path)

        # Stage everything (added, modified, deleted), commit.
        self.localRepo.git.add("--all")
        self.localRepo.index.commit(f"Prepare release {newTag}")
        if candidateBranch:
            # Option C: publish the release commit to the candidate branch and leave main UNTOUCHED.
            # Always reset local main + working tree back to the pre-release state afterward (even if
            # the push fails), so an unapproved or re-submitted release never rewrites main.  The App
            # archives main and fast-forwards it to this candidate on approval.
            try:
                self.localRepo.git.push("--force", "origin", f"HEAD:refs/heads/{candidateBranch}")
            finally:
                self.localRepo.git.reset("--hard", baseSha)
        else:
            self.localRepo.remote(name="origin").push("main")

    def resetToReleaseBackup(self):
        """Hard-reset main to the pre-release archive branch and discard untracked changes.
        Local-only: does NOT undo anything that was already pushed to origin/main, and does
        NOT delete the archive branch (it is intentionally kept as a permanent record)."""
        backup = getattr(self, 'releaseBackupBranch', None)
        if not (self.localRepo and backup):
            return False
        self.localRepo.git.reset("--hard", backup)
        self.localRepo.git.clean("-fd")
        self.releaseBackupBranch = None
        return True

    def discardReleaseBackup(self):
        """Clear the in-memory reference to the archive branch after a successful release.
        The branch itself is intentionally kept (locally and on origin) as a permanent
        archive of the pre-release state."""
        self.releaseBackupBranch = None

    def createRelease(self, releaseNotes="", baselineSegmentationNode=None, colorTableNode=None, screenshots=None):
        """Create a new release.

        Org/archival repo (gated, Option C): the release commit is published to a
        `release-candidate-{tag}` branch and **main is left untouched**.  On approval the App archives
        the current main as `pre-release-{tag}` (preserving the per-issue contributions), fast-forwards
        main to the candidate, cuts the tag, and mints the DOI.  This keeps main/history intact through
        an unapproved or re-submitted (request-changes) release.  Returns a pending marker dict.

        Personal/short-term repo: no gate -- archive main, rewrite main directly, and create the tag.
        Returns the new tag."""
        if not self.localRepo:
            return None
        if baselineSegmentationNode is None or colorTableNode is None:
            raise RuntimeError("A baseline segmentation and color table must both be selected.")
        tag = self.nextReleaseTag()
        if tag is None:
            return None
        originNameWithOwner = self.nameWithOwner("origin")
        if releaseNotes == "":
            releaseNotes = f"Version {tag} release."
        originOwner = originNameWithOwner.split("/")[0]

        if originOwner == self.morphoDepotOrg:
            # Build the release commit on the candidate branch (main untouched), then request review.
            candidate = f"release-candidate-{tag}"
            self.prepareReleaseSnapshot(tag, baselineSegmentationNode, colorTableNode,
                                        screenshots or [], candidateBranch=candidate)
            repoName = originNameWithOwner.split("/")[1]
            resp = self.controlPlaneRequest(
                "repos/release", {"repo": repoName, "tag": tag, "notes": releaseNotes}) or {}
            if resp.get("status") == "pending_review":
                return {"pending": True, "tag": tag, "reviewSentTo": resp.get("review_sent_to")}
            return tag

        # Personal/short-term repo: archive main, rewrite main directly, then create the tag.
        backupName = f"pre-release-{tag}"
        if backupName in [head.name for head in self.localRepo.heads]:
            self.localRepo.git.branch("-D", backupName)
        self.localRepo.git.branch(backupName)
        self.releaseBackupBranch = backupName
        self.localRepo.remote(name="origin").push(backupName)
        self.prepareReleaseSnapshot(tag, baselineSegmentationNode, colorTableNode, screenshots or [])
        commandList = ["release", "create", tag, "--repo", originNameWithOwner]
        commandList += ["--notes", releaseNotes]
        self.gh(commandList)
        return tag

    def openIssuesAndPRs(self, nameWithOwner):
        """Return (issues, prs) lists of open items for the given repo, each with number and title."""
        issues = self.ghJSON(["issue", "list", "--repo", nameWithOwner, "--state", "open",
                             "--json", "number,title"])
        prs = self.ghJSON(["pr", "list", "--repo", nameWithOwner, "--state", "open",
                          "--json", "number,title"])
        return issues, prs

    def announceUpcomingRelease(self, nameWithOwner, deadlineISO, message):
        """Announce an upcoming release.  Posts a comment on every open issue and PR (so people
        who watch notifications are pinged) AND creates a dedicated, pinned, `release-pending`
        announcement issue (so people who only visit the repo see it).  The announcement issue
        body carries an invisible marker encoding the target tag and deadline, which is the
        repo-state signal `findReleaseAnnouncement` later reads.  Substitutes {deadline} in the
        message body. Returns (issueCount, prCount)."""
        issues, prs = self.openIssuesAndPRs(nameWithOwner)
        body = message.replace("{deadline}", deadlineISO)
        for issue in issues:
            n = str(issue['number'])
            self.progressMethod(f"Announcement on issue #{n}: {issue['title']}")
            self.gh(["issue", "comment", n, "--repo", nameWithOwner, "--body", body])
        for pr in prs:
            n = str(pr['number'])
            self.progressMethod(f"Announcement on PR #{n}: {pr['title']}")
            self.gh(["pr", "comment", n, "--repo", nameWithOwner, "--body", body])
        self._upsertReleaseAnnouncementIssue(nameWithOwner, deadlineISO, body)
        return len(issues), len(prs)

    def _releaseAnnounceMarker(self, tag, deadlineISO):
        """Invisible HTML-comment marker embedded in the announcement issue body."""
        return f"<!-- {self.releaseAnnounceMarkerName} tag={tag} deadline={deadlineISO} -->"

    def _ensureReleasePendingLabel(self, nameWithOwner):
        """Create (or update) the release-pending label; idempotent via --force."""
        try:
            self.gh(["label", "create", self.releasePendingLabel, "--repo", nameWithOwner,
                     "--color", "FBCA04",
                     "--description", "An upcoming release has been announced but not yet cut",
                     "--force"])
        except Exception as e:
            logging.warning(f"Could not ensure '{self.releasePendingLabel}' label: {e}")

    def _upsertReleaseAnnouncementIssue(self, nameWithOwner, deadlineISO, body):
        """Update the existing release-pending announcement issue in place (new title/body/
        deadline), or create+pin one if none exists. Editing in place — rather than close +
        recreate — keeps a single, stable announcement issue. Best-effort: a failure here must
        not abort the announcement comments above."""
        tag = self.nextReleaseTag() or ""
        self._ensureReleasePendingLabel(nameWithOwner)
        marker = self._releaseAnnounceMarker(tag, deadlineISO)
        title = f"Upcoming release {tag} - finish by {deadlineISO}".strip()
        fullBody = f"{body}\n\n{marker}"
        existing = self.findReleaseAnnouncement(nameWithOwner)
        if existing:
            n = str(existing["number"])
            try:
                self.gh(["issue", "edit", n, "--repo", nameWithOwner, "--title", title, "--body", fullBody])
            except Exception as e:
                logging.warning(f"Could not update announcement issue #{n}: {e}")
            return n
        try:
            url = self.gh(["issue", "create", "--repo", nameWithOwner,
                           "--title", title, "--body", fullBody,
                           "--label", self.releasePendingLabel])
        except Exception as e:
            logging.warning(f"Could not create announcement issue: {e}")
            return None
        number = url.strip().rstrip("/").split("/")[-1]
        try:
            self.gh(["issue", "pin", number, "--repo", nameWithOwner])
        except Exception as e:
            logging.warning(f"Could not pin announcement issue #{number}: {e}")
        return number

    def listReleaseAnnouncements(self, nameWithOwner):
        """All open release-pending announcement issues as [{'number','deadline'}], oldest first.
        Returns **None** if the check itself FAILED (e.g. transient gh/network error) vs **[]**
        when there genuinely are none — so callers can tell "couldn't check" from "none exist"
        and avoid falsely clearing UI state.  Normally 0 or 1, but a repo can carry duplicates
        from before the dedup guard."""
        try:
            items = self.ghJSON(["issue", "list", "--repo", nameWithOwner, "--state", "open",
                                 "--label", self.releasePendingLabel, "--json", "number,body"])
        except Exception as e:
            logging.warning(f"Could not list release announcements: {e}")
            return None
        found = []
        for item in items or []:
            body = item.get("body", "") or ""
            match = re.search(re.escape(self.releaseAnnounceMarkerName) + r"\b([^>]*)", body)
            if match:
                deadlineMatch = re.search(r"deadline=(\S+)", match.group(1))
                found.append({"number": item["number"],
                              "deadline": deadlineMatch.group(1) if deadlineMatch else None})
        return sorted(found, key=lambda a: a["number"])

    def findReleaseAnnouncement(self, nameWithOwner):
        """The open release-pending announcement {'number','deadline'}, or None (the first, if
        several exist).  Reads repo state only."""
        announcements = self.listReleaseAnnouncements(nameWithOwner)
        return announcements[0] if announcements else None

    def clearReleaseAnnouncement(self, nameWithOwner, tag=None, message=None):
        """Retire EVERY open pre-release announcement: unpin, remove the release-pending label,
        comment, and close each.  Used both after a release (default message) and when an
        announcement is superseded by a new one (caller passes `message`).  Clears ALL of them
        (not just the oldest) so a repo carrying duplicates is fully cleaned up.  Best-effort
        and idempotent."""
        if message is None:
            message = (f"Release {tag} has been published; closing this announcement." if tag
                       else "The release has been published; closing this announcement.")
        for announcement in (self.listReleaseAnnouncements(nameWithOwner) or []):
            n = str(announcement["number"])
            for command in (
                ["issue", "unpin", n, "--repo", nameWithOwner],
                ["issue", "edit", n, "--repo", nameWithOwner, "--remove-label", self.releasePendingLabel],
                ["issue", "comment", n, "--repo", nameWithOwner, "--body", message],
                ["issue", "close", n, "--repo", nameWithOwner],
            ):
                try:
                    self.gh(command)
                except Exception as e:
                    logging.warning(f"Announcement cleanup step failed (#{n} {command[1]}): {e}")

    def closeOpenItemsForRelease(self, nameWithOwner, version, message=None):
        """Comment on and close every open issue and PR. PRs are closed without merging.
        Returns (issueCount, prCount)."""
        if message is None:
            message = (
                f"Release {version} has been published. This will be closed.\n"
                f"Open a new issue or PR on the updated baseline to continue contributing."
            )
        issues, prs = self.openIssuesAndPRs(nameWithOwner)
        for issue in issues:
            n = str(issue['number'])
            self.progressMethod(f"Closing issue #{n}: {issue['title']}")
            self.gh(["issue", "comment", n, "--repo", nameWithOwner, "--body", message])
            self.gh(["issue", "close", n, "--repo", nameWithOwner])
        for pr in prs:
            n = str(pr['number'])
            self.progressMethod(f"Closing PR #{n}: {pr['title']}")
            self.gh(["pr", "comment", n, "--repo", nameWithOwner, "--body", message])
            self.gh(["pr", "close", n, "--repo", nameWithOwner])
        return len(issues), len(prs)
