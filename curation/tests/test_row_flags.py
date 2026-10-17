"""Tests for row-level flags and curator triage tiers.

The design risk here is the opposite of the extractor's. The extractor can fail
by reading too little; this module can fail by *acting* on what it reads. Every
property pinned below is therefore about restraint:

1. **Nothing is ever dropped, reordered or rewritten.** Flagging adds two fields
   and touches nothing else. If this ever stops being true, correct records start
   disappearing silently, which is the one failure mode with no visible symptom.
2. **An unknown host is never treated as a wrong host.** Astur gentilis,
   Agelaioides badius and Casmerodius albus are real birds MalAvi does not hold.
   A fail-closed host rule would delete them, so the flag must stay advisory.
3. **A paper never confirms itself.** Every paper in the ground-truth corpus is
   already in MalAvi; without excluding a paper's own reference, its entire
   contribution reads as "MalAvi already has this".
4. **A blanket paper-level label does not fill the review queue.** Fecchio et al
   2023b declares itself a compilation, which marks all 2,683 of its rows
   uncertain. A trigger that selects every row selects nothing.
5. **An empty release index is safe.** A checkout without the release tables
   raises fewer flags; it never errors and never mis-tiers into ``new``.
"""
from __future__ import annotations

import copy

from malavi_curation.release_index import ReleaseIndex
from malavi_curation.row_flags import (
    FLAGS, TIERS, flag_row, flag_rows, rows_in_tier, tier_of,
)


# --- Test doubles ------------------------------------------------------------

def make_index() -> ReleaseIndex:
    """A small stand-in for the release, built from the same row shapes it has.

    Using the real constructor (rather than stubbing the class) keeps these
    tests honest about the normalization the real index applies: SPECIES_NAME
    repeats the genus in the release, and these rows do too.
    """
    host_rows = [
        # GRW04 in the great reed warbler, credited to two references.
        {"LINEAGE_NAME": "GRW04", "SPECIES_NAME": "Acrocephalus Acrocephalus arundinaceus",
         "COUNTRY_NAME": "Sweden", "REFERENCE_NAME": "Bensch et al 2000"},
        {"LINEAGE_NAME": "GRW04", "SPECIES_NAME": "Acrocephalus Acrocephalus arundinaceus",
         "COUNTRY_NAME": "Nigeria", "REFERENCE_NAME": "Hellgren 2005"},
        # A pair held by one reference only -- the self-confirmation case.
        {"LINEAGE_NAME": "SGS1", "SPECIES_NAME": "Passer Passer domesticus",
         "COUNTRY_NAME": "Sweden", "REFERENCE_NAME": "Palinauskas et al 2008"},
    ]
    lineage_rows = [
        {"LINEAGE_NAME": "GRW04", "GENBANK_ACC": "AF254975"},
        {"LINEAGE_NAME": "SGS1", "GENBANK_ACC": "AF495571"},
        # A lineage MalAvi holds with no host row of its own.
        {"LINEAGE_NAME": "CALMIN01", "GENBANK_ACC": "MG726162"},
    ]
    return ReleaseIndex(host_rows=host_rows, lineage_rows=lineage_rows)


def empty_index() -> ReleaseIndex:
    """The index a checkout without the release CSVs produces."""
    return ReleaseIndex(host_rows=[], lineage_rows=[])


def codes(flags) -> set:
    return {f.code for f in flags}


# --- 1. Flagging is purely additive -----------------------------------------

def test_flagging_adds_two_fields_and_changes_nothing_else():
    """The row that comes out is the row that went in, plus flags and tier."""
    rows = [
        {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
         "country": "Sweden", "number_tested": "10", "number_found": "2"},
        {"lineage_name": "SGS1", "host_species": "Passer domesticus",
         "country": "Sweden"},
    ]
    before = copy.deepcopy(rows)

    flag_rows(rows, index=make_index())

    assert len(rows) == len(before), "flagging must never add or remove rows"
    for original, flagged in zip(before, rows):
        assert set(flagged) - set(original) == {"flags", "tier"}
        for key, value in original.items():
            assert flagged[key] == value, f"flagging rewrote {key!r}"


