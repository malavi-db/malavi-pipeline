"""Correcting records MalAvi already holds.

These are corrections to *published* data, so the properties that matter are about not
doing too much: a selector that matches more than the operator meant is how a fix for one
site's coordinates becomes a fault at another. The real data made that concrete --
"Mata Seca State Park" and "Manga, Mata Seca State Park" are two different sites with
entirely different coordinates, 54 rows and 53 rows, and a substring match would have
corrupted the second while fixing the first.
"""
from __future__ import annotations

import pytest

from malavi_curation import store_corrections
from malavi_curation.store_corrections import Correction


def _host(record, site="Ottenby", coords="55°N, 16°E", lineage="TURDUS01",
          species="Turdus merula", reference="Bensch 2000"):
    return {"RECORD_ID": record, "LINEAGE_NAME": lineage, "SPECIES_NAME": species,
            "GENUS_NAME": "Turdus", "SITE_NAME": site, "SITE_COORDINATES": coords,
            "COUNTRY_NAME": "Sweden", "REFERENCE_NAME": reference,
            "NUMBER_FOUND": "1", "NUMBER_TESTED": "10", "_source": "seed",
            "_added": "2026-03-23"}


def _store(*rows):
    return {"host_records": list(rows), "vector_records": [], "lineages": [],
            "references": [], "morpho_species": [], "alt_names": []}


def _correction(**kwargs):
    base = dict(table="host_records", column="SITE_COORDINATES",
                new_value="55°N, -16°E", reason="sign", selector_kind="site",
                selector_value="Ottenby")
    base.update(kwargs)
    return Correction(**base)


class TestSelection:

    def test_a_site_selects_every_row_at_it_as_one_decision(self):
        store = _store(_host("HST-1"), _host("HST-2"), _host("HST-3", site="Kvismaren"))
        changes = store_corrections.plan(store, _correction())
        assert [change.record for change in changes] == ["HST-1", "HST-2"]

    def test_a_site_is_matched_exactly_not_by_substring(self):
        """REAL DATA: 'Mata Seca State Park' (54 rows) and 'Manga, Mata Seca State Park'
        (53 rows) are different sites with different coordinates. A substring match would
        have corrupted the second while fixing the first."""
        store = _store(_host("HST-1", site="Mata Seca State Park"),
                       _host("HST-2", site="Manga, Mata Seca State Park"))
        changes = store_corrections.plan(
            store, _correction(selector_value="Mata Seca State Park"))
        assert [change.record for change in changes] == ["HST-1"]

    def test_a_record_id_selects_exactly_one_row(self):
        store = _store(_host("HST-1"), _host("HST-2"))
        changes = store_corrections.plan(
            store, _correction(selector_kind="record", selector_value="HST-2"))
        assert [change.record for change in changes] == ["HST-2"]

    def test_a_value_selector_selects_by_exact_value(self):
        store = _store(_host("HST-1", coords="bad"), _host("HST-2", coords="fine"))
        changes = store_corrections.plan(
            store, _correction(selector_kind="where", selector_column="SITE_COORDINATES",
                               selector_value="bad"))
        assert [change.record for change in changes] == ["HST-1"]

    def test_a_row_already_holding_the_new_value_is_not_a_change(self):
        """Re-running an applied correction must report nothing to do, not inflate the
        count in the log and in the edition report."""
        store = _store(_host("HST-1", coords="55°N, -16°E"))
        assert store_corrections.plan(store, _correction()) == []


class TestRefusals:

    def test_an_unknown_column_is_refused_with_the_real_columns(self):
        with pytest.raises(ValueError, match="has no column"):
            store_corrections.plan(_store(_host("HST-1")),
                                   _correction(column="LONGITUDE"))

    def test_an_unknown_table_is_refused(self):
        with pytest.raises(ValueError, match="unknown table"):
            store_corrections.plan(_store(_host("HST-1")), _correction(table="birds"))

    def test_a_table_without_sites_cannot_be_selected_by_site(self):
        store = {"lineages": [{"RECORD_ID": "LIN-1", "LINEAGE_NAME": "TURDUS01"}]}
        with pytest.raises(ValueError, match="no SITE_NAME"):
            store_corrections.plan(store, _correction(table="lineages",
                                                      column="GENBANK_ACC"))


class TestApplying:

    def test_only_the_selected_rows_change(self):
        store = _store(_host("HST-1"), _host("HST-2", site="Kvismaren"))
        store_corrections.apply(store, _correction())
        rows = {row["RECORD_ID"]: row for row in store["host_records"]}
        assert rows["HST-1"]["SITE_COORDINATES"] == "55°N, -16°E"
        assert rows["HST-2"]["SITE_COORDINATES"] == "55°N, 16°E"

    def test_provenance_is_not_rewritten_by_a_correction(self):
        """_source says where a row came from and _added when it first appeared. A later
        correction changes neither: the row is still that study's record."""
        store = _store(_host("HST-1"))
        store_corrections.apply(store, _correction())
        row = store["host_records"][0]
        assert (row["_source"], row["_added"]) == ("seed", "2026-03-23")


