"""Tests for the edition comparison that goes into a release's report.

Two properties matter here and they fail in different directions.

**A difference must not be invented.** A published edition carries no row identity, so
rows are matched on natural keys, and those keys are not unique -- MalAvi ships one
duplicated lineage name (TUPHI01) and 302 byte-identical host records. Every test below
that involves a duplicate exists because the obvious implementation (index by key, keep
the last row, compare) reports a *correction* that never happened. That is worse than
missing one: a fabricated change sends a curator looking for a decision nobody made.

**A difference must not be lost.** A table that could not be compared has to say so.
"Not compared" and "nothing changed" render identically as a zero, so the structure
carries ``None`` and ``compared: False`` and the tests pin them.
"""
from __future__ import annotations

import csv

import pytest

from malavi_curation import release_diff
from malavi_curation.release_diff import (
    Edition, compare, current_edition, diff_table, load_previous_edition,
)


# ---------------------------------------------------------------------------
# Fixtures: two small editions, written out in full so a test can be read alone
# ---------------------------------------------------------------------------

def _lineage(name, acc="AB123456", genus="Haemoproteus", species="", sequence="ACGT",
             seq_length="Full"):
    return {"LINEAGE_NAME": name, "GENBANK_ACC": acc, "SEQ_LENGTH": seq_length,
            "GENUS_NAME": genus, "SPECIES_NAME": species, "SEQUENCE": sequence}


def _host(lineage, species="Turdus migratorius", country="United States",
          site="Newark", reference="Smith et al 2020", genus="Turdus", **extra):
    row = {"LINEAGE_NAME": lineage, "ALT_NAME": "", "PARASITE_GENUS": "Haemoproteus",
           "ORDER_NAME": "Passeriformes", "FAMILY_NAME": "Turdidae", "GENUS_NAME": genus,
           "SPECIES_NAME": species, "SUB_SPECIES_NAME": "", "HOST_STATUS": "",
           "HOST_AGE": "", "HOST_ENVIRONMENT": "", "CONTINENT_NAME": "North America",
           "COUNTRY_NAME": country, "COUNTRY_REGION_NAME": "", "SITE_NAME": site,
           "SITE_COORDINATES": "", "NUMBER_FOUND": "1", "NUMBER_TESTED": "10",
           "REFERENCE_NAME": reference, "COMMENT": ""}
    row.update(extra)
    return row


def _reference(name, year="2020", title="A study", journal="Journal"):
    return {"REFERENCE_NAME": name, "PUBLICATION_YEAR": year, "TITLE": title,
            "JOURNAL_NAME": journal, "VOLUME_PAGES": "", "STUDY_TYPE": ""}


def _store(lineages=(), hosts=(), references=(), vectors=(), morpho=(), alt=()):
    return {"lineages": list(lineages), "host_records": list(hosts),
            "references": list(references), "vector_records": list(vectors),
            "morpho_species": list(morpho), "alt_names": list(alt)}


def _edition(label, store, summary=None):
    """An Edition built directly, bypassing the region map the real builder needs."""
    return Edition(label=label, tables=dict(store),
                   summary=list(summary if summary is not None else store["lineages"]),
                   sources={name: "(test)" for name in store})


# ---------------------------------------------------------------------------
# Matching rows
# ---------------------------------------------------------------------------