def test_no_row_is_ever_dropped_however_broken():
    """A row with nothing usable on it still comes back, tiered as incomplete."""
    rows = [{}, {"lineage_name": None, "host_species": ""}, {"country": "Peru"}]

    summary = flag_rows(rows, index=make_index())

    assert len(rows) == 3
    assert all(row["tier"] == "incomplete" for row in rows)
    assert summary["tiers"]["incomplete"] == 3


# --- 2. An unknown host is information, never a rejection -------------------

def test_unknown_host_is_flagged_but_never_gates():
    """Astur gentilis is a real bird MalAvi does not hold: flag it, keep it."""
    row = {"lineage_name": "GRW04", "host_species": "Astur gentilis",
           "country": "Sweden"}

    flags = flag_row(row, make_index())

    assert "host_not_in_malavi" in codes(flags)
    # Advisory severity, and phrased as a question rather than a verdict.
    unknown = next(f for f in flags if f.code == "host_not_in_malavi")
    assert unknown.severity == "info"
    assert FLAGS["host_not_in_malavi"]["severity"] == "info"
    # It goes to a human, not to the bin.
    assert tier_of(flags) == "review"


def test_unknown_host_does_not_make_the_row_incomplete():
    """'MalAvi has never seen this bird' is not 'this row has no bird'."""
    row = {"lineage_name": "GRW04", "host_species": "Casmerodius albus"}

    assert tier_of(flag_row(row, make_index())) != "incomplete"


# --- 3. A paper never confirms itself ---------------------------------------

def test_pair_held_only_by_this_paper_is_not_a_confirmation():
    """Excluding the paper's own reference leaves its own contribution 'new'."""
    row = {"lineage_name": "SGS1", "host_species": "Passer domesticus",
           "country": "Sweden"}

    flags = flag_row(row, make_index(), own_reference="Palinauskas et al 2008")

    assert "pair_already_in_malavi" not in codes(flags)
    assert tier_of(flags) == "new"


def test_pair_held_by_another_paper_still_confirms():
    """Dropping our own reference must not hide everybody else's."""
    row = {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
           "country": "Sweden"}

    flags = flag_row(row, make_index(), own_reference="Bensch et al 2000")

    assert "pair_already_in_malavi" in codes(flags)
    assert tier_of(flags) == "confirms"
    # The curator is told who holds it, because that is how they judge it.
    held = next(f for f in flags if f.code == "pair_already_in_malavi")
    assert "Hellgren 2005" in (held.detail or "")


