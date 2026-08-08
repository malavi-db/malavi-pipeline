"""Tests for the pinned-release index.

Two things have to hold or every flag built on this index is wrong:

1. **Normalization matches the benchmark's.** The index and the scorer must agree
   on what counts as the same lineage and the same host, or the pipeline will
   flag rows the benchmark scores as correct. The release stores SPECIES_NAME
   with the genus repeated ('Pipile Pipile jacutinga'), so this is not cosmetic.
2. **A missing release degrades to empty, never to an error.** Row flags call
   this on every row; a checkout without the site downloads must raise fewer
   flags, not crash the intake.

The last test is a consistency check against the real release rather than a unit
test: the index's accession→lineage map is derived in Python from the same table
that ``curation/r/gate_reference.R`` derives ``db_snapshot.json`` from in R, so
the two must agree. They are independent implementations of one rule, which
makes disagreement a genuine signal.
"""
from __future__ import annotations

import json

import pytest

from malavi_curation.release_index import (
    ReleaseIndex, load_release_index, norm_country, norm_lineage, norm_species,
    pinned_release, table_dir,
)


# --- Normalization ----------------------------------------------------------

def test_norm_species_takes_the_binomial_from_the_repeated_genus_form():
    """The release repeats the genus inside SPECIES_NAME; both forms must agree."""
    assert norm_species("Pipile Pipile jacutinga") == "PIPILE JACUTINGA"
    assert norm_species("Pipile jacutinga") == "PIPILE JACUTINGA"


def test_norm_species_strips_subgenus_and_punctuation():
    assert norm_species("Parus (Cyanistes) caeruleus") == "PARUS CAERULEUS"
    assert norm_species("  Passer  domesticus  ") == "PASSER DOMESTICUS"


def test_norm_species_handles_a_single_token_without_crashing():
    assert norm_species("Passer") == "PASSER"
    assert norm_species("") == ""
    assert norm_species(None) == ""


def test_norm_lineage_strips_all_whitespace_and_upcases():
    assert norm_lineage(" grw 04 ") == "GRW04"
    assert norm_lineage(None) == ""


def test_norm_country_collapses_whitespace():
    assert norm_country("  United   States ") == "UNITED STATES"
    assert norm_country(None) == ""


# --- Construction and queries ----------------------------------------------

def small_index() -> ReleaseIndex:
    return ReleaseIndex(
        host_rows=[
            {"LINEAGE_NAME": "GRW04", "SPECIES_NAME": "Acrocephalus Acrocephalus arundinaceus",
             "COUNTRY_NAME": "Sweden", "REFERENCE_NAME": "Bensch et al 2000"},
            {"LINEAGE_NAME": "GRW04", "SPECIES_NAME": "Acrocephalus arundinaceus",
             "COUNTRY_NAME": "Nigeria", "REFERENCE_NAME": "Hellgren 2005"},
        ],
        lineage_rows=[
            {"LINEAGE_NAME": "GRW04", "GENBANK_ACC": "AF254975"},
            {"LINEAGE_NAME": "VECTORONLY01", "GENBANK_ACC": "AB123456"},
        ],
    )


def test_both_spellings_of_a_host_land_on_one_pair():
    """The repeated-genus and plain forms are the same association, not two."""
    index = small_index()

    references = index.references_for_pair("GRW04", "Acrocephalus arundinaceus")

    assert references == {"Bensch et al 2000", "Hellgren 2005"}
    assert len(index.pair_references) == 1


def test_a_lineage_known_only_from_the_summary_is_still_known():
    """A lineage with no host row (vector-only, or newly deposited) is not new."""
    index = small_index()

    assert index.knows_lineage("VECTORONLY01")
    assert not index.knows_host("Nonexistent bird")


def test_accession_lookup_is_version_agnostic():
    index = small_index()

    assert index.lineage_for_accession("AF254975") == "GRW04"
    assert index.lineage_for_accession("af254975.2") == "GRW04"
    assert index.lineage_for_accession("ZZ999999") is None
    assert index.lineage_for_accession(None) is None


def test_a_pair_needs_both_halves():
    index = small_index()

    assert index.references_for_pair("GRW04", None) == set()
    assert index.references_for_pair("", "Acrocephalus arundinaceus") == set()


def test_returned_reference_sets_are_copies():
    """A caller discarding its own reference must not mutate the shared index."""
    index = small_index()

    first = index.references_for_pair("GRW04", "Acrocephalus arundinaceus")
    first.discard("Hellgren 2005")

    assert index.references_for_pair("GRW04", "Acrocephalus arundinaceus") == {
        "Bensch et al 2000", "Hellgren 2005"}


def test_malformed_accession_cells_are_ignored_not_indexed():
    """Export artifacts in GENBANK_ACC must not become accessions."""
    index = ReleaseIndex(
        host_rows=[],
        lineage_rows=[{"LINEAGE_NAME": "X01", "GENBANK_ACC": "LINEAGE NAME\nnot-an-acc"}],
    )

    assert index.accession_to_lineage == {}
    assert index.knows_lineage("X01")


# --- The empty case ---------------------------------------------------------

def test_an_index_with_no_tables_is_empty_and_answers_everything_false():
    index = ReleaseIndex(host_rows=[], lineage_rows=[])

    assert index.is_empty
    assert not index.knows_lineage("GRW04")
    assert not index.knows_host("Passer domesticus")
    assert not index.knows_country("Sweden")
    assert index.references_for_pair("GRW04", "Passer domesticus") == set()
    assert index.lineage_for_accession("AF254975") is None


# --- Against the real release ----------------------------------------------

def test_the_real_release_loads_and_is_not_empty():
    """The repository ships the pinned release, so the index must be populated."""
    if pinned_release() is None:
        pytest.skip("no release CSVs in this checkout")

    index = load_release_index()

    assert not index.is_empty
    assert index.knows_lineage("GRW04")
    assert index.knows_host("Acrocephalus arundinaceus")


def test_index_agrees_with_the_r_generated_snapshot_on_accessions():
    """Two independent implementations of one rule must produce one answer.

    ``db_snapshot.json`` is built in R by curation/r/gate_reference.R; this index
    is built in Python from the same release table. A disagreement means one of
    them has drifted.
    """
    if pinned_release() is None:
        pytest.skip("no release CSVs in this checkout")

    snapshot_path = (table_dir().parents[3] / "curation" / "src" / "malavi_curation"
                     / "data" / "db_snapshot.json")
    if not snapshot_path.is_file():
        pytest.skip("db_snapshot.json has not been generated")

    snapshot = json.loads(snapshot_path.read_text())
    if snapshot.get("source_release") != pinned_release():
        pytest.skip("snapshot was built from a different release than the tables")

    index = load_release_index()

    assert index.accession_to_lineage == snapshot["accession_to_lineage"]