class TestDiffTable:

    def test_an_added_row_is_added_and_nothing_else(self):
        previous = [_host("TURDUS01")]
        current = [_host("TURDUS01"), _host("TURDUS02")]
        diff = diff_table("host_records", previous, current)
        assert len(diff.added) == 1
        assert diff.added[0]["LINEAGE_NAME"] == "TURDUS02"
        assert diff.removed == [] and diff.modified == []

    def test_a_removed_row_is_removed(self):
        diff = diff_table("host_records", [_host("TURDUS01"), _host("TURDUS02")],
                          [_host("TURDUS01")])
        assert len(diff.removed) == 1
        assert diff.removed[0]["LINEAGE_NAME"] == "TURDUS02"
        assert diff.added == []

    def test_an_edited_row_is_a_modification_not_an_add_and_a_remove(self):
        """The distinction the whole document rests on.

        A record whose count was corrected is one record being fixed. Reported as an
        addition plus a deletion it reads as MalAvi having lost a record and gained a
        different one, which is what a curator would then go and investigate.
        """
        previous = [_host("TURDUS01", **{"NUMBER_FOUND": "1"})]
        current = [_host("TURDUS01", **{"NUMBER_FOUND": "3"})]
        diff = diff_table("host_records", previous, current)
        assert diff.added == [] and diff.removed == []
        assert len(diff.modified) == 1
        assert diff.modified[0]["changed"] == {"NUMBER_FOUND": {"was": "1", "now": "3"}}

    def test_an_unchanged_row_is_reported_as_nothing_at_all(self):
        rows = [_host("TURDUS01"), _host("TURDUS02", species="Turdus merula")]
        diff = diff_table("host_records", rows, list(rows))
        assert (diff.added, diff.removed, diff.modified) == ([], [], [])

    def test_identical_duplicate_rows_survive_as_duplicates(self):
        """302 host records in the real release are byte-identical to another row.

        They are somebody's submitted data and deduplicating them is a curator's
        decision, so an edition that carries two must not be reported as having lost one.
        """
        rows = [_host("TURDUS01"), _host("TURDUS01")]
        diff = diff_table("host_records", rows, list(rows))
        assert (diff.added, diff.removed, diff.modified) == ([], [], [])

    def test_a_duplicated_key_is_never_paired_into_an_edit(self):
        """Two rows under one key: which became which cannot be known, so neither is an edit."""
        previous = [_host("TURDUS01", **{"NUMBER_FOUND": "1"}),
                    _host("TURDUS01", **{"NUMBER_FOUND": "2"})]
        current = [_host("TURDUS01", **{"NUMBER_FOUND": "1"}),
                   _host("TURDUS01", **{"NUMBER_FOUND": "9"})]
        diff = diff_table("host_records", previous, current)
        assert diff.modified == [], "a duplicated key must not produce a fabricated edit"
        assert diff.ambiguous_keys == 1
        assert [row["NUMBER_FOUND"] for row in diff.added] == ["9"]
        assert [row["NUMBER_FOUND"] for row in diff.removed] == ["2"]

    def test_gaining_a_second_copy_of_a_row_is_an_addition(self):
        diff = diff_table("host_records", [_host("TURDUS01")],
                          [_host("TURDUS01"), _host("TURDUS01")])
        assert len(diff.added) == 1
        assert diff.removed == []


# ---------------------------------------------------------------------------
# Loading a published edition
# ---------------------------------------------------------------------------

class TestLoadPreviousEdition:

    def _write(self, directory, stem, release, columns, rows):
        path = directory / f"{stem}_{release}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns))
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path

    def test_the_sibling_tables_are_found_beside_the_summary(self, tmp_path):
        release = "2026-03-23"
        summary_columns = list(release_diff.GRAND_LINEAGE_SUMMARY_COLUMNS)
        summary_row = {column: "" for column in summary_columns}
        summary_row.update(_lineage("TURDUS01"))
        summary = self._write(tmp_path, "grand_lineage_summary", release,
                              summary_columns, [summary_row])
        self._write(tmp_path, "hosts_and_sites", release,
                    _host("TURDUS01").keys(), [_host("TURDUS01")])
        self._write(tmp_path, "references", release,
                    _reference("Smith et al 2020").keys(), [_reference("Smith et al 2020")])

        edition = load_previous_edition(summary)
        assert edition.label == release
        assert len(edition.rows("host_records")) == 1
        # The lineages table is recovered from the summary's own primary-fact columns.
        assert edition.rows("lineages")[0]["GENBANK_ACC"] == "AB123456"
        # Absent siblings are named, not assumed empty.
        assert "vector_records" in edition.missing
        assert "host_records" not in edition.missing

    def test_a_missing_table_is_reported_as_uncompared_not_as_zero(self, tmp_path):
        """The failure this prevents: "0 vector records changed" when there was no table.

        Both render as a zero in a column of numbers, and only one of them means the
        vector data are unchanged.
        """
        release = "2026-03-23"
        summary_columns = list(release_diff.GRAND_LINEAGE_SUMMARY_COLUMNS)
        summary_row = {column: "" for column in summary_columns}
        summary_row.update(_lineage("TURDUS01"))
        summary = self._write(tmp_path, "grand_lineage_summary", release,
                              summary_columns, [summary_row])

        previous = load_previous_edition(summary)
        current = _edition("2026-08-14", _store(lineages=[_lineage("TURDUS01")],
                                                hosts=[_host("TURDUS01")]))
        result = compare(previous, current)

        hosts_total = next(row for row in result["totals"]
                           if row["table"] == "host_records" and row["entity"] ==
                           "Host records")
        assert hosts_total["previous"] is None
        assert hosts_total["delta"] is None
        assert result["tables"]["host_records"]["compared"] is False
        assert "not found" in result["tables"]["host_records"]["note"]
        assert result["hosts"]["compared"] is False


