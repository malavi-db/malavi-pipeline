"""Tests for source-scope classification (this paper's data vs somebody else's).

The properties that matter here are mostly negative, because the whole design
risk of this module is over-claiming. Calling a paper's own record "reprinted"
would push a correct record out of the curator's focal set, and calling a
compilation's pooled rows "focal" would credit six earlier studies' data to the
compiler. So the tests pin, in order of importance:

1. **Silence by default.** A paper that says nothing about provenance leaves
   every row ``unknown``, i.e. the pipeline behaves exactly as it did before this
   module existed.
2. **No declaration is inferred from accessions alone.** A sentence full of
   accessions that says the sequences were *retrieved* from GenBank is the
   inverse of a deposition statement and must not become one.
3. **Ranges are expanded**, because papers declare a block of new deposits as
   ``PV839571-PV839588`` and supplementary tables then cite the interior numbers.
4. **Nothing is ever dropped** -- classification only adds fields.

The two worked examples are the real ones from the ground-truth corpus, quoted
verbatim: Harl et al 2026's data-availability statement and Fecchio et al 2023b's
dataset-assembly sentence.
"""
from __future__ import annotations

from malavi_curation.source_scope import (
    assess, classify_rows, detect_pooled_compilation, focal_rows,
    parse_declared_accessions, scope_of, sentences, split_accession_cell,
)

# --- The corpus's two real statements ---------------------------------------

# Harl et al 2026, "Data availability". Declares two ranges: 47 cytb accessions
# and the 18 mitochondrial-genome accessions that Additional file 2 cites.
HARL_DECLARATION = (
    "Data availability The sequences generated for the present study were "
    "uploaded to NCBI GenBank under the accession numbers PV872084-PV872130 "
    "(cytb) and PV839571-PV839588 (mitochondrial genomes)."
)

# Fecchio et al 2023b, Methods. Says the dataset pools six earlier studies, and
# says nothing that separates its own 962 new samples from the rest.
FECCHIO_COMPILATION = (
    "We gathered geolocation and date for each bird screened for haemosporidian "
    "parasites from [9, 18, 24, 29-31]. We also included the results of screening "
    "962 unpublished samples from Alaska and Brazilian Amazonia."
)


# --- Sentence splitting ------------------------------------------------------

def test_sentences_keeps_abbreviations_intact():
    """"et al." must not end a sentence, or a declaration gets cut from its numbers."""
    text = "Sequenced by Harl et al. 2026. The data were deposited under PV839571."
    assert sentences(text) == [
        "Sequenced by Harl et al. 2026.",
        "The data were deposited under PV839571.",
    ]


# --- Declared accessions -----------------------------------------------------

def test_declaration_expands_ranges():
    """Both of Harl's declared ranges are expanded to their interior accessions."""
    declared = parse_declared_accessions(HARL_DECLARATION)
    assert declared                                     # truthy only when usable
    # 47 cytb (PV872084..PV872130) + 18 mitogenomes (PV839571..PV839588).
    assert len(declared.accessions) == 47 + 18
    # Endpoints and an interior accession Additional file 2 actually cites.
    assert "PV839571" in declared.accessions
    assert "PV839588" in declared.accessions
    assert "PV839577" in declared.accessions
    assert "PV872084" in declared.accessions
    # An accession one step outside either range is not declared.
    assert "PV839570" not in declared.accessions
    assert "PV839589" not in declared.accessions
    assert declared.evidence and "present study" in declared.evidence[0]


def test_retrieval_sentence_is_not_a_declaration():
    """The inverse statement must not be read as the paper's own deposition."""
    text = ("Reference sequences were retrieved from GenBank under accession "
            "numbers AB250415, KY653770 and KY653792.")
    assert not parse_declared_accessions(text)


def test_deposition_without_ownership_is_not_a_declaration():
    """Somebody else's deposition is still somebody else's."""
    text = ("These genomes were deposited in GenBank under accession numbers "
            "AB250415-AB250420 by earlier authors.")
    assert not parse_declared_accessions(text)


def test_declaration_without_accessions_yields_nothing():
    """Fecchio's pointer-to-a-table statement declares nothing usable."""
    text = ("All parasite sequences generated in this study are deposited in "
            "GenBank, and accession numbers can be found in the Supporting "
            "Information Table S1.")
    assert not parse_declared_accessions(text)


# --- Pooled compilations -----------------------------------------------------