class TestTheLog:

    def test_one_decision_is_one_line_however_many_rows(self, tmp_path):
        store = _store(_host("HST-1"), _host("HST-2"), _host("HST-3"))
        changes = store_corrections.apply(store, _correction(reason="longitude sign"))
        path = tmp_path / "corrections.csv"
        row = store_corrections.append_log(path, _correction(reason="longitude sign"),
                                           changes, applied_on="2026-08-14")
        assert row["ROWS_CHANGED"] == "3"
        assert row["REASON"] == "longitude sign"
        assert row["SELECTOR"] == "site 'Ottenby'"
        assert row["OLD_VALUE"] == "55°N, 16°E"
        assert len(store_corrections.read_log(path)) == 1

    def test_rows_that_held_different_values_are_recorded_as_various(self, tmp_path):
        store = _store(_host("HST-1", coords="one"), _host("HST-2", coords="two"))
        changes = store_corrections.apply(store, _correction())
        row = store_corrections.append_log(tmp_path / "corrections.csv",
                                           _correction(), changes)
        assert row["OLD_VALUE"] == "(various)"

    def test_the_next_id_comes_from_the_highest_not_the_count(self):
        """Counting rows mints a duplicate the moment one is ever removed."""
        existing = [{"CORRECTION_ID": "COR-000001"}, {"CORRECTION_ID": "COR-000007"}]
        assert store_corrections.next_correction_id(existing) == "COR-000008"
        assert store_corrections.next_correction_id([]) == "COR-000001"

    def test_the_log_appends_rather_than_replacing(self, tmp_path):
        path = tmp_path / "corrections.csv"
        store = _store(_host("HST-1"))
        changes = store_corrections.apply(store, _correction())
        store_corrections.append_log(path, _correction(), changes)
        store_corrections.append_log(path, _correction(reason="second"), changes)
        log = store_corrections.read_log(path)
        assert [row["CORRECTION_ID"] for row in log] == ["COR-000001", "COR-000002"]


# ------------------------------------------- whitespace damage: the fault it could not fix

def _accession_store(value):
    """A one-row lineages table whose accession holds `value` verbatim."""
    return {"lineages": [{"RECORD_ID": "LIN-000589", "LINEAGE_NAME": "ATALB01",
                          "GENBANK_ACC": value, "_source": "seed", "_added": "2026-03-23"}]}


def test_a_row_damaged_by_whitespace_can_be_selected_by_its_clean_value():
    """The needle and the haystack were being measured differently.

    ``_text`` stripped the stored value -- and Python's ``strip()`` removes U+00A0 -- while
    the target came straight from argv. So a row whose fault *was* the surrounding
    whitespace matched neither the damaged string an operator copied out of the CSV nor the
    clean one they would try first.
    """
    correction = store_corrections.Correction(
        table="lineages", column="GENBANK_ACC", new_value="KF717063",
        reason="non-breaking spaces from an Excel paste", actor="maintainer",
        selector_kind="where", selector_value="KF717063",
        selector_column="GENBANK_ACC")

    changes = store_corrections.plan(_accession_store("\xa0 KF717063"), correction)

    assert len(changes) == 1
    assert changes[0].was == "\xa0 KF717063"
    assert changes[0].now == "KF717063"


def test_stripping_whitespace_is_a_change_not_a_no_op():
    """The raw string is what gets written to the CSV, so the raw string is what counts.

    Judging "already carries the new value" on a stripped copy called this a no-op and
    skipped it, which is the second half of why whitespace damage was unfixable.
    """
    correction = store_corrections.Correction(
        table="lineages", column="GENBANK_ACC", new_value="KF717063",
        reason="trailing non-breaking space", actor="maintainer",
        selector_kind="record", selector_value="LIN-000589", selector_column="")

    for damaged in ("KF717063\xa0", "\xa0KF717063", "KF717063 ", "\xa0 KF717063"):
        changes = store_corrections.plan(_accession_store(damaged), correction)
        assert len(changes) == 1, f"{damaged!r} should be correctable"
        assert changes[0].was == damaged

    # And a genuinely clean row is still a no-op.
    assert store_corrections.plan(_accession_store("KF717063"), correction) == []


def test_a_selector_typed_with_stray_spaces_still_matches():
    """Now that both sides are stripped, a copy-paste with a trailing space works."""
    correction = store_corrections.Correction(
        table="lineages", column="GENBANK_ACC", new_value="KF717063",
        reason="whitespace", actor="maintainer",
        selector_kind="where", selector_value="  KF717063  ",
        selector_column="GENBANK_ACC")

    assert len(store_corrections.plan(_accession_store("\xa0KF717063"), correction)) == 1
