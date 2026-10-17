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


class TestUnusualBases:
    """2026-10-02: every base unusual at its site is listed, over the whole barcode.

    The rule is malaviR's (``.qc_score_site``), so the list is the list the quality screen
    counted. Checked against ``lineage_qc`` itself on 60 seeded queries when this was
    written (201 unusual bases, every count equal); these tests hold the rule in place
    without needing R.
    """

    @staticmethod
    def _release(column_bases):
        """A one-column-per-string release: 1,000 lineages, bases as given per column."""
        from malavi_curation.alignment_figure import site_profile
        width = len(column_bases)
        seqs = {f"L{i}": "".join(column_bases[c][i] for c in range(width))
                for i in range(1000)}
        return seqs, site_profile(seqs)

    def test_the_three_kinds_follow_malavir_s_rule(self):
        from malavi_curation.alignment_figure import unusual_bases
        columns = [
            "A" * 1000,                 # 1: invariant
            "A" * 995 + "C" * 5,        # 2: C carried by 5 of 1,000 -> rare (< 1%)
            "A" * 980 + "C" * 20,       # 3: C carried by 20 of 1,000 -> ordinary
            "A" * 990 + "C" * 10,       # 4: C at exactly 1% smoothed is not below 1%
            "A" * 500 + "C" * 500,      # 5: G never observed, site varies
        ]
        _seqs, profile = self._release(columns)
        found = unusual_bases("GCCCG", "ACAAA", profile)
        assert [(f["position"], f["base"], f["kind"]) for f in found] == [
            (1, "G", "invariant_site_change"),
            (2, "C", "rare"),
            (5, "G", "never_observed"),
        ]
        rare = found[1]
        assert (rare["carrying"], rare["covering"]) == (5, 1000)

    def test_whether_the_nearest_lineage_shares_the_base_is_recorded(self):
        from malavi_curation.alignment_figure import unusual_bases
        _seqs, profile = self._release(["A" * 995 + "C" * 5, "A" * 995 + "C" * 5])
        found = unusual_bases("CC", "CA", profile)
        assert [f["shared_with_nearest"] for f in found] == [True, False]
        assert [f["nearest_base"] for f in found] == ["C", "A"]

    def test_ambiguous_and_uncovered_positions_are_not_listed(self):
        from malavi_curation.alignment_figure import unusual_bases
        _seqs, profile = self._release(["A" * 995 + "C" * 5, "-" * 1000])
        # An N says nothing about the site.
        assert unusual_bases("N", "A", profile) == []
        # A site no lineage covers: malaviR calls any base there never observed, and so
        # does this (no such column exists in the release).
        found = unusual_bases("AC", "AA", profile)
        assert [(f["position"], f["kind"], f["covering"]) for f in found] == [
            (2, "never_observed", 0)]

    def test_a_nearest_lineage_that_holds_no_base_is_recorded_as_silent(self):
        from malavi_curation.alignment_figure import unusual_bases
        _seqs, profile = self._release(["A" * 995 + "C" * 5] * 3)
        found = unusual_bases("CCC", "C-A", profile)
        assert [(f["shared_with_nearest"], f["nearest_covers"]) for f in found] == [
            (True, True), (False, False), (False, True)]

    def test_the_list_covers_positions_the_figure_does_not_draw(self):
        # Thirty rare bases, twenty columns drawn: all thirty are listed.
        from malavi_curation.alignment_figure import site_profile
        width = 30
        common, rare_carrier = "A" * width, "C" * width
        refs = {f"L{i}": common for i in range(995)}
        refs.update({f"R{i}": rare_carrier for i in range(5)})
        fig = build_figure("Q", rare_carrier, [("L0", width, width)], refs,
                           profile=site_profile(refs))
        assert len(fig.positions) == MAX_COLUMNS
        assert len(fig.unusual_bases) == width
        assert fig.nearest_name == "L0"
        assert not any(f["shared_with_nearest"] for f in fig.unusual_bases)
        assert fig.as_dict()["unusual_bases"] == fig.unusual_bases

    def test_the_constants_are_malavir_s_defaults(self):
        from malavi_curation import alignment_figure
        assert alignment_figure.MALAVIR_RARE_BASE_FREQUENCY == 0.01
        assert alignment_figure.MALAVIR_PSEUDOCOUNT == 0.01


class TestFullRows:
    """2026-10-02 (to-do I): the figure carries the whole rows it was cut from, so the
    report can show the full alignment without a second placement."""

    def test_the_rows_are_the_very_strings_the_figure_used(self):
        fig = build_figure("Q", QUERY, [("ONE", 1, 10), ("PART", 0, 5)],
                           _refs(ONE=ONE_OFF, PART=PARTIAL))
        assert fig.sequences == [{"name": "Q", "seq": QUERY},
                                 {"name": "ONE", "seq": ONE_OFF},
                                 {"name": "PART", "seq": PARTIAL}]
        assert fig.as_dict()["sequences"] == fig.sequences

    def test_no_figure_means_no_rows(self):
        assert build_figure("Q", None, [("ONE", 1, 10)], _refs(ONE=ONE_OFF)).sequences == []

