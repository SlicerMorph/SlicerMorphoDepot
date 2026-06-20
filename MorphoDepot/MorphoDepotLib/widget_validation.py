"""Shared input-validation helpers used by both the Create and Release tabs.

Kept in a dedicated mixin (rather than living on one tab mixin and being called from another)
so the dependency is explicit and either tab can use them without an implicit cross-tab coupling.
"""


class ValidationMixin:
    def _segmentationIsEmpty(self, node):
        """True if the segmentation has no segments (nothing to release/credit)."""
        try:
            return node is not None and node.GetSegmentation().GetNumberOfSegments() == 0
        except Exception:
            return False

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
