"""Failure-mode tests for the template screen and its end-to-end run.

The screen is the highest-stakes code in the intake path: it is what stops a sequence
already in MalAvi from being handed a new name, which would put a duplicate name into a
paper and into GenBank where it is very hard to undo. Until now none of it was tested.

These tests cover the things that actually go wrong with real workbooks — a submitter's
file is not a well-formed input — and the contract that ``screen.json`` must keep, because
the public name-reservation feed is built from it.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from malavi_curation.config import repo_root                          # noqa: E402
from malavi_curation.template_adapter import CANONICAL_HEADERS        # noqa: E402


@pytest.fixture(scope="module")
def check_template():
    """The screen module, loaded from the script it lives in."""
    path = repo_root() / "curation" / "check_template.py"
    spec = importlib.util.spec_from_file_location("_check_template", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    """A tiny but real reference alignment.

    Built through ``Reference.from_fasta`` rather than stubbed, so the screen runs its
    actual sequence path. The verdicts themselves are ``sequence_check``'s responsibility
    and are tested there against the whole release; what is under test here is everything
    around them — which rows are read, which issues are raised, what the report looks
    like — and that needs the real object, not a shape that resembles it.
    """
    from malavi_curation.sequence_check import Reference

    # Two synthetic 479 bp lineages. The window length is the one the whole project
    # encodes; a shorter one would exercise a different code path than production.
    first = ("ATGCATGCTA" * 48)[:479]
    second = first[:200] + ("G" if first[200] != "G" else "C") + first[201:]
    path = tmp_path_factory.mktemp("aln") / "reference.fasta"
    path.write_text(f">P_SGS1\n{first}\n>H_TESTLIN01\n{second}\n", encoding="utf-8")
    return Reference.from_fasta(path)


def _sheet(workbook, title, header, rows):
    worksheet = workbook.create_sheet(title)
    worksheet.append(["an instruction note"])
    worksheet.append(header)
    for row in rows:
        worksheet.append(row)


def _workbook(tmp_path, name="ImportMalavi.xlsx", **overrides):
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    _sheet(workbook, "NewLineages", CANONICAL_HEADERS["NewLineages"],
           overrides.get("new_lineages", [
               ["TUMIG19", "MK493368", "Haemoproteus", "Turdus migratorius", None,
                "Ellis et al 2026", None]]))
    _sheet(workbook, "Sequences", CANONICAL_HEADERS["Sequences"],
           overrides.get("sequences", [["TUMIG19", "ACGT"]]))
    _sheet(workbook, "Reference", CANONICAL_HEADERS["Reference"],
           overrides.get("reference", [
               ["Ellis et al 2026", 2026, "A title", "A journal", None, None, None,
                "10.1234/abc"]]))
    _sheet(workbook, "Hosts_and_Sites", CANONICAL_HEADERS["Hosts_and_Sites"],
           overrides.get("hosts", [
               ["TUMIG19", "Turdus migratorius", None, None, None, None, None,
                "Sweden", None, "Lund", 3, 25, "Ellis et al 2026", None]]))
    path = tmp_path / name
    workbook.save(path)
    return path


def _codes(report):
    return {issue["code"] for issue in report["issues"]}


class TestScreenIssues:
    def test_a_taken_name_is_a_warning_with_an_alternative(self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(tmp_path), reference,
                                       known_lineages={"TUMIG19"})
        assert "name_already_in_malavi" in _codes(report)
        assert any(i["severity"] == "warn" for i in report["issues"]
                   if i["code"] == "name_already_in_malavi")

    def test_a_free_name_raises_nothing(self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(tmp_path), reference,
                                       known_lineages={"SGS1"})
        assert "name_already_in_malavi" not in _codes(report)

    # A name is unavailable two ways, and they are different findings. "MalAvi already has
    # it" is settled. "An earlier submission asked for it and is still under review" is a
    # queue position, decided by arrival date -- and until this was wired in, the screen
    # could not see it at all: two submitters could each be told the same name was theirs.
    def test_a_name_another_submission_claimed_is_reported(self, check_template,
                                                           reference, tmp_path):
        report = check_template.screen(
            _workbook(tmp_path), reference, known_lineages={"SGS1"},
            claimed_elsewhere={"TUMIG19": "20260101T000000_Someone"})
        assert "name_claimed_by_another_submission" in _codes(report)
        assert "name_already_in_malavi" not in _codes(report), (
            "the release does not hold this name; saying so would send the submitter "
            "looking for it in a table that does not contain it")
        message = next(i["message"] for i in report["issues"]
                       if i["code"] == "name_claimed_by_another_submission")
        assert "20260101T000000_Someone" in message, "say which submission, so it can be checked"


def _screened(code="name_already_in_malavi", name="TUMIG19",
              host="Turdus migratorius"):
    """A screen report carrying one unavailable name, as offer_free_names expects it."""
    return {"issues": [{"code": code, "subject": name, "severity": "warn",
                        "message": f"{name} is not available."}],
            "lineages": {name: {"host_species": host}},
            "sequences": []}


class TestFreeNameSuggestions:
    """What gets offered in place of a name that is not available.

    Driven through offer_free_names directly rather than through a whole run: the thing
    it must never do is offer submission B a name submission A has already been offered,
    and that failure does not need a workbook to reach.
    """

    def test_a_claimed_name_is_not_offered_to_the_next_submitter(self, check_template):
        """The failure that mattered. Two submitters were each told the same name was
        theirs, and it surfaced at ingest -- possibly after one had used it in GenBank."""
        claimed = {"TUMIG20": "20260101T000000_Someone",
                   "TUMIG21": "20260101T000000_Someone"}
        report = _screened()
        check_template.offer_free_names([report], {"TUMIG19"}, claimed, [])
        offered = set((report.get("name_suggestions") or {}).values())
        assert offered, "a taken name should still get an alternative"
        assert not offered & set(claimed), (
            f"offered {offered}, which another submission has already claimed")

    def test_a_name_claimed_by_another_submission_still_gets_an_alternative(
            self, check_template):
        report = _screened(code="name_claimed_by_another_submission")
        check_template.offer_free_names(
            [report], {"SGS1"}, {"TUMIG19": "20260101T000000_Someone"}, [])
        assert report.get("name_suggestions") == {"TUMIG19": "TUMIG20"}

    def test_the_alternative_reaches_the_finding_a_curator_reads(self, check_template):
        """The suggestion used to be attached only to 'already in MalAvi' findings, so a
        name claimed by another submission was reported with no way forward."""
        report = _screened(code="name_claimed_by_another_submission")
        check_template.offer_free_names(
            [report], {"SGS1"}, {"TUMIG19": "20260101T000000_Someone"}, [])
        finding = report["issues"][0]
        assert "Suggesting TUMIG20" in finding["message"]

    def test_a_taken_name_whose_sequence_is_the_known_lineage_is_left_alone(
            self, check_template):
        """Renaming it would create a duplicate of something MalAvi already has."""
        report = _screened()
        report["issues"].append({"code": "sequence_is_known_lineage",
                                 "subject": "TUMIG19", "severity": "warn",
                                 "message": "already in MalAvi"})
        check_template.offer_free_names([report], {"TUMIG19"}, {}, [])
        assert not report.get("name_suggestions")

    def test_a_malformed_accession_is_reported(self, check_template, reference, tmp_path):
        path = _workbook(tmp_path, new_lineages=[
            ["TUMIG19", "not-an-accession", "Haemoproteus", "Turdus migratorius", None,
             "Ellis et al 2026", None]])
        report = check_template.screen(path, reference, known_lineages=set())
        assert "accession_malformed" in _codes(report)

    def test_a_declared_lineage_with_no_sequence_is_reported(self, check_template, reference,
                                                             tmp_path):
        path = _workbook(tmp_path, sequences=[])
        report = check_template.screen(path, reference, known_lineages=set())
        assert "lineage_without_sequence" in _codes(report)

    def test_a_sequence_with_no_declaration_is_reported(self, check_template, reference, tmp_path):
        path = _workbook(tmp_path, sequences=[["ORPHAN01", "ACGT"]])
        report = check_template.screen(path, reference, known_lineages=set())
        assert "sequence_without_declaration" in _codes(report)

    def test_a_record_with_no_country_is_reported(self, check_template, reference, tmp_path):
        path = _workbook(tmp_path, hosts=[
            ["TUMIG19", "Turdus migratorius", None, None, None, None, None,
             None, None, "Lund", 3, 25, "Ellis et al 2026", None]])
        report = check_template.screen(path, reference, known_lineages=set())
        assert "record_without_country" in _codes(report)

    def test_a_missing_reference_row_is_blocking(self, check_template, reference, tmp_path):
        path = _workbook(tmp_path, reference=[])
        report = check_template.screen(path, reference, known_lineages=set())
        assert "reference_missing" in _codes(report)

    def test_every_issue_carries_a_code(self, check_template, reference, tmp_path):
        """The registry keys on codes. An issue without one is invisible to it."""
        path = _workbook(tmp_path, sequences=[], reference=[], hosts=[])
        report = check_template.screen(path, reference, known_lineages={"TUMIG19"})
        assert report["issues"], "the fixture should raise several issues"
        for issue in report["issues"]:
            assert issue.get("code"), f"issue without a code: {issue}"
            assert issue.get("severity") in ("error", "warn", "info")
            assert issue.get("message")


class TestMalformedWorkbooks:
    """What a real submitter's file does, as opposed to a well-formed one."""

    def test_a_missing_sheet_does_not_raise(self, check_template, reference, tmp_path):
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        _sheet(workbook, "NewLineages", CANONICAL_HEADERS["NewLineages"], [])
        path = tmp_path / "partial.xlsx"
        workbook.save(path)
        report = check_template.screen(path, reference, known_lineages=set())
        assert report["workbook"] == "partial.xlsx"

    def test_a_sheet_with_no_header_row_yields_no_rows(self, check_template, reference, tmp_path):
        # A submitter who deleted the header has not submitted data we can read; the
        # right answer is to read nothing, not to guess which column is which.
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        worksheet = workbook.create_sheet("NewLineages")
        worksheet.append(["a note"])
        worksheet.append(["TUMIG19", "MK493368", "Haemoproteus"])
        path = tmp_path / "noheader.xlsx"
        workbook.save(path)
        report = check_template.screen(path, reference, known_lineages=set())
        assert report["lineages"] == {}

    def test_an_entirely_empty_workbook_does_not_raise(self, check_template, reference, tmp_path):
        workbook = openpyxl.Workbook()
        path = tmp_path / "empty.xlsx"
        workbook.save(path)
        report = check_template.screen(path, reference, known_lineages=set())
        assert report["lineages"] == {} and report["sequences"] == []

    def test_non_ascii_names_survive(self, check_template, reference, tmp_path):
        path = _workbook(tmp_path, hosts=[
            ["TUMIG19", "Turdus migratorius", None, None, None, None, None,
             "Sweden", "Skåne", "Lund", 3, 25, "Ellis et al 2026", None]])
        report = check_template.screen(path, reference, known_lineages=set())
        assert report["n_host_records"] == 1

    def test_duplicate_lineage_names_do_not_crash(self, check_template, reference, tmp_path):
        path = _workbook(tmp_path, new_lineages=[
            ["TUMIG19", "MK493368", "Haemoproteus", "Turdus migratorius", None,
             "Ellis et al 2026", None],
            ["TUMIG19", "MK493369", "Plasmodium", "Turdus migratorius", None,
             "Ellis et al 2026", None]])
        report = check_template.screen(path, reference, known_lineages=set())
        # Last one wins in the mapping, but nothing raises and the name is present.
        assert "TUMIG19" in report["lineages"]

    def test_a_very_long_cell_does_not_crash(self, check_template, reference, tmp_path):
        path = _workbook(tmp_path, new_lineages=[
            ["TUMIG19", "MK493368", "Haemoproteus", "T" * 10000, None,
             "Ellis et al 2026", None]])
        report = check_template.screen(path, reference, known_lineages=set())
        assert "TUMIG19" in report["lineages"]


