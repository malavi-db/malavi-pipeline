"""Tests for the template -> submission adapter and the normalization contract.

The point of the adapter is that a filled template and an extracted PDF become the same
kind of object, so the same checks can run on both. These tests hold that line: the
contract test at the bottom is the one that fails if the two paths ever drift into
meaning different things by the same fields.

Every fixture is synthetic. No real submission data appears here -- the intake tree holds
unpublished sequences and submitter email addresses, and a test fixture is a file that
gets committed.
"""
from __future__ import annotations

import json

import pytest

openpyxl = pytest.importorskip("openpyxl")

from malavi_curation import normalize                                  # noqa: E402
from malavi_curation.record_builder import build_submission            # noqa: E402
from malavi_curation.template_adapter import (                         # noqa: E402
    CANONICAL_HEADERS, SCHEMA_VERSION, build_submission_from_workbook,
    looks_like_template, sheet_rows,
)


# ---------------------------------------------------------------------------------------
# Fixture construction
# ---------------------------------------------------------------------------------------

def _sheet(workbook, title, note, header, rows):
    """Build one template-shaped sheet: an instruction note, a header, then data.

    The leading note row matters -- the real template carries one on every sheet, and the
    header finder has to skip it. A fixture without it would test an easier problem than
    the one the adapter actually faces.
    """
    worksheet = workbook.create_sheet(title)
    worksheet.append([note])
    worksheet.append(header)
    for row in rows:
        worksheet.append(row)
    return worksheet


def _template_workbook(**overrides):
    """A minimal but realistic filled template covering every sheet the adapter reads."""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)

    _sheet(workbook, "NewLineages", "One row per NEW lineage.",
           CANONICAL_HEADERS["NewLineages"],
           overrides.get("new_lineages", [
               ["TUMIG19", "MK493368, MK493369", "Haemoproteus", "Turdus migratorius",
                "12345", "Ellis et al 2026", "a comment"],
           ]))
    _sheet(workbook, "Sequences", "The sequence for every new lineage.",
           CANONICAL_HEADERS["Sequences"],
           overrides.get("sequences", [["TUMIG19", "ACGT acgt\nNNNN"]]))
    _sheet(workbook, "Reference", "The publication.",
           CANONICAL_HEADERS["Reference"],
           overrides.get("reference", [
               ["Ellis et al 2026", 2026, "A title", "A journal", "1", "10", "20",
                "10.1234/abc"],
           ]))
    _sheet(workbook, "Hosts_and_Sites", "The records themselves.",
           CANONICAL_HEADERS["Hosts_and_Sites"],
           overrides.get("hosts", [
               ["TUMIG19", "Turdus migratorius", "12345", None, "Adult", "Resident",
                "Wild", "Sweden", "Skane", "Lund", 3, 25, "Ellis et al 2026", None],
           ]))
    _sheet(workbook, "Sites", "One row per locality.",
           CANONICAL_HEADERS["Sites"],
           overrides.get("sites", [["Lund", "Sweden", "55°42.00000'", "013°11.40000'",
                                    "20"]]))
    _sheet(workbook, "Alt_Lineage_names", "Synonyms.",
           CANONICAL_HEADERS["Alt_Lineage_names"],
           overrides.get("alt_names", []))
    _sheet(workbook, "Vectors", "Lineages detected in vectors.",
           CANONICAL_HEADERS["Vectors"],
           overrides.get("vectors", []))
    return workbook


def _build(**overrides):
    return build_submission_from_workbook(_template_workbook(**overrides), "fixture.xlsx")


# ---------------------------------------------------------------------------------------
# The normalization contract
# ---------------------------------------------------------------------------------------

