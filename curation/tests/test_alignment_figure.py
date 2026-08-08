"""Tests for the alignment figure.

The figure exists to support one judgment: is a small difference from a known lineage
real, or an artifact? So the properties that matter are that it shows the *right*
positions, that it never invents an alignment, and that it says so plainly when it cannot
help — because a figure that looks fine while meaning nothing is worse than no figure.
"""
from __future__ import annotations

import pytest

from malavi_curation.alignment_figure import (
    MAX_COLUMNS, AlignmentFigure, build_figure, build_figures,
)

# A tiny window. Positions are 1-based in the output, 0-based here.
#            pos: 1234567890
QUERY = "ACGTACGTAC"
SAME = "ACGTACGTAC"          # identical
ONE_OFF = "ACGTTCGTAC"       # differs at position 5
TWO_OFF = "ACGTTCGTAG"       # differs at 5 and 10
PARTIAL = "-----CGTAC"       # covers only positions 6-10


def _refs(**kwargs):
    return dict(kwargs)


class TestWhichPositionsAreShown:
    def test_only_differing_positions_appear(self):
        fig = build_figure("Q", QUERY, [("ONE", 1, 10)], _refs(ONE=ONE_OFF))
        assert fig.positions == [5]
        assert fig.n_differing == 1

    def test_positions_are_one_based(self):
        # A curator counts from 1. Reporting a 0-based index would send them to the
        # wrong base, which is the one thing this figure must not do.
        fig = build_figure("Q", QUERY, [("TWO", 2, 10)], _refs(TWO=TWO_OFF))
        assert fig.positions == [5, 10]

    def test_an_identical_neighbour_produces_no_columns(self):
        fig = build_figure("Q", QUERY, [("SAME", 0, 10)], _refs(SAME=SAME))
        assert fig.positions == []
        assert fig.n_differing == 0

    def test_a_column_varying_only_among_neighbours_is_shown(self):
        # The query may match one neighbour and not another at the same site. That the
        # site varies at all is what tells a curator whether a change is unusual.
        fig = build_figure("Q", QUERY, [("SAME", 0, 10), ("ONE", 1, 10)],
                           _refs(SAME=SAME, ONE=ONE_OFF))
        assert fig.positions == [5]

    def test_positions_a_neighbour_does_not_cover_are_not_differences(self):
        # A lineage that does not span a position has no opinion about it. Counting a
        # gap as a difference would make every short partial lineage look divergent.
        fig = build_figure("Q", QUERY, [("PART", 0, 5)], _refs(PART=PARTIAL))
        assert fig.positions == []


class TestCellStates:
    def test_identity_is_marked_same_and_difference_diff(self):
        fig = build_figure("Q", QUERY, [("ONE", 1, 10)], _refs(ONE=ONE_OFF))
        neighbour = fig.rows[1]
        assert neighbour["cells"][0]["state"] == "diff"
        assert neighbour["cells"][0]["base"] == "T"

    def test_the_query_row_is_marked_as_the_query(self):
        fig = build_figure("Q", QUERY, [("ONE", 1, 10)], _refs(ONE=ONE_OFF))
        assert fig.rows[0]["is_query"] is True
        assert all(c["state"] == "query" for c in fig.rows[0]["cells"])

    def test_an_uncovered_position_is_nodata_not_a_difference(self):
        fig = build_figure("Q", QUERY,
                           [("ONE", 1, 10), ("PART", 0, 5)],
                           _refs(ONE=ONE_OFF, PART=PARTIAL))
        part = [r for r in fig.rows if r["name"] == "PART"][0]
        # Position 5 is outside PARTIAL's span: shown, but not as a difference.
        assert part["cells"][0]["state"] == "nodata"

    def test_distances_travel_with_each_neighbour(self):
        fig = build_figure("Q", QUERY, [("ONE", 1, 478)], _refs(ONE=ONE_OFF))
        assert fig.rows[1]["distance"] == 1
        assert fig.rows[1]["comparable"] == 478