class TestScreenJsonContract:
    """``screen.json``'s shape is load-bearing, and not only for this module."""

    def test_the_report_is_json_serializable(self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(tmp_path), reference,
                                       known_lineages=set())
        json.dumps(report)          # the daily workflow writes this to disk

    def test_the_lineages_key_is_what_the_reservation_feed_reads(self, check_template, reference,
                                                                 tmp_path):
        """build_name_reservations reads claimed names out of ``lineages``.

        Names are taken from here rather than by re-parsing the workbook, so that what
        is publicly reserved is exactly what a curator was shown. Renaming or reshaping
        this key silently stops names being reserved at all.
        """
        report = check_template.screen(_workbook(tmp_path), reference,
                                       known_lineages=set())
        assert isinstance(report["lineages"], dict)
        assert "TUMIG19" in report["lineages"]
        assert set(report["lineages"]["TUMIG19"]) >= {"genus", "accessions"}

    def test_reservation_builder_still_reads_that_key(self):
        # Stated as a test rather than a comment, because the coupling is invisible
        # from either file alone.
        #
        # The read moved on 2026-08-07: it now lives in malavi_curation.enrollment, which
        # the reservation feed and the review ledger both call, so the names advertised
        # publicly and the names reserved internally cannot drift apart. Both ends are
        # asserted -- that the parse still reads the key, and that the feed still routes
        # through the shared parse rather than growing a private copy again.
        shared = (repo_root() / "curation" / "src" / "malavi_curation"
                  / "enrollment.py").read_text()
        assert re.search(r'\["lineages"\]|\.get\("lineages"', shared), (
            "malavi_curation.enrollment no longer reads screen.json's 'lineages' key — "
            "check that names are still being reserved")

        feed = (repo_root() / "curation" / "build_name_reservations.py").read_text()
        assert "claimed_names" in feed, (
            "build_name_reservations no longer routes through the shared reader; the "
            "public feed and the review ledger can now reserve different names")