class TestNormalize:
    def test_blank_becomes_none_not_empty_string(self):
        # The whole reason this module exists: one path emitting "" where the other
        # emits None makes two identical submissions compare as different.
        assert normalize.text("   ") is None
        assert normalize.text("") is None
        assert normalize.text(None) is None

    def test_invisible_spaces_and_unicode_form_are_normalized(self):
        # A non-breaking space survives str.strip() and would make an otherwise
        # identical host name fail to match the release index.
        assert normalize.text("Turdus migratorius") == "Turdus migratorius"
        assert normalize.text("  Turdus   migratorius ​") == "Turdus migratorius"
        # NFC: a precomposed and a decomposed accent must become one string.
        assert normalize.text("Skåne") == normalize.text("Skåne")

    def test_lineage_names_are_upper_cased(self):
        assert normalize.lineage_name(" tumig19 ") == "TUMIG19"

    def test_accessions_split_on_any_separator(self):
        assert normalize.accession_list("MK1, MK2;MK3 MK4") == ["MK1", "MK2", "MK3", "MK4"]

    def test_accession_list_keeps_malformed_entries(self):
        # Normalization must not quietly drop a bad accession -- a check has to be able
        # to report it. Vanishing during normalization would look like it was never sent.
        assert normalize.accession_list("not-an-accession") == ["NOT-AN-ACCESSION"]

    def test_sequence_keeps_both_forms_and_preserves_gaps(self):
        submitted, cleaned = normalize.sequence_pair("acgt 123\nAC-GT")
        assert submitted == "acgt 123\nAC-GT"
        assert cleaned == "ACGTAC-GT"

    def test_counts_reject_non_integers(self):
        assert normalize.clean_count("25") == 25.0
        assert normalize.clean_count(3.5) is None      # 3.5 birds is a mis-parsed cell
        assert normalize.clean_count("30/47") is None
        assert normalize.clean_count(True) is None     # bool is an int subclass

    def test_record_change_only_records_real_changes(self):
        changes = []
        normalize.record_change(changes, "lineage_name", "TUMIG19", "TUMIG19")
        assert changes == []
        normalize.record_change(changes, "lineage_name", "tumig19", "TUMIG19")
        assert changes[0]["submitted"] == "tumig19"
        assert changes[0]["normalized"] == "TUMIG19"

    def test_record_change_ignores_blank_to_none(self):
        # An empty cell becoming None is the schema's own convention, not a change
        # anyone needs to review.
        changes = []
        normalize.record_change(changes, "country", "   ", None)
        assert changes == []


# ---------------------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------------------