class TestRefusals:
    """What the figure does when it cannot honestly help."""

    def test_an_unregistered_sequence_gets_no_figure(self):
        # NECMON01's case. Drawing a de-novo alignment here would hide the finding
        # behind something that looks fine.
        fig = build_figure("Q", None, [("ONE", 1, 10)], _refs(ONE=ONE_OFF))
        assert fig.positions == []
        assert fig.unavailable and "could not be registered" in fig.unavailable
        assert "resolve the framing" in fig.unavailable

    def test_no_overlapping_neighbour_gets_no_figure(self):
        fig = build_figure("Q", QUERY, [("MISSING", 1, 10)], _refs(OTHER=SAME))
        assert fig.unavailable and "overlaps" in fig.unavailable

    def test_no_neighbours_at_all_gets_no_figure(self):
        fig = build_figure("Q", QUERY, [], _refs(ONE=ONE_OFF))
        assert fig.unavailable


class TestTruncation:
    def test_a_divergent_sequence_is_truncated_and_says_so(self):
        query = "A" * 100
        neighbour = "C" * 100
        fig = build_figure("Q", query, [("FAR", 100, 100)], _refs(FAR=neighbour))
        assert fig.n_differing == 100
        assert fig.truncated is True
        assert len(fig.positions) == MAX_COLUMNS
        # The full count survives truncation: "20 of 100" is the useful statement,
        # and reporting only the 20 shown would understate how divergent it is.
        assert fig.n_differing > len(fig.positions)

    def test_a_short_figure_is_not_marked_truncated(self):
        fig = build_figure("Q", QUERY, [("ONE", 1, 10)], _refs(ONE=ONE_OFF))
        assert fig.truncated is False


class TestFromScreenReports:
    def test_builds_one_figure_per_sequence(self):
        reports = [{
            "workbook": "w.xlsx",
            "sequences": [
                {"label": "A", "nearest": [{"lineage": "ONE", "distance": 1,
                                            "comparable": 10}]},
                {"label": "B", "nearest": []},
            ],
        }]
        figures = build_figures(reports, {"A": QUERY, "B": QUERY}, _refs(ONE=ONE_OFF))
        assert [f.label for f in figures] == ["A", "B"]
        assert figures[0].positions == [5]
        assert figures[1].unavailable        # no neighbours to align against

    def test_a_sequence_with_no_registration_is_handled(self):
        reports = [{"sequences": [{"label": "A", "nearest": [
            {"lineage": "ONE", "distance": 1, "comparable": 10}]}]}]
        figures = build_figures(reports, {}, _refs(ONE=ONE_OFF))
        assert figures[0].unavailable


class TestAgainstTheRealRelease:
    """The figure must agree with the distances printed elsewhere in the report.

    This is the whole reason no external aligner is used: the release is already a fixed
    alignment and the checker already registered the query into it. A figure built from a
    different alignment could contradict the numbers above it.
    """

    def test_the_figure_agrees_with_the_reported_distance(self):
        from malavi_curation.config import load_config, repo_root
        from malavi_curation.sequence_check import (
            Reference, check_sequence, default_alignment_path,
        )

        path = default_alignment_path(repo_root(), load_config()["malaviR"]["release"])
        if path is None or not path.is_file():
            pytest.skip("no release alignment on disk")

        release = Reference.from_fasta(path)
        seqs = dict(zip(release.names, release.seqs))

        # A single-base mutant of a real lineage: distance 1, and the figure must show
        # exactly one column where that lineage differs from the query.
        name = next(n for n, s in seqs.items()
                    if len(s) == release.width and not set(s) - set("ACGT"))
        original = seqs[name]
        mutated = original[:200] + ("G" if original[200] != "G" else "C") + original[201:]

        res = check_sequence(mutated, release, label="mutant")
        fig = build_figure("mutant", res.registered, res.nearest, seqs)

        reported = [n for n in res.nearest if n[0] == name][0][1]
        row = [r for r in fig.rows if r["name"] == name][0]
        drawn = sum(1 for c in row["cells"] if c["state"] == "diff")
        assert drawn == reported == 1, (
            f"the figure shows {drawn} differences where the report states {reported}")
        assert 201 in fig.positions, "the differing position should be the mutated one"
