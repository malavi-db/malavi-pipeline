"""Tests for sequence-based lineage resolution.

The property that matters most is the negative one, and it is the mirror of the
one ``test_sequence_check`` pins: a name may be assigned **only** on exact
identity over a substantial overlap. A sequence that differs from a known lineage
by one base, or that is identical to two lineages, or that is identical over too
short a stretch, must come back with no name at all. Assigning the wrong lineage
name to a real host association would put a false record into MalAvi, which is
worse than leaving the row for the curator.

The synthetic alignment below is 479 columns wide -- the real MalAvi frame -- so
the anchor-window index is genuinely exercised rather than falling through to the
brute-force path.
"""
from __future__ import annotations

import random

import pytest

from malavi_curation.lineage_resolve import (
    MIN_COMPARABLE_TO_ASSIGN, LineageResolver, lineage_name_of, resolve_rows,
)

WIDTH = 479

# A deterministic pseudo-random backbone. Random (rather than a repeated motif)
# matters: a periodic sequence would register equally well at several offsets.
_rng = random.Random(20260729)
BASE = "".join(_rng.choice("ACGT") for _ in range(WIDTH))


def _mutate(sequence: str, position: int) -> str:
    """Change one base, deterministically, to something else."""
    replacement = {"A": "C", "C": "A", "G": "T", "T": "G"}[sequence[position]]
    return sequence[:position] + replacement + sequence[position + 1:]


# LIN_ONE is the backbone; LIN_TWO differs from it at a single position inside
# every anchor window's reach; LIN_DUP is a second name carrying an identical
# sequence (MalAvi really does hold such pairs); LIN_PART is a partial deposit,
# padded with N outside the middle 300 columns.
LIN_ONE = BASE
LIN_TWO = _mutate(BASE, 200)
LIN_DUP = BASE
LIN_PART = "N" * 90 + BASE[90:390] + "N" * (WIDTH - 390)


@pytest.fixture
def resolver(tmp_path):
    path = tmp_path / "alignment.fasta"
    path.write_text(
        f">H_LINONE01_Haemoproteus_test\n{LIN_ONE}\n"
        f">P_LINTWO01\n{LIN_TWO}\n"
        f">L_LINPART01\n{LIN_PART}\n"
    )
    return LineageResolver.from_alignment(path)


# --------------------------------------------------------------------------
# header parsing
# --------------------------------------------------------------------------

def test_lineage_name_parsed_from_alignment_header():
    """The release names records <genus letter>_<lineage>[_<morphospecies>]."""
    assert lineage_name_of("H_COLBUC01_Haemoproteus_multipigmentatus") == "COLBUC01"
    assert lineage_name_of("P_MYRAXI01") == "MYRAXI01"
    assert lineage_name_of(">L_BUBT3") == "BUBT3"


# --------------------------------------------------------------------------
# the safety properties
# --------------------------------------------------------------------------

def test_one_base_difference_is_never_assigned_a_name(resolver):
    """A single mismatch means it is not that lineage. No name, no exception."""
    query = _mutate(LIN_ONE, 300)
    match = resolver.resolve(query, detail_on_miss=False)
    assert match.verdict == "novel"
    assert match.lineage_name is None


def test_two_lineages_with_the_same_sequence_stay_unresolved(tmp_path):
    """When the sequence cannot tell two names apart, neither is chosen."""
    path = tmp_path / "dup.fasta"
    path.write_text(f">H_LINONE01\n{LIN_ONE}\n>H_LINDUP01\n{LIN_DUP}\n")
    match = LineageResolver.from_alignment(path).resolve(LIN_ONE)
    assert match.verdict == "ambiguous"
    assert match.lineage_name is None
    assert match.matched == ["LINDUP01", "LINONE01"]


def test_short_exact_match_is_reported_but_not_assigned(tmp_path):
    """Identical over 150 positions is consistent with a lineage, not proof of it.

    A single-record alignment is used so the outcome can only be about the
    overlap: with more records a 150 bp fragment ties between every lineage that
    happens to agree over that stretch, which is the ``ambiguous`` case instead.
    """
    path = tmp_path / "one.fasta"
    path.write_text(f">H_LINONE01\n{LIN_ONE}\n")
    match = LineageResolver.from_alignment(path).resolve(LIN_ONE[:150],
                                                        detail_on_miss=False)
    assert match.verdict == "match_too_short"
    assert match.lineage_name is None
    assert match.comparable < MIN_COMPARABLE_TO_ASSIGN
    assert "LINONE01" in match.note


def test_a_fragment_that_cannot_tell_two_lineages_apart_is_ambiguous(resolver):
    """The other short-fragment outcome, and the more common one.

    ``LIN_TWO`` differs from ``LIN_ONE`` only at column 200, so a fragment of the
    first 150 columns is identical to both. Nothing in that fragment identifies
    either, and no name is assigned.
    """
    match = resolver.resolve(LIN_ONE[:150], detail_on_miss=False)
    assert match.verdict == "ambiguous"
    assert match.lineage_name is None
    assert "LINONE01" in match.matched and "LINTWO01" in match.matched