# ---------------------------------------------------------------------------------
# Regressions from the independent review of 2026-08-06.
# ---------------------------------------------------------------------------------

def test_a_lower_case_name_still_matches_a_taken_malavi_name(
        check_template, reference, tmp_path):
    """REGRESSION: `sgs1` did not match `SGS1`, so the blocking check did not fire.

    The highest-stakes check in the intake path, defeated by the shift key.
    """
    book = _workbook(tmp_path, new_lineages=[
        ["sgs1", "MK493368", "Plasmodium", "Turdus migratorius", None,
         "Ellis et al 2026", None]],
        sequences=[["sgs1", "ACGT"]])
    report = check_template.screen(book, reference, {"SGS1", "GRW04"})
    codes = [i["code"] for i in report["issues"]]
    assert "name_already_in_malavi" in codes, \
        "a lower-case spelling of a taken name must still be blocked"


def test_a_spaced_name_still_matches_a_taken_malavi_name(
        check_template, reference, tmp_path):
    book = _workbook(tmp_path, new_lineages=[
        ["SGS 1", "MK493368", "Plasmodium", "Turdus migratorius", None,
         "Ellis et al 2026", None]],
        sequences=[["SGS 1", "ACGT"]])
    report = check_template.screen(book, reference, {"SGS1"})
    assert "name_already_in_malavi" in [i["code"] for i in report["issues"]]


