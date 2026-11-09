"""Shared input-validation helpers used by both the Create and Release tabs.

Kept in a dedicated mixin (rather than living on one tab mixin and being called from another)
so the dependency is explicit and either tab can use them without an implicit cross-tab coupling.
"""

import logging

import slicer


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
            import socket
            # name_backbone() runs synchronously on the Qt UI thread and pygbif uses requests,
            # which has no default timeout -- so a slow/hung GBIF server would freeze Slicer.
            # Cap the wait with a temporary socket timeout (restored in finally).
            previousTimeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(8)
            try:
                result = pygbif.species.name_backbone(species)
            finally:
                socket.setdefaulttimeout(previousTimeout)
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
        # Only the real accepted name (never the synonym itself) -- so we don't emit the
        # self-referential "X is a synonym of X" when GBIF omits acceptedUsage.
        acceptedName = accepted.get("canonicalName") or accepted.get("name") or ""
        if matchType == "NONE":
            return f"'{species}' was not found in the GBIF backbone taxonomy."
        if rank and rank != "SPECIES":
            return f"GBIF could match '{species}' only to {rank.lower()} '{canonical}', not to a species."
        if isSynonym and acceptedName and acceptedName.strip().lower() != species.lower():
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

    def _isDefaultSlicerColorTable(self, colorNode):
        """True if colorNode is a built-in/default Slicer color table rather than a real terminology
        table.  Two cases: a procedural built-in (Labels/Grey/Rainbow/...), which the shared
        _colorTableNotTerminology already detects by source type ('UserDefined'/'File' are the real
        terminology types); or one of Slicer's shipped file-loaded color tables (GenericColors,
        GenericAnatomyColors, the colormaps, brain atlases, ...), which report Type 'File' like a user
        table and so are matched by name (UI #3a)."""
        if self._colorTableNotTerminology(colorNode):
            return True
        shippedDefaults = {
            "GenericColors", "GenericAnatomyColors", "AbdomenColors", "PelvisColor",
            "64Color-Nonsemantic", "Slicer3_2010_Brain_Labels", "Slicer3_2010_Label_Colors",
            "SPL-BrainAtlas-ColorFile", "SPL-BrainAtlas-2009-ColorFile", "SPL-BrainAtlas-2012-ColorFile",
            "Cividis", "Inferno", "Magma", "Plasma", "Viridis", "ColdToHotRainbow", "HotToColdRainbow",
            "DivergingBlueRed", "DarkBrightChartColors", "LightPaleChartColors", "MediumChartColors",
        }
        try:
            return colorNode.GetName() in shippedDefaults  # shipped file-loaded default
        except Exception:
            return False

    # --- #238: term-lookup color tables ------------------------------------------------------

    def _termLookupNames(self, segmentationNode=None, colorNode=None):
        """Names to pre-fill the term-lookup page with: the segments of `segmentationNode` if it has
        any, else the entries of `colorNode` -- unless that is one of Slicer's built-in tables, whose
        entries (jake, Peach, Brain, ...) are not the user's structures.  Empty, "(none)" and
        duplicate names are dropped."""
        names = []
        try:
            if segmentationNode is not None:
                segmentation = segmentationNode.GetSegmentation()
                names = [segmentation.GetNthSegment(i).GetName()
                         for i in range(segmentation.GetNumberOfSegments())]
            if not names and colorNode is not None and not self._isDefaultSlicerColorTable(colorNode):
                names = [colorNode.GetColorName(i) for i in range(1, colorNode.GetNumberOfColors())]
        except Exception as e:
            logging.warning(f"Could not collect names for the term lookup: {e}")
        result, seen = [], set()
        for name in names:
            name = (name or "").strip()
            if name and name != "(none)" and name.casefold() not in seen:
                seen.add(name.casefold())
                result.append(name)
        return result

    @staticmethod
    def _terminologyTypeFields(serialized):
        """(scheme, code, meaning) of the Type part of a serialized terminology entry -- the format
        shared by segment terminology tags and vtkMRMLColorNode.GetTerminologyAsString():
        context~category~type~typeModifier~regionContext~region~regionModifier, each coded entry
        written scheme^code^meaning.  None when there is no type code."""
        parts = (serialized or "").split("~")
        if len(parts) < 3:
            return None
        fields = (parts[2].split("^") + ["", "", ""])[:3]
        if not fields[1].strip():
            return None
        return fields[0].strip(), fields[1].strip(), fields[2].strip()

    @staticmethod
    def _segmentTerminology(segment):
        """The segment's serialized terminology entry, or "" (works with and without the
        vtkSegment.GetTerminology() convenience method)."""
        try:
            if hasattr(segment, "GetTerminology"):
                return segment.GetTerminology() or ""
            import vtk
            value = vtk.reference("")
            segment.GetTag(slicer.vtkSegment.GetTerminologyEntryTagName(), value)
            return str(value)
        except Exception:
            return ""

    def _baselineColorTableMismatches(self, segmentationNode, colorNode):
        """Where the baseline and the color table disagree: a segment whose name has no entry in the
        table, or whose term differs from that entry's term.  Names are compared ignoring case and
        surrounding spaces; terms by coding scheme (ignoring case) and code.  Table entries with no
        segment are fine -- a baseline may cover only some structures."""
        entries = {}
        for i in range(colorNode.GetNumberOfColors()):
            name = (colorNode.GetColorName(i) or "").strip()
            if name and name != "(none)":
                entries.setdefault(name.casefold(), i)
        problems = []
        segmentation = segmentationNode.GetSegmentation()
        for k in range(segmentation.GetNumberOfSegments()):
            segment = segmentation.GetNthSegment(k)
            name = (segment.GetName() or "").strip()
            index = entries.get(name.casefold())
            if index is None:
                problems.append(f"'{name}' has no entry with that name in the color table.")
                continue
            want = self._terminologyTypeFields(colorNode.GetTerminologyAsString(index))
            if want is None:
                continue  # the table entry carries no term, so the name is all there is to compare
            have = self._terminologyTypeFields(self._segmentTerminology(segment))
            if have is None:
                problems.append(f"'{name}' has no term; the color table's entry is '{want[2]}'.")
            elif (have[0].casefold(), have[1]) != (want[0].casefold(), want[1]):
                problems.append(f"'{name}' is labeled '{have[2]}', but the color table's entry is '{want[2]}'.")
        return problems

    def _confirmBaselineMatchesColorTable(self, segmentationNode, colorNode, organizational, action):
        """#238: a baseline must agree with its color table.  Organizational repositories are
        blocked until it does; Personal ones are warned and may continue.  `action` completes
        "can't be ... until" ("staged" / "released").  Returns True to proceed.  Never corrects
        the segments itself: which segment is which structure is the user's call."""
        try:
            problems = self._baselineColorTableMismatches(segmentationNode, colorNode)
        except Exception as e:
            logging.warning(f"Baseline/color table comparison skipped: {e}")
            return True
        if not problems:
            return True
        listed = "\n".join(f"• {p}" for p in problems[:12])
        if len(problems) > 12:
            listed += f"\n• …and {len(problems) - 12} more."
        intro = (f"The baseline segmentation '{segmentationNode.GetName()}' does not match the color "
                 f"table '{colorNode.GetName()}':")
        howToFix = ("To fix this, open the Segment Editor, double-click each segment listed above, and "
                    f"pick its entry from the color table '{colorNode.GetName()}'. That sets the "
                    "segment's name, color and term together.")
        title = "Baseline and color table don't match"
        if organizational:
            slicer.util.errorDisplay(
                f"{intro}\n\n{listed}\n\n{howToFix}\n\nOrganizational repositories can't be {action} "
                "until every segment matches the color table.", windowTitle=title)
            return False
        return self.testingMode or slicer.util.confirmOkCancelDisplay(
            f"{intro}\n\n{listed}\n\n{howToFix}\n\nClick OK to continue anyway, or Cancel to fix it first.",
            windowTitle=title)

    def _confirmColorTableSpecies(self, colorNode, species, nonBiological):
        """#238: warn (never block) when a table built with the term lookup was matched for a
        different specimen than the one now described -- terms that fit one species may not fit
        another.  Tables not built with the tool carry no record and pass silently."""
        from MorphoDepotLib.widget_termlookup import NON_BIOLOGICAL, SPECIES_ATTRIBUTE
        builtFor = (colorNode.GetAttribute(SPECIES_ATTRIBUTE) or "").strip() if colorNode else ""
        if not builtFor:
            return True
        current = NON_BIOLOGICAL if nonBiological else " ".join((species or "").split())
        if " ".join(builtFor.split()).casefold() == current.casefold():
            return True
        describe = lambda s: "a non-biological specimen" if s == NON_BIOLOGICAL else (f"'{s}'" if s else "no species")
        return self.testingMode or slicer.util.confirmOkCancelDisplay(
            f"The color table '{colorNode.GetName()}' was built for {describe(builtFor)}, but this "
            f"repository describes {describe(current)}. Terms that fit one species may not fit another.\n\n"
            "Click Cancel and use 'Build color table…' to rebuild it, or OK to continue with this table.",
            windowTitle="Color table built for a different specimen")
