"""Tests for the canonical record store and the one-time seed.

The store is about to become the database. The properties that matter are that it can
hold what the release holds without losing anything, that a row's identity survives
rebuilds, and that a diff between two versions says exactly what changed and nothing
else — because a diff nobody trusts is a diff nobody reads, and then the safety check
before publishing a release is decorative.
"""
from __future__ import annotations

import pytest

from malavi_curation.release_seed import compare_tables, seed_store, verify_round_trip
from malavi_curation.release_store import (
    PROVENANCE_COLUMNS, TABLES, assign_ids, natural_key_violations, read_table,
    record_id, row_key, write_table,
)

SPEC = TABLES["host_records"]


def _row(**kwargs):
    row = {column: "" for column in SPEC.columns}
    row.update(kwargs)
    return row


class TestIdentity:
    def test_ids_are_assigned_to_rows_that_lack_them(self):
        rows = assign_ids(SPEC, [_row(LINEAGE_NAME="A"), _row(LINEAGE_NAME="B")])
        assert all(record_id(r).startswith("HST-") for r in rows)
        assert len({record_id(r) for r in rows}) == 2

    def test_existing_ids_are_never_reissued(self):
        """A curator decision, a correction and a published record may all point at an
        id. Renumbering would detach every reference at once."""
        rows = assign_ids(SPEC, [
            {**_row(LINEAGE_NAME="A"), "RECORD_ID": "HST-000042"},
            _row(LINEAGE_NAME="B"),
        ])
        assert record_id(rows[0]) == "HST-000042"
        assert record_id(rows[1]) != "HST-000042"

    def test_identical_rows_keep_separate_identities(self):
        # 302 host records in the seed release are byte-identical to another row.
        # Merging them would silently discard somebody's submitted data.
        rows = assign_ids(SPEC, [_row(LINEAGE_NAME="A"), _row(LINEAGE_NAME="A")])
        assert len({record_id(r) for r in rows}) == 2

    def test_natural_key_collisions_are_reported_not_resolved(self):
        rows = assign_ids(SPEC, [_row(LINEAGE_NAME="A"), _row(LINEAGE_NAME="A")])
        violations = natural_key_violations(SPEC, rows)
        assert len(violations) == 1
        assert len(violations[0]["record_ids"]) == 2


class TestFileFormat:
    def test_rows_round_trip_through_the_file(self, tmp_path):
        rows = assign_ids(SPEC, [_row(LINEAGE_NAME="B", SPECIES_NAME="Parus major"),
                                 _row(LINEAGE_NAME="A", SPECIES_NAME="Turdus merula")])
        write_table(tmp_path, SPEC, rows)
        back = read_table(tmp_path, SPEC)
        assert {r["LINEAGE_NAME"] for r in back} == {"A", "B"}
        assert all(record_id(r) for r in back)

    def test_written_order_is_stable_regardless_of_input_order(self):
        """A store whose row order depends on insertion order produces a diff in which
        every release appears to change every line, which makes the diff worthless."""
        import tempfile
        from pathlib import Path

        rows = assign_ids(SPEC, [_row(LINEAGE_NAME=n) for n in ("C", "A", "B")])
        outputs = []
        for order in (rows, list(reversed(rows))):
            directory = Path(tempfile.mkdtemp())
            write_table(directory, SPEC, order)
            outputs.append((directory / SPEC.filename).read_text())
        assert outputs[0] == outputs[1]

    def test_provenance_columns_are_written(self, tmp_path):
        write_table(tmp_path, SPEC, assign_ids(SPEC, [_row(LINEAGE_NAME="A")]))
        header = (tmp_path / SPEC.filename).read_text().splitlines()[0]
        for column in PROVENANCE_COLUMNS:
            assert column in header

    def test_provenance_can_be_withheld_for_a_release(self, tmp_path):
        # The release format is Staffan's and does not carry our bookkeeping.
        write_table(tmp_path, SPEC, assign_ids(SPEC, [_row(LINEAGE_NAME="A")]),
                    with_provenance=False)
        header = (tmp_path / SPEC.filename).read_text().splitlines()[0]
        assert "RECORD_ID" not in header
        assert "_source" not in header