# ---------------------------------------------------------------------------
# The comparison as a whole
# ---------------------------------------------------------------------------

class TestCompare:

    def test_a_new_lineage_carries_what_the_records_say_about_it(self):
        previous = _edition("2026-03-23", _store(lineages=[_lineage("TURDUS01")],
                                                 hosts=[_host("TURDUS01")]))
        current = _edition("2026-08-14", _store(
            lineages=[_lineage("TURDUS01"), _lineage("TURDUS02", acc="PQ118834")],
            hosts=[_host("TURDUS01"),
                   _host("TURDUS02", species="Turdus merula", country="Sweden",
                         reference="Jones et al 2026")]))
        result = compare(previous, current)

        assert result["lineages"]["added_count"] == 1
        added = result["lineages"]["added"][0]
        assert added["lineage"] == "TURDUS02"
        assert added["accession"] == "PQ118834"
        assert added["countries"] == ["Sweden"]
        assert added["references"] == ["Jones et al 2026"]
        assert added["host_records"] == 1

    def test_a_duplicated_lineage_name_is_reported_rather_than_compared(self):
        """REGRESSION: indexing lineages by name silently kept one row and called the
        difference between the two a correction.

        Found by rehearsing an ingest that wrote a second row for a name MalAvi already
        held: the report claimed the existing lineage's accession and sequence had been
        replaced. MalAvi also ships TUPHI01 under two species assignments, so this fires
        on real data in every release.
        """
        previous = _edition("2026-03-23", _store(
            lineages=[_lineage("TURDUS01", acc="KF314763")]))
        current = _edition("2026-08-14", _store(
            lineages=[_lineage("TURDUS01", acc="KF314763"),
                      _lineage("TURDUS01", acc="PQ118836")]))
        result = compare(previous, current)

        assert result["lineages"]["fact_changes"] == [], \
            "a duplicated name must never produce a fabricated correction"
        ambiguous = result["lineages"]["ambiguous"]
        assert [entry["lineage"] for entry in ambiguous] == ["TURDUS01"]
        assert ambiguous[0]["previous_rows"] == 1
        assert ambiguous[0]["current_rows"] == 2
        # And it is excluded from the derived-column corrections for the same reason.
        assert result["summary_columns"]["lineages_not_compared"] == ["TURDUS01"]
        assert result["summary_columns"]["lineages_compared"] == 0

    def test_a_genuine_change_of_fact_is_reported(self):
        previous = _edition("2026-03-23", _store(
            lineages=[_lineage("TURDUS01", acc="KF314763", species="")]))
        current = _edition("2026-08-14", _store(
            lineages=[_lineage("TURDUS01", acc="KF314763",
                               species="Haemoproteus majoris")]))
        result = compare(previous, current)
        assert result["lineages"]["fact_changes"] == [
            {"lineage": "TURDUS01",
             "changed": {"SPECIES_NAME": {"was": "", "now": "Haemoproteus majoris"}}}]

    def test_a_changed_sequence_is_reported_without_printing_it(self):
        """479 characters of nucleotide in a printed table is not a record anybody reads."""
        previous = _edition("2026-03-23", _store(
            lineages=[_lineage("TURDUS01", sequence="ACGT")]))
        current = _edition("2026-08-14", _store(
            lineages=[_lineage("TURDUS01", sequence="ACGA")]))
        change = compare(previous, current)["lineages"]["fact_changes"][0]
        assert change["changed"]["SEQUENCE"] == {
            "was": "4 bp", "now": "4 bp", "detail": "sequence replaced"}

    def test_new_studies_carry_how_much_they_brought(self):
        previous = _edition("2026-03-23", _store(
            lineages=[_lineage("TURDUS01")], hosts=[_host("TURDUS01")],
            references=[_reference("Smith et al 2020")]))
        current = _edition("2026-08-14", _store(
            lineages=[_lineage("TURDUS01")],
            hosts=[_host("TURDUS01"),
                   _host("TURDUS01", site="Dover", reference="Jones et al 2026"),
                   _host("TURDUS01", site="Lewes", reference="Jones et al 2026")],
            references=[_reference("Smith et al 2020"),
                        _reference("Jones et al 2026", year="2026", title="New work")]))
        result = compare(previous, current)

        assert [entry["reference"] for entry in result["references"]["added"]] == \
            ["Jones et al 2026"]
        assert result["references"]["added"][0]["records"] == 2
        assert result["references"]["added"][0]["title"] == "New work"

    def test_added_records_are_grouped_by_the_study_that_reported_them(self):
        previous = _edition("2026-03-23", _store(lineages=[_lineage("TURDUS01")]))
        current = _edition("2026-08-14", _store(
            lineages=[_lineage("TURDUS01")],
            hosts=[_host("TURDUS01", site="Dover", reference="Jones et al 2026"),
                   _host("TURDUS01", site="Lewes", reference="Jones et al 2026",
                         species="Turdus merula", country="Sweden")]))
        groups = compare(previous, current)["tables"]["host_records"]["by_reference"]
        assert groups == [{"reference": "Jones et al 2026", "records": 2,
                           "lineages": 1, "countries": 2, "hosts": 2}]

    def test_a_host_species_is_new_only_when_no_earlier_record_held_it(self):
        """A study reporting a host MalAvi already knows has not added a host species."""
        previous = _edition("2026-03-23", _store(
            lineages=[_lineage("TURDUS01")], hosts=[_host("TURDUS01")]))
        current = _edition("2026-08-14", _store(
            lineages=[_lineage("TURDUS01")],
            hosts=[_host("TURDUS01"),
                   _host("TURDUS01", site="Dover", reference="Jones et al 2026"),
                   _host("TURDUS01", species="Turdus merula", genus="Turdus",
                         country="Sweden", reference="Jones et al 2026")]))
        result = compare(previous, current)
        assert result["hosts"]["new_species"] == ["Turdus merula"]
        assert result["hosts"]["new_countries"] == ["Sweden"]

    def test_the_totals_cover_every_table_and_the_two_derived_counts(self):
        previous = _edition("2026-03-23", _store(lineages=[_lineage("TURDUS01")]))
        current = _edition("2026-08-14", _store(lineages=[_lineage("TURDUS01")]))
        entities = [row["entity"] for row in compare(previous, current)["totals"]]
        assert entities == ["Lineages", "Host records", "Vector records",
                            "References (studies)", "Morphospecies assignments",
                            "Alternative lineage names", "Host species", "Countries"]

    def test_row_listings_are_capped_and_say_so(self):
        previous = _edition("2026-03-23", _store(lineages=[_lineage("TURDUS01")]))
        current = _edition("2026-08-14", _store(
            lineages=[_lineage("TURDUS01")],
            hosts=[_host("TURDUS01", site=f"site-{index}") for index in range(10)]))
        result = compare(previous, current, example_limit=4)
        hosts = result["tables"]["host_records"]
        assert hosts["added"] == 10, "the count must stay complete"
        assert len(hosts["added_rows"]) == 4
        assert hosts["truncated"] is True


class TestCurrentEdition:

    def test_the_store_is_projected_onto_the_release_columns(self):
        """Provenance columns are ours and a published edition cannot have them.

        Left in, every row would differ from its published counterpart in RECORD_ID and
        the whole database would report as changed.
        """
        store = _store(lineages=[dict(_lineage("TURDUS01"), RECORD_ID="LIN-000001",
                                      _source="seed", _added="2026-03-23")])
        edition = current_edition(store, "2026-08-14", summary=[])
        assert "RECORD_ID" not in edition.rows("lineages")[0]
        assert "_source" not in edition.rows("lineages")[0]

    def test_a_supplied_summary_is_used_rather_than_derived_again(self):
        """Deriving it twice costs a pass over 18,000 records for the same answer."""
        summary = [{"LINEAGE_NAME": "TURDUS01", "SUM_HOST": "3"}]
        edition = current_edition(_store(lineages=[_lineage("TURDUS01")]),
                                  "2026-08-14", summary=summary)
        assert edition.summary == summary
