"""The lineage-history events: the row each writes and what each does to the store.

The invariant under test is the curators' condition of 2026-09-24: no change to what a
lineage name means without a public row that maps the old onto the new, and no row
without the change it describes.
"""
from __future__ import annotations

import pytest

from malavi_curation import lineage_history as lh
from malavi_curation.release_store import TABLES

SEQ = ("ATGCATGCTA" * 48)[:479]
PARTIAL = "-" * 9 + SEQ[9:]           # the same barcode with the first nine bases undetermined


def _store():
    return {
        "lineages": [
            {"LINEAGE_NAME": "CCF2", "GENBANK_ACC": "AB000001", "SEQ_LENGTH": "470",
             "SEQUENCE": PARTIAL, "RECORD_ID": "LIN-000001", "_source": "seed", "_added": "2026-03-23"},
            {"LINEAGE_NAME": "ACCBRE02", "GENBANK_ACC": "AB000002", "SEQ_LENGTH": "479",
             "SEQUENCE": SEQ, "RECORD_ID": "LIN-000002", "_source": "seed", "_added": "2026-03-23"},
            {"LINEAGE_NAME": "ACCBRE01", "GENBANK_ACC": "AB000003", "SEQ_LENGTH": "300",
             "SEQUENCE": SEQ[:300], "RECORD_ID": "LIN-000003", "_source": "seed", "_added": "2026-03-23"},
            {"LINEAGE_NAME": "TURGUL01", "GENBANK_ACC": "", "SEQ_LENGTH": "479",
             "SEQUENCE": SEQ, "RECORD_ID": "LIN-000004", "_source": "seed", "_added": "2026-03-23"},
        ],
        "host_records": [
            {"LINEAGE_NAME": "ACCBRE01", "SPECIES_NAME": "Accipiter brevipes", "SITE_NAME": "x",
             "REFERENCE_NAME": "A 2020", "RECORD_ID": "HST-000001", "_source": "seed", "_added": ""},
            {"LINEAGE_NAME": "ACCBRE02", "SPECIES_NAME": "Accipiter brevipes", "SITE_NAME": "y",
             "REFERENCE_NAME": "B 2021", "RECORD_ID": "HST-000002", "_source": "seed", "_added": ""},
            {"LINEAGE_NAME": "TURGUL01", "SPECIES_NAME": "Turdus", "SITE_NAME": "z",
             "REFERENCE_NAME": "C 2022", "RECORD_ID": "HST-000003", "_source": "seed", "_added": ""},
        ],
        "vector_records": [],
        "alt_names": [{"LINEAGE_NAME": "ACCBRE01", "ALT_NAME": "AB1", "REFERENCE_NAME": "A 2020",
                       "RECORD_ID": "ALT-000001", "_source": "seed", "_added": ""}],
        "morpho_species": [],
        "references": [],
        "lineage_history": [],
        "genomic_resources": [{"LINEAGE_NAME": "ACCBRE01", "RESOURCE_TYPE": "mitogenome",
                               "ACCESSION": "MT000001", "RECORD_ID": "GEN-000001", "_source": "seed", "_added": ""}],
    }


def _names(store, table):
    return sorted(r["LINEAGE_NAME"] for r in store[table])


class TestSequenceUpdated:
    def test_keeps_the_name_and_replaces_the_sequence(self):
        store = _store()
        ev = lh.Event("sequence_updated", "CCF2", new_sequence=SEQ, accession="PQ000001",
                      date="2026-09-25", reason="complete barcode", source="MALAVI-SUB-2026-000011",
                      release="2026-10-01")
        p = lh.apply(store, ev, source_tag="MALAVI-SUB-2026-000011")
        row = [r for r in store["lineages"] if r["LINEAGE_NAME"] == "CCF2"][0]
        assert row["SEQUENCE"] == SEQ and row["SEQ_LENGTH"] == "479" and row["GENBANK_ACC"] == "PQ000001"
        hist = store["lineage_history"][0]
        assert hist["EVENT"] == "sequence_updated"
        assert hist["OLD_NAME"] == hist["NEW_NAME"] == hist["LINEAGE_NAME"] == "CCF2"
        assert hist["OLD_SEQUENCE"] == PARTIAL and hist["NEW_SEQUENCE"] == SEQ
        assert hist["SOURCE"] == "MALAVI-SUB-2026-000011" and hist["RELEASE"] == "2026-10-01"
        assert hist["_source"] == "MALAVI-SUB-2026-000011"
        assert set(hist) >= set(TABLES["lineage_history"].columns)
        assert p.counts == {"lineages": 1}

    def test_the_same_sequence_is_refused(self):
        with pytest.raises(lh.HistoryError, match="already stored"):
            lh.plan(_store(), lh.Event("sequence_updated", "ACCBRE02", new_sequence=SEQ))

    def test_no_sequence_is_refused(self):
        with pytest.raises(lh.HistoryError, match="needs the new sequence"):
            lh.plan(_store(), lh.Event("sequence_updated", "CCF2"))