def test_without_own_reference_every_holder_is_reported():
    """Real intake curates a paper MalAvi does not have, so nothing is excluded."""
    row = {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus"}

    flags = flag_row(row, make_index())

    held = next(f for f in flags if f.code == "pair_already_in_malavi")
    assert "Bensch et al 2000" in (held.detail or "")
    assert "Hellgren 2005" in (held.detail or "")


# --- 4. A blanket label does not fill the review queue ----------------------

def test_compilation_label_alone_does_not_send_a_row_to_review():
    """scope_uncertain fires on every row of a compilation, so it cannot triage."""
    row = {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
           "country": "Sweden", "source_scope": "scope_uncertain"}

    flags = flag_row(row, make_index())

    # Still reported -- the curator must know the paper pools earlier studies.
    assert "source_uncertain" in codes(flags)
    # But the row is triaged on its own evidence, not on the blanket label.
    assert tier_of(flags) == "confirms"


def test_row_level_reprint_evidence_does_send_a_row_to_review():
    """'reprinted' comes from this row's own accession, so it is actionable."""
    row = {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
           "country": "Sweden", "source_scope": "reprinted",
           "source_scope_evidence": "sequences were retrieved from GenBank"}

    flags = flag_row(row, make_index())

    assert "source_reprinted" in codes(flags)
    assert tier_of(flags) == "review"


# --- 5. An empty index is safe ----------------------------------------------

def test_empty_index_raises_no_malavi_flags_and_never_errors():
    """A checkout without the release tables degrades quietly."""
    row = {"lineage_name": "NOTALINEAGE99", "host_species": "Invented bird",
           "country": "Atlantis"}

    flags = flag_row(row, empty_index())

    assert not (codes(flags) & {"lineage_not_in_malavi", "host_not_in_malavi",
                                "country_not_in_malavi", "pair_already_in_malavi"})
    # It is complete, so it is not "incomplete"; with no index to consult there
    # is nothing to review it against.
    assert tier_of(flags) == "new"


# --- The individual checks --------------------------------------------------

def test_accession_conflict_is_raised_not_resolved():
    """The row and MalAvi disagree; only a curator decides which is right."""
    row = {"lineage_name": "LAGLAG04", "host_species": "Calidris minutilla",
           "accession": "MG726162"}

    flags = flag_row(row, make_index())

    conflict = next(f for f in flags if f.code == "lineage_accession_conflict")
    assert conflict.severity == "warn"
    assert "CALMIN01" in conflict.message
    # The row keeps the name the paper printed; nothing is overwritten.
    assert row["lineage_name"] == "LAGLAG04"
    assert tier_of(flags) == "review"


def test_accession_agreeing_with_the_lineage_raises_nothing():
    row = {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
           "accession": "AF254975.1"}   # version suffix must not defeat the match

    assert "lineage_accession_conflict" not in codes(flag_row(row, make_index()))


def test_unresolved_lineage_is_reported_once_not_twice():
    """A study-local name is unresolved, not additionally 'not in MalAvi'."""
    row = {"lineage_name": "T009", "host_species": "Passer domesticus",
           "lineage_resolution": "novel",
           "lineage_resolution_note": "not identical to any MalAvi lineage"}

    flags = flag_row(row, make_index())

    assert "lineage_unresolved" in codes(flags)
    assert "lineage_not_in_malavi" not in codes(flags)
    assert tier_of(flags) == "review"


def test_resolved_lineage_is_not_flagged_as_unresolved():
    row = {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
           "lineage_resolution": "resolved", "lineage_name_source": "T009"}

    assert "lineage_unresolved" not in codes(flag_row(row, make_index()))


def test_prevalence_inconsistency_is_caught():
    row = {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
           "number_tested": 5, "number_found": 9}

    flags = flag_row(row, make_index())

    assert "prevalence_inconsistent" in codes(flags)
    assert tier_of(flags) == "review"


def test_unparseable_counts_are_not_treated_as_an_error():
    """A supplement's 'n/a' in a count column is not a prevalence contradiction."""
    row = {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus",
           "number_tested": "n/a", "number_found": "12 (3)"}

    assert "prevalence_inconsistent" not in codes(flag_row(row, make_index()))


def test_missing_country_is_reported_but_does_not_demand_judgment():
    """Supplements routinely omit geography; that does not doubt the association."""
    row = {"lineage_name": "SGS1", "host_species": "Passer domesticus"}

    flags = flag_row(row, make_index(), own_reference="Palinauskas et al 2008")

    assert "country_missing" in codes(flags)
    assert tier_of(flags) == "new"


# --- Tiering and the summary ------------------------------------------------

def test_review_outranks_confirms():
    """A conflict is worth a curator's time whether or not the pair is novel."""
    row = {"lineage_name": "GRW04", "host_species": "Astur gentilis"}

    assert tier_of(flag_row(row, make_index())) == "review"


def test_incomplete_outranks_everything():
    """A row missing half the association must not clutter the review queue."""
    row = {"lineage_name": "T009", "host_species": None,
           "lineage_resolution": "novel", "source_scope": "reprinted"}

    assert tier_of(flag_row(row, make_index())) == "incomplete"


def test_summary_counts_every_tier_and_flag():
    rows = [
        {"lineage_name": "SGS1", "host_species": "Passer domesticus"},
        {"lineage_name": "GRW04", "host_species": "Acrocephalus arundinaceus"},
        {"lineage_name": "GRW04", "host_species": "Astur gentilis"},
        {"host_species": "Passer domesticus"},
    ]

    summary = flag_rows(rows, index=make_index(),
                        own_reference="Palinauskas et al 2008")

    assert summary["tiers"] == {"new": 1, "confirms": 1, "review": 1, "incomplete": 1}
    assert summary["flags"]["host_not_in_malavi"] == 1
    assert summary["release_index_available"] is True
    assert set(summary["tiers"]) == set(TIERS)


def test_rows_in_tier_selects_without_mutating():
    rows = [
        {"lineage_name": "GRW04", "host_species": "Astur gentilis"},
        {"lineage_name": "SGS1", "host_species": "Passer domesticus"},
    ]
    flag_rows(rows, index=make_index(), own_reference="Palinauskas et al 2008")

    queue = rows_in_tier(rows, "review")

    assert len(queue) == 1
    assert queue[0]["host_species"] == "Astur gentilis"
    assert len(rows) == 2


# --- Vector rows ------------------------------------------------------------

def test_a_complete_vector_row_is_not_incomplete():
    """A vector row's second half is vector_species, not host_species.

    Checking the wrong field made every vector row in a supplement look like it
    was missing its species.
    """
    row = {"lineage_name": "GRW04", "vector_species": "Culex pipiens",
           "country": "Sweden"}

    flags = flag_row(row, make_index(), kind="vectors")

    assert "host_missing" not in codes(flags)
    assert "vector_missing" not in codes(flags)
    assert tier_of(flags) != "incomplete"


def test_a_vector_row_without_a_vector_is_incomplete():
    row = {"lineage_name": "GRW04", "country": "Sweden"}

    flags = flag_row(row, make_index(), kind="vectors")

    assert "vector_missing" in codes(flags)
    assert tier_of(flags) == "incomplete"


def test_a_vector_is_never_checked_against_bird_taxonomy():
    """The index holds the host table, which says nothing about arthropods."""
    row = {"lineage_name": "GRW04", "vector_species": "Culex pipiens"}

    flags = flag_row(row, make_index(), kind="vectors")

    assert "host_not_in_malavi" not in codes(flags)
    # Nor can a host-table pair lookup mean anything for a vector row.
    assert "pair_already_in_malavi" not in codes(flags)


def test_vector_rows_still_get_the_lineage_side_checks():
    """Everything that does not depend on bird taxonomy still applies."""
    row = {"lineage_name": "LAGLAG04", "vector_species": "Culex pipiens",
           "accession": "MG726162"}

    flags = flag_row(row, make_index(), kind="vectors")

    assert "lineage_accession_conflict" in codes(flags)
    assert tier_of(flags) == "review"


def test_every_flag_code_has_an_entry_in_the_flags_table():
    """A flag with no documented severity/description would render as '?'."""
    rows = [
        {"lineage_name": "LAGLAG04", "host_species": "Astur gentilis",
         "accession": "MG726162", "country": "Atlantis",
         "source_scope": "reprinted", "number_tested": 1, "number_found": 5},
        {"lineage_name": "T009", "host_species": "Acrocephalus arundinaceus",
         "lineage_resolution": "ambiguous", "source_scope": "scope_uncertain"},
        {},
    ]
    flag_rows(rows, index=make_index())

    for row in rows:
        for flag in row["flags"]:
            assert flag["code"] in FLAGS
            assert flag["severity"] == FLAGS[flag["code"]]["severity"]