def test_unrelated_sequence_is_unplaceable_not_matched(resolver):
    """Something that is not a cytb barcode must not be forced onto the frame."""
    match = resolver.resolve("ACGT" * 120, detail_on_miss=False)
    assert match.verdict in ("unplaceable", "novel")
    assert match.lineage_name is None


# --------------------------------------------------------------------------
# resolution that should succeed
# --------------------------------------------------------------------------

def test_exact_match_resolves_to_the_lineage_name(resolver):
    match = resolver.resolve(LIN_ONE, detail_on_miss=False)
    assert match.verdict == "resolved"
    assert match.lineage_name == "LINONE01"
    assert match.comparable == WIDTH


def test_offset_sequence_still_resolves(resolver):
    """Deposits are routinely a base or two out of frame; registration fixes that.

    The offset convention is ``sequence_check``'s: query position 1 sits at frame
    position ``offset + 1``. A sequence missing its first two bases therefore
    registers at +2.
    """
    match = resolver.resolve(LIN_ONE[2:], detail_on_miss=False)
    assert match.verdict == "resolved"
    assert match.lineage_name == "LINONE01"
    assert match.offset == 2
    assert match.comparable == WIDTH - 2


def test_long_amplicon_containing_the_barcode_resolves(resolver):
    """McNew et al 2021 deposits 818 bp for some samples: the window is inside it.

    ``sequence_check``'s default 25 bp slide cannot find a window that starts 200
    bases into the query, so the resolver widens the search by the query's length.
    """
    query = "".join(_rng.choice("ACGT") for _ in range(200)) + LIN_ONE
    match = resolver.resolve(query, detail_on_miss=False)
    assert match.verdict == "resolved"
    assert match.lineage_name == "LINONE01"
    assert match.offset == -200


def test_full_length_match_wins_over_a_shorter_partial_deposit(resolver):
    """A partial deposit identical as far as it goes is not a competing name.

    About a fifth of the release is a partial sequence, so this is the common
    case, not an edge one: McNew's T025 matches BAEBIC02 over 478 positions and
    two shorter deposits over 367 and 285. The best-covered match is the
    identification; the others are reported.
    """
    match = resolver.resolve(LIN_ONE, detail_on_miss=False)
    assert match.lineage_name == "LINONE01"
    assert [name for name, _ in match.also_consistent] == ["LINPART01"]
    assert "LINPART01" in match.note


def test_index_prefilter_finds_matches_the_brute_force_scan_would(resolver):
    """The anchor index must be exhaustive for exact matches.

    A reference that is ambiguous inside the anchor windows (a partial deposit) can
    never be found by fragment equality, so it has to be a candidate every time.
    Resolving the partial deposit's own sequence proves it is reachable.
    """
    match = resolver.resolve(LIN_PART, detail_on_miss=False)
    # The partial deposit is N outside its middle 300 columns, so it is identical
    # to LINONE01 over exactly the same 300 positions it matches itself over: a
    # true tie, correctly left unresolved. What this pins is that LINPART01 is
    # found at all -- it can only have come from the per-window ambiguous list,
    # because a record padded with N is never indexed by fragment equality.
    assert "LINPART01" in match.matched
    assert match.verdict == "ambiguous"
    assert match.lineage_name is None


# --------------------------------------------------------------------------
# applying resolution to extracted rows
# --------------------------------------------------------------------------

def test_resolve_rows_replaces_a_study_local_name_and_keeps_the_original(resolver):
    """The curator must be able to see that the paper said "T001"."""
    rows = [{"lineage_name": "T001", "host_species": "Columbina talpacoti",
             "sequence": LIN_ONE}]
    tally = resolve_rows(rows, resolver, known_lineages=frozenset({"LINONE01"}))
    assert rows[0]["lineage_name"] == "LINONE01"
    assert rows[0]["lineage_name_source"] == "T001"
    assert rows[0]["lineage_resolution"] == "resolved"
    assert tally == {"resolved": 1}


def test_resolve_rows_leaves_a_name_malavi_already_knows(resolver):
    """The paper is the authority on its own records; do not re-derive them."""
    rows = [{"lineage_name": "GRW04", "host_species": "Turdus merula",
             "sequence": LIN_ONE}]
    tally = resolve_rows(rows, resolver, known_lineages=frozenset({"GRW04"}))
    assert rows[0]["lineage_name"] == "GRW04"
    assert "lineage_name_source" not in rows[0]
    assert tally == {"already_named": 1}


def test_resolve_rows_ignores_rows_without_a_sequence(resolver):
    rows = [{"lineage_name": "T001", "host_species": "Turdus merula"}]
    tally = resolve_rows(rows, resolver, known_lineages=frozenset())
    assert rows[0]["lineage_name"] == "T001"
    assert tally == {}


def test_resolve_rows_does_not_name_an_unresolved_row(resolver):
    """A novel sequence leaves the study-local name in place, flagged."""
    rows = [{"lineage_name": "T999", "host_species": "Turdus merula",
             "sequence": _mutate(LIN_ONE, 300)}]
    resolve_rows(rows, resolver, known_lineages=frozenset())
    assert rows[0]["lineage_name"] == "T999"
    assert rows[0]["lineage_resolution"] == "novel"