def test_casing_differences_across_sheets_are_one_lineage(
        check_template, reference, tmp_path):
    """REGRESSION: `TUMIG19` and `tumig19` were two keys, raising two spurious issues."""
    book = _workbook(tmp_path,
                     new_lineages=[["TUMIG19", "MK493368", "Haemoproteus",
                                    "Turdus migratorius", None, "Ellis et al 2026", None]],
                     sequences=[["tumig19", "ACGT"]])
    report = check_template.screen(book, reference, set())
    codes = [i["code"] for i in report["issues"]]
    assert "lineage_without_sequence" not in codes
    assert "sequence_without_declaration" not in codes
    assert list(report["lineages"]) == ["TUMIG19"]


def test_the_screen_reports_the_name_check_as_skipped_when_it_cannot_run(
        check_template, reference, tmp_path):
    """REGRESSION: a missing snapshot made the check report a clean PASS."""
    book = _workbook(tmp_path)
    report = check_template.screen(book, reference, None)
    skipped = {entry["code"] for entry in report.get("skipped", [])}
    assert "name_already_in_malavi" in skipped
    assert "name_already_in_malavi" not in [i["code"] for i in report["issues"]]


def test_a_skipped_name_check_becomes_a_skip_not_a_pass_in_the_registry():
    """The screen's skip must survive into checks.json, where a curator reads it."""
    from malavi_curation.checks import Outcome, run_checks

    screen_report = {"workbook": "ImportMalavi.xlsx", "sequences": [], "issues": [],
                     "lineages": {"TUMIG19": {}},
                     "skipped": [{"code": "name_already_in_malavi",
                                  "reason": "the snapshot has not been generated"}]}
    run = run_checks({"records": []}, screen=screen_report, run_r=False)
    result = next(r for r in run.results if r.check_id == "name_already_in_malavi")
    assert result.outcome is Outcome.SKIP
    assert "snapshot" in (result.skip_reason or "")


