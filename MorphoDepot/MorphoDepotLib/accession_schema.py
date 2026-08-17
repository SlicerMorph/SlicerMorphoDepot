"""Accession questionnaire schema — PURE DATA, no Slicer/Qt imports.

Single source of truth for the accession questionnaire, shared by two
renderers:
  * the Qt form in ``accession_form.py`` (this extension), and
  * the MorphoDepot deposit portal's web form (deposit.morphodepot.org),
    which imports this module directly from this repository.

Because the second consumer lives outside Slicer, this module must stay
importable by any plain Python: data literals and stdlib only.

``FORM_QUESTIONS`` and ``SECTION_TITLES`` are the same structures that
previously lived as class attributes of ``MorphoDepotAccessionForm``
(each question is a tuple of question text, answer options, tooltip;
an empty-string options value means a free-text answer).

``SECTION_LAYOUT``, ``VISIBILITY_RULES`` and ``REQUIRED_RULES`` describe
the form structure declaratively for non-Qt renderers.  The Qt form's
``validateForm`` remains the reference implementation of the same rules;
if you change one, change both.
"""

SECTION_TITLES = {
    0: "Subject Type",
    1: "Acquisition type",
    2: "Accessioned specimen",
    3: "Species information",
    4: "Image data description",
    "4a": "Subject Description",
    5: "Partial specimen",
    6: "Licensing",
    7: "Github",
}

SECTION_ORDER = [0, 1, 2, 3, 4, "4a", 5, 6, 7]

FORM_QUESTIONS = {
    # section 4a
    "otherSubjectDescription": (
        "Please describe the subject of the data.",
        "",
        "Provide a description for this non-biological subject."
    ),
    # section 0
    "subjectType": (
        "What is the subject type?",
        ["Biological specimen", "Other"],
        "Select the type of subject for this data."
    ),
    # section 1
    "specimenSource": (
        "Is your data from a commercially acquired organism or from an accessioned specimen (i.e., from a natural history collection)?",
        ["Non-accessioned", "Accessioned specimen"],
        ""
    ),
    # section 2
    "iDigBioAccessioned": (
        "Is your specimen accessioned in a public database or repository (e.g. GBIF, iDigBio, Arctos)?",
        ["Yes", "No"],
        ""
    ),
    "iDigBioURL": (
        "Paste the record URL:",
        "",
        "Paste the URL of this specimen's record in a public database. For example:\n"
        "  GBIF:     https://www.gbif.org/occurrence/1702720653\n"
        "  iDigBio:  https://portal.idigbio.org/portal/records/<record-uuid>\n"
        "  Arctos:   https://arctos.database.museum/guid/UWBM:Mamm:82522"
    ),
    # section 3
    "species": (
        "What is your specimen's species?",
        "",
        "Enter a valid genus and species for your specimen.  If unsure, use the 'Search taxon in GBIF' button to look it up and pick the correct name."
    ),
    "biologicalSex": (
        "What is your specimen's sex?",
        ["Male", "Female", "Unknown"],
        ""
    ),
    "developmentalStage": (
        "What is your specimen's developmental stage?",
        ["Prenatal (fetus, embryo)", "Juvenile (neonatal to subadult)", "Adult"],
        ""
    ),
    # section 4
    "modality": (
        "What is the modality of the acquisition?",
        ["Micro CT (or synchrotron)", "Medical CT", "MRI", "Lightsheet microscopy", "3D confocal microscopy", "Surface model (photogrammetry, structured light, or laser scanning)"],
        ""
    ),
    "contrastEnhancement": (
        "Is there contrast enhancement treatment applied to the specimen?",
        ["Yes", "No"],
        ""
    ),
    "imageContents": (
        "What is in the image?",
        ["Whole specimen", "Partial specimen"],
        ""
    ),
    # section 5
    "anatomicalAreas": (
        "What anatomical area(s) is/are present in the scan?",
        ["Head and neck (e.g., cranium, mandible, proximal vertebral colum)", "Pectoral girdle", "Forelimb", "Trunk (e.g. body cavity, torso, spine, ribs)", "Pelvic girdle", "Hind limb", "Tail", "Other"],
        ""
    ),
    # section 6
    "redistributionAcknowledgement": (
        "Acknowledgement:",
        ["I have the right to allow redistribution of this data."],
        ""
    ),
    "license": (
        "Choose a license:",
        ["CC BY 4.0 (requires attribution, allows commercial usage)", "CC BY-NC 4.0 (requires attribution, non-commercial usage only)"],
        ""
    ),
    # section 7
    "githubRepoName": (
        "What should the repository in your github account called? This needs to be unique value for your account.",
        "",
        "Name should be fairly short and contain only letters, numbers, and the dash, underscore, or dot characters."
    ),
    "repoType": (
        "What is the intended lifespan of this repository?",
        ["Archival (intended for long-term maintenance)", "Short-term (e.g. repositories for classroom exercises, that are not meant to be maintained for long-term)"],
        ""
    ),
}