class TestDiff:
    def test_an_unchanged_table_reports_no_differences(self):
        rows = [_row(LINEAGE_NAME="A"), _row(LINEAGE_NAME="B")]
        assert compare_tables(SPEC, rows, list(rows))["identical"] is True

    def test_order_alone_is_not_a_difference(self):
        rows = [_row(LINEAGE_NAME="A"), _row(LINEAGE_NAME="B")]
        assert compare_tables(SPEC, rows, list(reversed(rows)))["identical"] is True

    def test_an_added_row_is_reported(self):
        before = [_row(LINEAGE_NAME="A")]
        after = before + [_row(LINEAGE_NAME="B")]
        result = compare_tables(SPEC, before, after)
        assert result["n_added"] == 1 and result["n_removed"] == 0

    def test_a_removed_row_is_reported(self):
        before = [_row(LINEAGE_NAME="A"), _row(LINEAGE_NAME="B")]
        result = compare_tables(SPEC, before, [_row(LINEAGE_NAME="A")])
        assert result["n_removed"] == 1

    def test_duplicate_rows_are_counted_not_collapsed(self):
        """The comparison is a multiset. A key-indexed diff would report a clean result
        while having quietly lost one of two identical rows."""
        before = [_row(LINEAGE_NAME="A"), _row(LINEAGE_NAME="A")]
        result = compare_tables(SPEC, before, [_row(LINEAGE_NAME="A")])
        assert result["n_removed"] == 1
        assert result["identical"] is False

    def test_provenance_is_ignored_by_the_diff(self):
        # The release never carries it, so a change to it is not a change to the data.
        before = [{**_row(LINEAGE_NAME="A"), "_source": "seed", "RECORD_ID": "HST-1"}]
        after = [{**_row(LINEAGE_NAME="A"), "_source": "MALAVI-SUB-2026-000001",
                  "RECORD_ID": "HST-1"}]
        assert compare_tables(SPEC, before, after)["identical"] is True


class TestSeedFromTheRealRelease:
    """The seed is a one-time, irreversible change in where the database lives. It has to
    be shown to lose nothing before anything is built on top of it."""

    @pytest.fixture(scope="class")
    @classmethod
    def seeded(cls):
        from malavi_curation.config import load_config, repo_root

        release = load_config()["malaviR"]["release"]
        downloads = repo_root() / "docs" / "assets" / "downloads" / "tables"
        if not (downloads / f"hosts_and_sites_{release}.csv").is_file():
            pytest.skip("release tables not exported (run export/build_downloads.R)")
        store, report = seed_store(downloads, release)
        return store, report, downloads, release

    def test_the_round_trip_loses_nothing(self, seeded):
        store, _report, downloads, release = seeded
        check = verify_round_trip(downloads, release, store)
        assert check["clean"], (
            "the seed did not reproduce the release it came from; the store cannot be "
            "trusted as the new authority until this is explained")

    def test_every_row_has_a_unique_identity(self, seeded):
        store, _report, _d, _r = seeded
        for name, rows in store.items():
            ids = [record_id(row) for row in rows]
            assert all(ids), f"{name} has rows without an identity"
            assert len(set(ids)) == len(ids), f"{name} has duplicate identities"

    def test_the_derived_summary_is_not_stored(self, seeded):
        # The Grand Lineage Summary's counts and region flags are computable from the
        # records. Storing them would let them drift out of agreement with the records
        # they summarize, which is a defect the current release already has.
        store, report, _d, _r = seeded
        assert "grand_lineage_summary" not in store
        dropped = report["tables"]["lineages"]["columns_dropped"]
        assert "SUM_HOST" in dropped and "EUROPE" in dropped

    def test_known_data_quirks_are_surfaced_rather_than_silently_carried(self, seeded):
        _store, report, _d, _r = seeded
        # The release is known to contain a duplicated lineage name and many host records
        # that share a natural key. The seed must report them, not hide them.
        assert report["tables"]["lineages"]["natural_key_violations"]
        assert report["tables"]["host_records"]["natural_key_violations"]


def test_write_table_does_not_truncate_on_failure(tmp_path):
    """REGRESSION: the authoritative table was opened with "w" and written row by row.

    A crash midway left a truncated database file with no copy of what it replaced.
    """
    from malavi_curation.release_store import TABLES, write_table, read_table

    spec = TABLES["references"]
    good = [{column: f"v{i}" for column in spec.columns} for i in range(3)]
    write_table(tmp_path, spec, good)
    before = (tmp_path / spec.filename).read_text(encoding="utf-8")

    class Exploding(dict):
        def get(self, key, default=None):
            raise RuntimeError("disk full, halfway through")

    with pytest.raises(RuntimeError):
        write_table(tmp_path, spec, good + [Exploding()])

    assert (tmp_path / spec.filename).read_text(encoding="utf-8") == before, \
        "a failed write must leave the previous table intact"
    assert len(read_table(tmp_path, spec)) == 3
    assert not list(tmp_path.glob("*.tmp")), "the temporary file must be cleaned up"


def test_seeding_twice_over_a_populated_store_is_refused(tmp_path):
    """REGRESSION: a second seed silently reassigned every record id from 1."""
    from malavi_curation.release_store import TABLES, write_store, store_is_populated

    spec = TABLES["references"]
    rows = [{column: "x" for column in spec.columns}]
    write_store(tmp_path, {"references": rows})
    assert store_is_populated(tmp_path)

    with pytest.raises(ValueError, match="already holds records"):
        write_store(tmp_path, {"references": rows}, allow_overwrite=False)