def test_compilation_detected_from_fecchio_sentence():
    compilation = detect_pooled_compilation(FECCHIO_COMPILATION)
    assert compilation.is_compilation
    assert "gathered" in compilation.evidence[0]


def test_compilation_needs_a_source_marker():
    """Compiling climate layers is not a claim about data provenance."""
    text = "We gathered data for each sampling point using a 30 arc-second grid."
    assert not detect_pooled_compilation(text).is_compilation


def test_ordinary_paper_declares_nothing():
    """A methods paragraph with no provenance statement leaves both signals off."""
    text = ("Blood samples were collected from 171 raptors in Austria between "
            "2015 and 2023. DNA was extracted using a commercial kit and screened "
            "by nested PCR targeting the cytochrome b gene.")
    scope = assess(text)
    assert not scope.declared
    assert not scope.compilation.is_compilation


# --- Accession cells ---------------------------------------------------------

def test_split_accession_cell_reads_compound_and_versioned_cells():
    # Harl writes a two-record genome as a single cell.
    assert split_accession_cell("PV839574_PV839575") == ["PV839574", "PV839575"]
    # Version suffixes are dropped so a table cell matches a declared range.
    assert split_accession_cell("PV839571.1") == ["PV839571"]
    # Non-accessions are not invented into accessions.
    assert split_accession_cell("n/a") == []
    assert split_accession_cell(None) == []
    # A voucher-shaped token with no real INSDC prefix is rejected.
    assert split_accession_cell("GM 001") == []


# --- Row classification ------------------------------------------------------

def test_rows_are_classified_against_the_declaration():
    """Harl's case: a declared accession is focal, an undeclared one is reprinted."""
    rows = [
        {"lineage_name": "CIAE08", "host_species": "Buteo buteo",
         "accession": "PV839577"},                      # inside the declared range
        {"lineage_name": "GALLUS02", "host_species": "Gallus domesticus",
         "accession": "AB250415"},                      # a previously published genome
        {"lineage_name": "CIAE11", "host_species": "Astur gentilis",
         "accession": "PV839574_PV839575"},             # compound cell, both declared
        {"lineage_name": "BT7", "host_species": "Buteo buteo"},   # no accession at all
    ]
    tally = classify_rows(rows, assess(HARL_DECLARATION))

    assert tally == {"focal": 2, "reprinted": 1, "unknown": 1}
    assert [scope_of(r) for r in rows] == ["focal", "reprinted", "focal", "unknown"]
    # The reprinted row carries the sentence that justifies the call.
    assert "present study" in rows[1]["source_scope_evidence"]


def test_compilation_rows_are_uncertain_not_reprinted():
    """Fecchio's case: pooled, but no row says which study it came from.

    ``scope_uncertain`` rather than ``reprinted`` is the point of this test. The
    paper contributed 962 genuinely new samples that no column distinguishes, so
    claiming the rows are somebody else's would be as wrong as claiming they are
    the paper's own.
    """
    rows = [{"lineage_name": "CYACYA05", "host_species": "Cyanocompsa cyanoides",
             "accession": "KU562119"}]
    tally = classify_rows(rows, assess(FECCHIO_COMPILATION))

    assert tally == {"scope_uncertain": 1}
    # And they survive the focal filter, because nobody can tell.
    assert focal_rows(rows) == rows


def test_silent_paper_leaves_every_row_unknown():
    """No provenance evidence means no claim, and no change in behavior."""
    rows = [{"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
             "accession": "AF254975"}]
    tally = classify_rows(rows, assess("A short methods paragraph with no claims."))

    assert tally == {"unknown": 1}
    assert focal_rows(rows) == rows


def test_classification_never_drops_or_overwrites_rows():
    """Classification only adds fields; the extracted values are untouched."""
    rows = [{"lineage_name": "GALLUS02", "host_species": "Gallus domesticus",
             "accession": "AB250415", "country": "Japan"}]
    before = {k: v for k, v in rows[0].items()}

    classify_rows(rows, assess(HARL_DECLARATION))

    assert len(rows) == 1
    for key, value in before.items():
        assert rows[0][key] == value


def test_focal_rows_drops_only_reprinted():
    rows = [{"accession": "PV839577"},      # focal
            {"accession": "AB250415"},      # reprinted
            {}]                             # unknown
    classify_rows(rows, assess(HARL_DECLARATION))

    kept = focal_rows(rows)
    assert len(kept) == 2
    assert all(scope_of(r) != "reprinted" for r in kept)