class TestRenamed:
    def test_every_citing_row_follows_and_the_old_name_becomes_a_synonym(self):
        store = _store()
        p = lh.apply(store, lh.Event("renamed", "ACCBRE01", new_name="ACCBRE03",
                                     reason="r", source="curator decision", release="2026-10-01"))
        assert "ACCBRE01" not in _names(store, "lineages") and "ACCBRE03" in _names(store, "lineages")
        assert [r["LINEAGE_NAME"] for r in store["host_records"]][0] == "ACCBRE03"
        alts = {(r["LINEAGE_NAME"], r["ALT_NAME"]) for r in store["alt_names"]}
        assert ("ACCBRE03", "AB1") in alts and ("ACCBRE03", "ACCBRE01") in alts
        hist = store["lineage_history"][0]
        assert hist["LINEAGE_NAME"] == "ACCBRE03" and hist["OLD_NAME"] == "ACCBRE01"
        assert hist["OLD_SEQUENCE"] == hist["NEW_SEQUENCE"] == SEQ[:300]
        assert p.counts["host_records"] == 1 and p.counts["alt_names"] == 1

    def test_a_taken_name_is_refused_and_points_at_merge(self):
        with pytest.raises(lh.HistoryError, match="merged_into"):
            lh.plan(_store(), lh.Event("renamed", "ACCBRE01", new_name="ACCBRE02"))


class TestMergedInto:
    def test_rows_move_to_the_target_and_the_old_lineage_row_goes(self):
        store = _store()
        p = lh.apply(store, lh.Event("merged_into", "ACCBRE01", new_name="ACCBRE02",
                                     reason="identical over the overlap", source="curators 2026-09-24",
                                     release="2026-10-01"))
        assert _names(store, "lineages") == ["ACCBRE02", "CCF2", "TURGUL01"]
        assert _names(store, "host_records") == ["ACCBRE02", "ACCBRE02", "TURGUL01"]
        assert _names(store, "genomic_resources") == ["ACCBRE02"]      # the genome follows the merge
        alts = {(r["LINEAGE_NAME"], r["ALT_NAME"]) for r in store["alt_names"]}
        assert ("ACCBRE02", "ACCBRE01") in alts and ("ACCBRE02", "AB1") in alts
        hist = store["lineage_history"][0]
        assert hist["EVENT"] == "merged_into" and hist["LINEAGE_NAME"] == "ACCBRE02"
        assert hist["OLD_SEQUENCE"] == SEQ[:300] and hist["NEW_SEQUENCE"] == SEQ
        assert p.counts == {"lineages": 1, "host_records": 1, "alt_names": 1, "genomic_resources": 1}

    def test_an_unknown_target_is_refused_and_points_at_rename(self):
        with pytest.raises(lh.HistoryError, match="use renamed"):
            lh.plan(_store(), lh.Event("merged_into", "ACCBRE01", new_name="NOPE01"))

    def test_merging_twice_does_not_duplicate_the_synonym(self):
        store = _store()
        lh.apply(store, lh.Event("merged_into", "ACCBRE01", new_name="ACCBRE02", release="r"))
        n = sum(1 for r in store["alt_names"] if r["ALT_NAME"] == "ACCBRE01")
        assert n == 1


class TestRemoved:
    def test_the_lineage_and_everything_citing_it_go(self):
        store = _store()
        p = lh.apply(store, lh.Event("removed", "TURGUL01", reason="should not be in MalAvi",
                                     source="curators 2026-09-24", release="2026-10-01"))
        assert "TURGUL01" not in _names(store, "lineages")
        assert "TURGUL01" not in _names(store, "host_records")
        hist = store["lineage_history"][0]
        assert hist["EVENT"] == "removed" and hist["NEW_NAME"] == "" and hist["NEW_SEQUENCE"] == ""
        assert hist["OLD_SEQUENCE"] == SEQ and hist["LINEAGE_NAME"] == "TURGUL01"
        assert p.counts == {"lineages": 1, "host_records": 1}


class TestGuards:
    def test_an_unknown_lineage_is_refused(self):
        with pytest.raises(lh.HistoryError, match="not a lineage"):
            lh.plan(_store(), lh.Event("removed", "NOPE99"))

    def test_an_unknown_event_is_refused(self):
        with pytest.raises(lh.HistoryError, match="unknown event"):
            lh.history_row(lh.Event("split", "CCF2"), "", "")

    def test_plan_writes_nothing(self):
        store = _store()
        before = {k: [dict(r) for r in v] for k, v in store.items()}
        lh.plan(store, lh.Event("removed", "TURGUL01"))
        assert store == before


class TestCurrentName:
    def test_follows_a_chain_of_renames_and_merges(self):
        history = [
            {"EVENT": "renamed", "OLD_NAME": "A01", "NEW_NAME": "A02"},
            {"EVENT": "merged_into", "OLD_NAME": "A02", "NEW_NAME": "B01"},
            {"EVENT": "sequence_updated", "OLD_NAME": "B01", "NEW_NAME": "B01"},
        ]
        assert lh.current_name(history, "A01") == ("B01", ["A01 renamed A02", "A02 merged_into B01"])
        assert lh.current_name(history, "B01") == ("B01", [])

    def test_a_removed_name_has_no_current_name(self):
        history = [{"EVENT": "removed", "OLD_NAME": "T01", "NEW_NAME": ""}]
        assert lh.current_name(history, "T01") == ("", ["T01 removed"])

    def test_a_cycle_stops(self):
        history = [{"EVENT": "renamed", "OLD_NAME": "A", "NEW_NAME": "B"},
                   {"EVENT": "renamed", "OLD_NAME": "B", "NEW_NAME": "A"}]
        current, trail = lh.current_name(history, "A")
        assert len(trail) == 2