# Ordered (question key, widget type) per section.  Widget types:
# radio | text | checkboxes | species (a text field with GBIF lookup).
SECTION_LAYOUT = {
    0: [("subjectType", "radio")],
    1: [("specimenSource", "radio")],
    2: [("iDigBioAccessioned", "radio"), ("iDigBioURL", "text")],
    3: [("species", "species"), ("biologicalSex", "radio"),
        ("developmentalStage", "radio")],
    4: [("modality", "radio"), ("contrastEnhancement", "radio"),
        ("imageContents", "radio")],
    "4a": [("otherSubjectDescription", "text")],
    5: [("anatomicalAreas", "checkboxes")],
    6: [("redistributionAcknowledgement", "checkboxes"),
        ("license", "radio")],
    7: [("githubRepoName", "text"), ("repoType", "radio")],
}

DEFAULTS = {
    # the Qt form pre-checks the first license option
    "license": "CC BY 4.0 (requires attribution, allows commercial usage)",
    # it is the depositor's own data -- they know whether they may share it
    "redistributionAcknowledgement":
        ["I have the right to allow redistribution of this data."],
}

# Declarative mirror of accession_form.validateForm's visibility logic.
# A section/question is shown when ALL its conditions hold; conditions
# reference other answers as {question key: required answer}.
# CONTRACT: question-level rules are the FULL preconditions for that
# question (they repeat any parent-section conditions), so a renderer may
# evaluate them independently; questions WITHOUT a rule inherit their
# section's visibility.
VISIBILITY_RULES = {
    "sections": {
        1: {"subjectType": "Biological specimen"},
        2: {"subjectType": "Biological specimen",
            "specimenSource": "Accessioned specimen"},
        3: {"subjectType": "Biological specimen"},
        "4a": {"subjectType": "Other"},
        5: {"subjectType": "Biological specimen",
            "imageContents": "Partial specimen"},
    },
    "questions": {
        "contrastEnhancement": {"subjectType": "Biological specimen"},
        "imageContents": {"subjectType": "Biological specimen"},
        "iDigBioURL": {"subjectType": "Biological specimen",
                       "specimenSource": "Accessioned specimen",
                       "iDigBioAccessioned": "Yes"},
    },
}

# Declarative mirror of validateForm's required-field logic.  A visible
# question listed here must be answered; extra entries:
#   species          -- must be exactly two words (genus species)
#   iDigBioURL       -- captured but NEVER gates submission
#   redistributionAcknowledgement -- required only when repoType is Archival
REQUIRED_RULES = {
    "always": ["subjectType", "modality", "license", "githubRepoName",
               "repoType"],
    "when_visible": ["specimenSource", "species", "biologicalSex",
                     "developmentalStage", "contrastEnhancement",
                     "imageContents", "anatomicalAreas",
                     "otherSubjectDescription"],
    "never": ["iDigBioURL", "iDigBioAccessioned"],
    "species_two_words": True,
    "redistribution_required_when_repoType_prefix": "Archival",
}

# Repo-name suggestion inputs (see accession_form.suggestedRepoName).
MODALITY_SLUGS = {
    "Micro CT (or synchrotron)": "microct",
    "Medical CT": "medicalct",
    "MRI": "mri",
    "Lightsheet microscopy": "lightsheet",
    "3D confocal microscopy": "confocal",
    "Surface model (photogrammetry, structured light, or laser scanning)": "surface",
}
CONTENTS_SLUGS = {"Whole specimen": "whole", "Partial specimen": "partial"}

# Safe repo-name pattern (review S2: rejects '.', '..' and path tricks).
REPO_NAME_REGEX = r"^(?:([a-zA-Z\d]+(?:-[a-zA-Z\d]+)*)/)?((?!\.\.?$)[\w.-]+)$"
