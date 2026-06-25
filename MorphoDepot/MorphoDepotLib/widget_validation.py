"""Shared input-validation helpers used by both the Create and Release tabs.

Kept in a dedicated mixin (rather than living on one tab mixin and being called from another)
so the dependency is explicit and either tab can use them without an implicit cross-tab coupling.
"""

import logging


class ValidationMixin:
    def _segmentationIsEmpty(self, node):
        """True if the segmentation has no segments (nothing to release/credit)."""
        try:
            return node is not None and node.GetSegmentation().GetNumberOfSegments() == 0
        except Exception:
            return False

    def _gbifTaxonStatus(self, species):
        """Advisory check of a species name against the GBIF backbone taxonomy.

        Returns a short, human-readable sentence describing a discrepancy worth flagging
        (not found / matched only above species rank / a GBIF synonym / no exact match), or
        None when the name resolves cleanly to an accepted species -- OR when GBIF cannot be
        reached or its response cannot be parsed.  Those failures return None on purpose: an
        unverifiable name must never block or nag, because there are legitimate reasons a valid
        name is absent from GBIF (a recent reclassification, a newly described species, indexing
        lag).  Accepts both pygbif response shapes (flat pre-0.6, nested >= 0.6)."""
        species = (species or "").strip()
        if not species:
            return None
        try:
            import pygbif
            result = pygbif.species.name_backbone(species)
        except Exception as e:
            logging.warning(f"GBIF taxon check skipped for '{species}': {e}")
            return None
        if not isinstance(result, dict):
            return None
        diagnostics = result.get("diagnostics") or {}
        usage = result.get("usage") or {}
        accepted = result.get("acceptedUsage") or {}
        matchType = diagnostics.get("matchType") or result.get("matchType") or "NONE"
        canonical = usage.get("canonicalName") or usage.get("name") or result.get("canonicalName") or ""
        rank = usage.get("rank") or result.get("rank") or ""
        isSynonym = bool(result.get("synonym"))
        acceptedName = accepted.get("canonicalName") or accepted.get("name") or canonical
        if matchType == "NONE":
            return f"'{species}' was not found in the GBIF backbone taxonomy."
        if rank and rank != "SPECIES":
            return f"GBIF could match '{species}' only to {rank.lower()} '{canonical}', not to a species."
        if isSynonym and acceptedName:
            return f"GBIF lists '{species}' as a synonym of the accepted name '{acceptedName}'."
        if canonical and canonical.strip().lower() != species.lower():
            return f"GBIF has no exact match for '{species}'. Its closest accepted name is '{canonical}'."
        return None

    def _colorTableNotTerminology(self, node):
        """True if the color node does not look like a discrete terminology color table -- e.g. a
        built-in continuous colormap (Rainbow/Grey/...) or generic Labels.  A real MorphoDepot color
        table is loaded from a file ('File') or built by the user ('UserDefined' -- note: NOT 'User',
        which is what a User-type node's GetTypeAsString() actually returns).  False on any error.
        (Terminology presence alone does not discriminate -- even File anatomy tables can report no
        per-entry terminology -- so this keys on the source type.)"""
        try:
            return node is not None and node.GetTypeAsString() not in ("File", "UserDefined")
        except Exception:
            return False