class TestAdapter:
    def test_builds_a_schema_valid_submission(self):
        # build_submission_from_workbook validates by default; reaching here is the
        # assertion, but state it explicitly so a future reader sees the intent.
        submission = _build()
        assert submission["schema_version"] == SCHEMA_VERSION
        assert submission["provenance"]["source"] == "template"

    def test_reads_every_sheet(self):
        submission = _build()
        assert len(submission["records"]) == 1
        assert len(submission["sequences"]) == 1
        assert len(submission["proposed_lineages"]) == 1
        assert submission["reference"]["doi"] == "10.1234/abc"
        assert submission["reference"]["year"] == 2026
        assert submission["accessions"] == ["MK493368", "MK493369"]

    def test_publication_year_is_an_integer(self):
        # "2026.0" in a curator's report reads as a bug in our software, not a year.
        year = _build()["reference"]["year"]
        assert isinstance(year, int) and not isinstance(year, bool)

    def test_records_carry_their_worksheet_row(self):
        # The row number is what lets a finding say "Hosts_and_Sites, row 3" instead of
        # making the curator hunt. Row 1 is the note, row 2 the header, so data is row 3.
        source = _build()["records"][0]["source"]
        assert source == {"sheet": "Hosts_and_Sites", "row": 3, "file": "fixture.xlsx"}

    def test_coordinates_join_from_the_sites_sheet(self):
        # The template tells submitters SiteName must match SITE_NAME, so following it
        # is reading the workbook, not inferring from it.
        assert _build()["records"][0]["coordinates"] == "55°42.00000' 013°11.40000'"

    def test_coordinates_absent_when_the_site_does_not_resolve(self):
        submission = _build(sites=[["Somewhere else", "Sweden", "55°42.00000'",
                                    "013°11.40000'", "20"]])
        assert submission["records"][0]["coordinates"] is None

    def test_half_a_coordinate_pair_is_not_a_location(self):
        submission = _build(sites=[["Lund", "Sweden", "55°42.00000'", None, "20"]])
        assert submission["records"][0]["coordinates"] is None

    def test_parasite_genus_joins_from_new_lineages(self):
        # The records sheet has no genus column; the proposal sheet states it.
        assert _build()["records"][0]["parasite_genus"] == "Haemoproteus"

    def test_no_genus_invented_for_an_existing_lineage(self):
        # A row naming a lineage MalAvi already holds gets no genus from this workbook.
        # The release knows it; inventing one would put an unsourced value in the report.
        submission = _build(hosts=[
            ["SGS1", "Turdus migratorius", None, None, None, None, None, "Sweden", None,
             "Lund", None, None, "Ellis et al 2026", None],
        ])
        assert submission["records"][0]["parasite_genus"] is None

    def test_worked_example_rows_are_never_read_as_data(self):
        # The template ships a gray italic example and the READ ME says it may be left
        # in place. Reading it would invent a submission for Gupta et al 2019.
        submission = _build(new_lineages=[
            ["ALCPOI02", "MK493368", "Haemoproteus", "Alcippe poioicephala", "181645",
             "Gupta et al 2019", None],
            ["TUMIG19", "MK111111", "Plasmodium", "Turdus migratorius", None,
             "Ellis et al 2026", None],
        ])
        names = [entry["lineage_name"] for entry in submission["proposed_lineages"]]
        assert names == ["TUMIG19"]

    def test_a_real_record_for_a_common_lineage_survives(self):
        """Regression: SGS1 and GRW04 rows must not be mistaken for the worked example.

        The example filter used to match a handful of marker strings anywhere in a row,
        and two of those markers -- SGS1 and GRW04 -- are among the most-recorded
        lineages in MalAvi (571 and 187 host records in the 2026-03-23 release). Any
        submitter reporting a new SGS1 record had that row silently discarded.
        """
        submission = _build(hosts=[
            ["TUMIG19", "Turdus migratorius", None, None, None, None, None, "Sweden",
             None, "Lund", 1, 10, "Ellis et al 2026", None],
            ["SGS1", "Parus major", None, None, None, None, None, "Sweden", None,
             "Lund", 2, 10, "Ellis et al 2026", None],
            ["GRW04", "Acrocephalus arundinaceus", None, None, None, None, None,
             "Sweden", None, "Lund", 3, 10, "Ellis et al 2026", None],
        ])
        names = [record["lineage_name"] for record in submission["records"]]
        assert names == ["TUMIG19", "SGS1", "GRW04"]

    def test_a_real_record_for_the_example_lineage_survives_below_the_example(self):
        # ALCPOI02 is a real lineage too (7 host records). Only the row in the example's
        # own position is the example.
        submission = _build(hosts=[
            ["ALCPOI02", "Alcippe poioicephala", "181645", None, None, None, None,
             "India", None, "Ambalapara", None, None, "Gupta et al 2019", None],
            ["ALCPOI02", "Alcippe poioicephala", None, None, None, None, None,
             "Vietnam", None, "Cuc Phuong", 4, 12, "Ellis et al 2026", None],
        ])
        assert len(submission["records"]) == 1
        assert submission["records"][0]["country"] == "Vietnam"

    def test_an_edited_example_row_is_read_as_data(self):
        # A submitter who types over the example has made it their row.
        submission = _build(new_lineages=[
            ["TUMIG19", "MK111111", "Plasmodium", "Turdus migratorius", None,
             "Ellis et al 2026", None],
        ])
        assert len(submission["proposed_lineages"]) == 1

    def test_stray_rows_without_a_lineage_or_a_host_are_dropped(self):
        # A leftover formatted row would otherwise be tiered "incomplete" and put a
        # phantom in the curator's queue.
        submission = _build(hosts=[
            [None, None, None, None, None, None, None, None, None, None, None, None,
             None, "a note the submitter typed in the margin"],
        ])
        assert submission["records"] == []

    def test_lineage_case_is_normalized_and_reported(self):
        submission = _build(new_lineages=[
            ["tumig19", "MK493368", "Haemoproteus", "Turdus migratorius", None,
             "Ellis et al 2026", None],
        ])
        assert submission["proposed_lineages"][0]["lineage_name"] == "TUMIG19"
        changes = submission["provenance"]["normalizations"]
        assert any(c["submitted"] == "tumig19" and c["normalized"] == "TUMIG19"
                   for c in changes), "a semantic change must be reported, never silent"

    def test_template_columns_kept_that_the_schema_has_no_field_for(self):
        # Nothing submitted is discarded merely because MalAvi's submission schema has
        # no slot for it; it goes to notes where the curator can see it.
        notes = _build()["records"][0]["notes"]
        for expected in ("age: Adult", "status: Resident", "environment: Wild",
                         "region: Skane"):
            assert expected in notes

    def test_source_scope_is_not_asserted_for_a_template(self):
        # "Is this the paper's own data or reprinted?" is a question about extraction
        # from a publication. The workbook makes no such statement, so neither do we.
        assert _build()["records"][0]["source_scope"] is None

    def test_deterministic_apart_from_the_timestamp(self):
        first, second = _build(), _build()
        for submission in (first, second):
            submission["provenance"].pop("extracted_at")
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


