"""Tests for the country -> MalAvi-region table.

The table is the only part of a release that is not computable from the records, so it is
the part most worth checking. The test that earns its place is the last one: it re-derives
the mapping from the pinned release and asserts the committed table has not drifted away
from the evidence it was read out of. Without it, a well-meaning edit ("surely Russia is
Asia") would silently change what every future release says about 343 records.
"""
from __future__ import annotations

import csv

import pytest

from malavi_curation.country_regions import (
    HAWAII_COUNTRY, HAWAII_COUNTRY_REGION, REGION_COLUMNS, infer_from_release,
    load_region_map, region_for, rows_needing_review, table_path, unmapped_countries,
)
from malavi_curation.release_index import table_dir

RELEASE = "2026-03-23"


@pytest.fixture(scope="module")
def committed():
    return load_region_map()


def _release_csv(name):
    path = table_dir() / f"{name}_{RELEASE}.csv"
    if not path.is_file():
        pytest.skip(f"{path.name} is not in this checkout")
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


class TestTable:
    def test_every_region_is_one_of_the_twelve(self, committed):
        assert set(committed.values()) <= set(REGION_COLUMNS)

    def test_the_conventions_that_are_not_guessable(self, committed):
        """Three entries a reasonable person would get wrong from first principles.

        Each is MalAvi's own choice, recovered from the release rather than invented.
        """
        assert committed["Russia"] == "EUROPE"
        assert committed["Mexico"] == "CENTRAL_AMERICA"
        assert committed["Armenia"] == "NORTH_AFRICA_AND_MIDDLE_EAST"

    def test_hawaii_is_resolved_before_the_country(self, committed):
        assert region_for(HAWAII_COUNTRY, HAWAII_COUNTRY_REGION, committed) == "HAWAI"
        assert region_for(HAWAII_COUNTRY, "Texas", committed) == "NORTH_AMERICA"

    def test_an_unknown_country_is_none_not_a_region(self, committed):
        assert region_for("Atlantis", "", committed) is None

    def test_unmapped_countries_are_counted_by_row(self, committed):
        rows = [{"COUNTRY_NAME": "Atlantis"}, {"COUNTRY_NAME": "Atlantis"},
                {"COUNTRY_NAME": "Sweden"}]
        assert unmapped_countries(rows, committed) == {"Atlantis": 2}


class TestAgainstTheRelease:
    def test_every_country_in_the_release_is_mapped(self, committed):
        """A record whose country is not in the table sets no region at all, which reads
        exactly like a lineage that genuinely has none."""
        hosts = _release_csv("hosts_and_sites")
        vectors = _release_csv("vector_data")
        missing = unmapped_countries(hosts + vectors, committed)
        assert missing == {}, f"unmapped countries in the pinned release: {missing}"

    def test_no_inferred_entry_contradicts_the_release(self, committed):
        """The committed table must still say what the release says.

        A lineage whose host records name exactly one country got its region flags from
        that country, so each such lineage is a vote. A committed region that loses its
        own vote has been edited away from the evidence.
        """
        votes = infer_from_release(_release_csv("hosts_and_sites"),
                                   _release_csv("grand_lineage_summary"))
        contradicted = []
        for country, tally in votes.items():
            winner, count = tally.most_common(1)[0]
            # Only where the release is decisive: a country whose votes are split is
            # exactly the stale-summary noise this table exists to clean up.
            if count >= 3 and count / sum(tally.values()) > 0.9:
                if committed.get(country) != winner:
                    contradicted.append((country, committed.get(country), winner))
        assert not contradicted, f"committed table contradicts the release: {contradicted}"

    def test_entries_still_awaiting_a_curator_are_visible(self):
        """Not an assertion that there are none -- an assertion that if there are, they
        are reachable, so the release build can warn instead of shipping a guess."""
        for row in rows_needing_review():
            assert row["COUNTRY_NAME"]
            assert row.get("EVIDENCE"), (
                f"{row['COUNTRY_NAME']} is flagged for review with no evidence recorded, "
                f"so a curator has nothing to decide from")

    def test_the_table_records_how_each_row_was_settled(self):
        """BASIS is what makes the difference between measured and asserted auditable."""
        with open(table_path(), newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert rows
        assert {row["BASIS"] for row in rows} <= {"inferred", "eliminated", "authored"}
        assert all(row["EVIDENCE"] for row in rows)