def test_a_taken_name_whose_sequence_is_the_existing_lineage_gets_no_rename(
        check_template, reference, tmp_path):
    """REGRESSION: the report gave two contradictory instructions about one lineage.

    Page one said "suggesting TUMIG25" while page two said "identical to H_TUMIG06 — do
    not assign a new name". Both cannot be right, and the second is correct: if the
    sequence IS the lineage that holds the name, this is a record of a known lineage, not
    a new one, and renaming it would duplicate something MalAvi already has.
    """
    from malavi_curation.checks import Outcome  # noqa: F401  (import parity with module)

    report = {
        "issues": [
            {"code": "name_already_in_malavi", "subject": "SGS1"},
            {"code": "sequence_is_known_lineage", "subject": "SGS1"},
        ],
        "lineages": {"SGS1": {"host_species": "Turdus migratorius"}},
    }
    already = {i["subject"] for i in report["issues"]
               if i["code"] == "sequence_is_known_lineage"}
    assert "SGS1" in already, "the screen must mark it as a known lineage"

    # The rule the fix encodes: a name in `already` is never offered a replacement.
    taken = {i["subject"] for i in report["issues"]
             if i["code"] == "name_already_in_malavi"}
    suggestible = taken - already
    assert suggestible == set()


def test_two_taken_names_do_not_receive_the_same_suggestion():
    """REGRESSION: each suggestion was computed against the release alone, so two taken
    names from the same host were both offered the identical free number."""
    from malavi_curation.naming import suggest_name

    known = ["TUMIG01", "TUMIG02"]
    claimed = set(known)

    first = suggest_name("Turdus migratorius", sorted(claimed))
    claimed.add(first.proposal)
    second = suggest_name("Turdus migratorius", sorted(claimed))

    assert first.proposal != second.proposal
    assert (first.proposal, second.proposal) == ("TUMIG03", "TUMIG04")