class TestHeaderRepair:
    """A blank header cell over a column that still holds data."""

    def test_blank_header_is_recovered_by_position_and_reported(self):
        workbook = _template_workbook()
        # Blank the "HostSpecies" header exactly as the Shimizu 2026 submission did.
        workbook["NewLineages"].cell(row=2, column=4).value = None
        submission = build_submission_from_workbook(workbook, "fixture.xlsx")

        assert submission["proposed_lineages"][0]["host_species"] == "Turdus migratorius"
        repairs = submission["provenance"]["header_repairs"]
        assert len(repairs) == 1 and "HostSpecies" in repairs[0]

    def test_nothing_is_assumed_when_the_layout_differs(self):
        # If a submitter has reordered or renamed a column, positional recovery would
        # file one column's values under another column's name. Refuse instead.
        workbook = _template_workbook()
        worksheet = workbook["NewLineages"]
        worksheet.cell(row=2, column=4).value = None
        worksheet.cell(row=2, column=3).value = "SomethingElse"
        submission = build_submission_from_workbook(workbook, "fixture.xlsx")

        assert submission["proposed_lineages"][0]["host_species"] is None
        assert submission["provenance"]["header_repairs"] == []

    def test_canonical_headers_match_the_shipped_template(self):
        """The adapter's column contract must not drift from the generated template.

        This is what allows CANONICAL_HEADERS to be declared here rather than imported
        from the generator script: the two are kept honest by a test instead of by an
        import that would drag a build script into the package.
        """
        from pathlib import Path

        from malavi_curation.config import repo_root

        templates = sorted((repo_root() / "curation" / "templates").glob("*.xlsx"))
        if not templates:
            pytest.skip("no generated template on disk to compare against")

        shipped = openpyxl.load_workbook(templates[-1], data_only=True)
        for sheet_name, expected in CANONICAL_HEADERS.items():
            assert sheet_name in shipped.sheetnames, f"{sheet_name} missing from template"
            header, _body = sheet_rows(shipped[sheet_name], expected[0])
            assert header[:len(expected)] == expected, (
                f"{sheet_name} column order has drifted from CANONICAL_HEADERS")


class TestTemplateDetection:
    def test_a_supplement_is_not_a_template(self):
        # Supplementary spreadsheets travel with a submission and belong to the
        # table-extraction path. Adapting one yields an empty submission, and an empty
        # submission in the queue is a phantom for the curator to dismiss.
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        workbook.create_sheet("Table S1")
        workbook.create_sheet("Author Information")
        assert looks_like_template(workbook) is False

    def test_a_filled_template_is_a_template(self):
        assert looks_like_template(_template_workbook()) is True


# ---------------------------------------------------------------------------------------
# The contract between the two intake paths
# ---------------------------------------------------------------------------------------

class TestIntakeContract:
    """Equivalent content through either path must normalize to equivalent records.

    This is the exit criterion for the convergence work. If it fails, the checks that
    run on both paths are no longer answering the same question about the same fields,
    and every downstream guarantee weakens quietly rather than loudly.
    """

    # The fields both paths are expected to agree on. Fields that are legitimately
    # path-specific -- source location, provenance, tiers assigned later -- are excluded
    # deliberately rather than by oversight.
    SHARED_RECORD_FIELDS = ("lineage_name", "host_species", "country", "site",
                            "parasite_genus", "number_tested", "number_found")

    def test_same_facts_produce_the_same_record(self):
        from_template = _build()["records"][0]

        # The same row as the PDF path would deliver it: a structured table row.
        from_pdf = build_submission(
            reference={"doi": "10.1234/abc", "title": "A title", "year": 2026},
            structured_records=[{
                "lineage_name": "TUMIG19",
                "host_species": "Turdus migratorius",
                "country": "Sweden",
                "site": "Lund",
                "parasite_genus": "Haemoproteus",
                "number_tested": 25,
                "number_found": 3,
            }],
        )["records"][0]

        for field in self.SHARED_RECORD_FIELDS:
            assert from_template[field] == from_pdf[field], (
                f"the two intake paths disagree about {field!r}: "
                f"{from_template[field]!r} vs {from_pdf[field]!r}")

    def test_counts_are_the_same_type_from_both_paths(self):
        # A float from one path and an int from the other compares equal in Python but
        # serializes differently, which is exactly the kind of divergence that survives
        # a passing equality test and then surprises a reader of the JSON.
        from_template = _build()["records"][0]
        from_pdf = build_submission(
            reference={"doi": "10.1234/abc"},
            structured_records=[{"lineage_name": "TUMIG19",
                                 "host_species": "Turdus migratorius",
                                 "number_tested": 25, "number_found": 3}],
        )["records"][0]
        assert type(from_template["number_tested"]) is type(from_pdf["number_tested"])

    def test_both_paths_validate_against_one_schema(self):
        # Neither builder is allowed to be the only one that fits the contract.
        assert _build()["records"]
        assert build_submission(reference={"doi": "10.1234/abc"})["records"] == []
