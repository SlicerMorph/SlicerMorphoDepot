"""MorphoDepotLogic ObjectStoreMixin (split from MorphoDepot.py)."""
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


class ObjectStoreMixin:
    def resolveVolumeURL(self, volumeRef, repoNameWithOwner):
        """Convert a source_volume file reference to a full download URL.
        Accepts both legacy full URLs (backwards compatible) and new relative paths.
        A source_volume now holds an absolute object-store (JS2) URL; because it starts with
        "http" it passes straight through here unchanged — only the older relative
        "releases/download/v1/..." pointers are re-based onto the repo owner.
        """
        if volumeRef.startswith("http"):
            return volumeRef  # full URL (object-store or legacy hardcoded) — use as-is
        return f"https://github.com/{repoNameWithOwner}/{volumeRef}"

    def uploadSourceVolumeToObjectStore(self, sourceFilePath, sha256, creator, repo, filename):
        """Upload the source volume to the MorphoDepot object store (JS2) with a server-mediated
        S3 MULTIPART upload, and return the public URL of the stored object.

        The signing service holds the bucket credentials; the client never does.  The server
        runs create / complete / abort and signs each part URL on demand, so a slow upload never
        outlives a part URL (the client re-signs on a 403).  The client PUTs each chunk directly
        to S3 and verifies the returned part ETag.  On any failure the whole upload is aborted so
        no orphaned parts linger (a bucket lifecycle rule is the backstop).  The object is keyed
        {creator}/{repo}/{filename} with the volume's identity (sha256, creator, repo, original
        filename) stamped as immutable S3 user-metadata at create time; integrity is guaranteed
        end-to-end by the committed source_volume_checksum (SHA-256 of the whole file).  Endpoint
        + fallback token come from QSettings (MorphoDepot/uploadSignEndpoint,
        MorphoDepot/uploadSignToken).  See docs/ObjectStorage-model.md."""
        import requests
        qsettings = qt.QSettings()
        signEndpoint = qsettings.value("MorphoDepot/uploadSignEndpoint",
                                       "https://join.morphodepot.org/uploads/sign")
        # Derive the multipart base: …/uploads/sign -> …/uploads/multipart
        base = signEndpoint.rsplit("/uploads/", 1)[0] + "/uploads/multipart"
        # Authenticate with the user's own gh token (member tier — "git + gh, nothing else").
        # Falls back to a QSettings shared token only if gh is somehow unavailable.
        try:
            token = self._ghToken()
        except Exception:
            token = ""
        if not token:
            token = qsettings.value("MorphoDepot/uploadSignToken", "")
        authHeaders = {"Authorization": f"Bearer {token}"} if token else {}

        def post(path, body):
            r = requests.post(f"{base}/{path}", json=body, headers=authHeaders, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"Object-store multipart {path} failed "
                                   f"({r.status_code}): {r.text}")
            return r.json()

        size = os.path.getsize(sourceFilePath)
        self.progressMethod("Requesting multipart upload from the object-store signing service...")
        created = post("create", {"sha256": sha256, "size": size, "creator": creator,
                                  "repo": repo, "filename": filename})
        publicURL = created["public_url"]
        if created.get("already_exists"):
            self.progressMethod("Source volume already in the object store; skipping upload.")
            return publicURL

        key = created["key"]
        uploadId = created["upload_id"]
        partSize = int(created.get("part_size") or (128 * 2**20))
        parts = []
        try:
            with open(sourceFilePath, "rb") as fp:
                partNumber = 0
                uploaded = 0
                while True:
                    chunk = fp.read(partSize)
                    if not chunk:
                        break
                    partNumber += 1
                    etag = self._uploadOneVolumePart(post, key, uploadId, partNumber, chunk)
                    parts.append({"part_number": partNumber, "etag": etag})
                    uploaded += len(chunk)
                    self.progressMethod(f"Uploaded {uploaded / 2**20:.0f} / {size / 2**20:.0f} MB "
                                        "to the object store...")
            self.progressMethod("Finalizing multipart upload...")
            completed = post("complete", {"key": key, "upload_id": uploadId, "parts": parts})
            return completed.get("public_url", publicURL)
        except Exception:
            # Abort so a partial upload leaves no orphaned parts (lifecycle rule is the backstop).
            try:
                post("abort", {"key": key, "upload_id": uploadId})
                self.progressMethod("Upload failed — aborted the multipart upload (no orphaned data).")
            except Exception as abortError:
                logging.warning(f"Could not abort multipart upload {key}/{uploadId}: {abortError}")
            raise

    def _uploadOneVolumePart(self, post, key, uploadId, partNumber, chunk):
        """PUT one multipart chunk to a freshly-signed part URL, verify its ETag, and return it.
        Re-signs the URL (handling expiry) and retries on transient failure."""
        import requests
        import hashlib
        expectedMd5 = hashlib.md5(chunk).hexdigest()
        lastError = None
        for _attempt in range(4):
            # Send the exact chunk length so the signing service binds it into the part URL
            # (S3 then rejects a mismatched body) — part of the server-side per-file size cap.
            signed = post("sign", {"key": key, "upload_id": uploadId, "part_number": partNumber,
                                   "content_length": len(chunk)})
            try:
                resp = requests.put(signed["url"], data=chunk, timeout=None)
            except Exception as e:
                lastError = e
                continue
            if resp.status_code == 403:   # part URL expired — re-sign and retry
                lastError = RuntimeError("part URL expired (403)")
                continue
            if resp.status_code not in (200, 201):
                lastError = RuntimeError(f"part {partNumber} PUT failed "
                                         f"({resp.status_code}): {resp.text}")
                continue
            etag = (resp.headers.get("ETag") or "").strip()
            clean = etag.strip('"').lower()
            # For our unencrypted bucket the part ETag is the part's MD5 — verify when it looks
            # like one (skip for non-MD5 ETags; the whole-file SHA-256 still backstops on download).
            if len(clean) == 32 and all(c in "0123456789abcdef" for c in clean) and clean != expectedMd5:
                lastError = RuntimeError(f"part {partNumber} checksum mismatch")
                continue
            if not etag:
                lastError = RuntimeError(f"part {partNumber} returned no ETag")
                continue
            return etag
        raise RuntimeError(f"part {partNumber} failed after retries: {lastError}")

    # --- Membership tier + App control plane (member repos are born in-org, App-mediated) ---

    morphoDepotOrg = "MorphoDepot"

    # Throwaway org for the developer Reload-and-Test self-test.  Test repos are created here
    # directly (the developer's own gh rights — no App, no S3), never on a personal account or in
    # the production org, and are deleted at the end of the test.  Both dev accounts are members.
    morphoDepotTestingOrg = "MorphoDepotTesting"

    def volumeChecksumIndexURL(self):
        """RepoClerk's published checksum->repo index (GitHub Pages JSON)."""
        return qt.QSettings().value(
            "MorphoDepot/volumeChecksumIndexURL",
            "https://MorphoDepot.github.io/RepoClerk/volume-checksums.json")

    def duplicateVolumeRepos(self, checksum, exclude=None):
        """Repos (nameWithOwner) that already hold a volume with this SHA-256, per RepoClerk's
        published checksum->repo index.  Best-effort and ADVISORY: returns [] on any failure
        (network down, index missing/stale) so it never blocks staging or publishing.  The index
        lags RepoClerk's crawl (~6 h), so a very recently published duplicate may not appear yet.
        `exclude` drops the repo being created/published (a repo never duplicates itself)."""
        if not checksum:
            return []
        sha = checksum.strip()
        if ":" in sha:  # the committed file is "SHA256:<hex>"; the index stores bare hex
            sha = sha.split(":", 1)[1].strip()
        sha = sha.lower()
        if not sha:
            return []
        try:
            resp = requests.get(self.volumeChecksumIndexURL(), timeout=10)
            resp.raise_for_status()
            index = resp.json().get("checksums", {})
        except Exception as e:
            logging.warning(f"Duplicate-volume check skipped ({e})")
            return []
        return [r for r in index.get(sha, []) if r != exclude]
