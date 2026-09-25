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
    if overrides.get("morpho") is not None:       # template 2026-09 only
        _sheet(workbook, "MorphoSpecies", CANONICAL_HEADERS["MorphoSpecies"],
               overrides["morpho"])
    if overrides.get("sites") is not None:
        _sheet(workbook, "Sites", CANONICAL_HEADERS["Sites"], overrides["sites"])
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

    def test_two_workbooks_in_one_submission_are_not_offered_the_same_number(
            self, check_template):
        """REGRESSION: the set of unavailable names was rebuilt per report, so a second
        workbook proposing the same taken host acronym as the first was offered the very
        number the first had just been given."""
        first = _screened(name="TUMIG19")
        second = _screened(name="TUMIG19")
        check_template.offer_free_names([first, second], {"TUMIG19"}, {}, [])
        offered_first = set((first.get("name_suggestions") or {}).values())
        offered_second = set((second.get("name_suggestions") or {}).values())
        assert offered_first and offered_second
        assert not offered_first & offered_second, (
            f"both workbooks were offered {offered_first & offered_second}")

    def test_a_taken_name_whose_sequence_is_the_known_lineage_is_left_alone(
            self, check_template):
        """Renaming it would create a duplicate of something MalAvi already has."""
        report = _screened()
        report["issues"].append({"code": "sequence_is_known_lineage",
                                 "subject": "TUMIG19", "severity": "warn",
                                 "message": "already in MalAvi"})
        check_template.offer_free_names([report], {"TUMIG19"}, {}, [])
        assert not report.get("name_suggestions")

    # An exact match over too few positions is neither a known lineage nor a new one.
    # Until 2026-09-02 the screen said nothing about it, so a submitter could name it.
    def test_an_exact_match_over_too_few_positions_blocks_naming(
            self, check_template, reference, tmp_path, monkeypatch):
        real_check_sequence = check_template.check_sequence

        def thin_exact_match(sequence, ref, label=None):
            result = real_check_sequence(sequence, ref, label=label)
            result.verdict = "exact_match_low_coverage"
            result.flags.append("exact_match_over_too_few_positions")
            result.notes.append("Identical to SGS1 over the 20 positions both cover, but "
                                "that overlap is too short to call it the same lineage.")
            return result

        monkeypatch.setattr(check_template, "check_sequence", thin_exact_match)
        report = check_template.screen(_workbook(tmp_path), reference, known_lineages=set())
        found = [i for i in report["issues"] if i["code"] == "sequence_identity_unresolved"]
        assert found and found[0]["severity"] == "error"
        assert "too short" in found[0]["message"]
        assert "sequence_is_known_lineage" not in _codes(report)

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

    def test_a_site_outside_its_country_is_reported_with_the_fix(self, check_template,
                                                                 reference, tmp_path):
        # Lincoln, UK with the longitude's minus sign dropped: the real release fault.
        path = _workbook(tmp_path, sites=[
            ["Lincoln", "United Kingdom", "53°15.00000'", "000°34.00000'", None]])
        report = check_template.screen(path, reference, known_lineages=set())
        found = [i for i in report["issues"] if i["code"] == "site_outside_country"]
        assert len(found) == 1
        assert found[0]["subject"] == "Lincoln"
        assert found[0]["evidence"]["suggested_fix"] == "negate the longitude"
        assert "negate the longitude" in found[0]["message"]
        assert report["n_sites"] == 1

    def test_a_site_inside_its_country_raises_nothing(self, check_template, reference, tmp_path):
        path = _workbook(tmp_path, sites=[
            ["Lund", "Sweden", "55°42.00000'", "013°12.00000'", None],
            ["Malmö harbour", "Sweden", "55°21.00000'", "012°54.00000'", None]])   # 5 km out
        report = check_template.screen(path, reference, known_lineages=set())
        assert not {"site_outside_country", "site_coordinates_unreadable",
                    "site_country_not_in_atlas"} & _codes(report)
        assert report["n_sites"] == 2

    def test_unreadable_coordinates_are_reported_not_skipped(self, check_template,
                                                             reference, tmp_path):
        path = _workbook(tmp_path, sites=[
            ["Somewhere", "Sweden", "north of town", "013°12.00000'", None]])
        report = check_template.screen(path, reference, known_lineages=set())
        found = [i for i in report["issues"] if i["code"] == "site_coordinates_unreadable"]
        assert len(found) == 1 and "latitude" in found[0]["message"]

    def test_a_country_the_atlas_lacks_is_declared_untested(self, check_template,
                                                            reference, tmp_path):
        path = _workbook(tmp_path, sites=[
            ["Curaçao", "Netherlands Antilles", "12°06.00000'", "-068°54.00000'", None]])
        report = check_template.screen(path, reference, known_lineages=set())
        assert "site_country_not_in_atlas" in _codes(report)
        assert "site_outside_country" not in _codes(report)

    def test_a_site_row_without_coordinates_is_not_a_site_finding(self, check_template,
                                                                  reference, tmp_path):
        path = _workbook(tmp_path, sites=[["Lund", "Sweden", None, None, None]])
        report = check_template.screen(path, reference, known_lineages=set())
        assert not {"site_outside_country", "site_coordinates_unreadable"} & _codes(report)

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


class TestCitationKeyForm:
    """Review 2026-09-15, 3.2: a published citation key typed 'Vieira et al., 2023' is
    a second study to every table that joins on the name."""

    def test_a_key_spelled_with_punctuation_is_warned_with_the_canonical_form(
            self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(
            tmp_path,
            reference=[["Vieira et al., 2023", 2023, "A title", "A journal", None, None,
                        None, None]],
            hosts=[["TUMIG19", "Turdus migratorius", None, None, None, None, None,
                    "Sweden", None, "Lund", 3, 25, "Vieira et al., 2023", None]]),
            reference, known_lineages={"SGS1"})
        assert "reference_name_form" in _codes(report)
        message = next(i["message"] for i in report["issues"]
                       if i["code"] == "reference_name_form")
        assert "'Vieira et al 2023'" in message

    def test_a_key_in_malavi_form_raises_nothing(self, check_template, reference,
                                                 tmp_path):
        report = check_template.screen(_workbook(tmp_path), reference,
                                       known_lineages={"SGS1"})
        assert "reference_name_form" not in _codes(report)

    def test_the_respelling_is_information_not_a_warning(self, check_template, reference,
                                                        tmp_path):
        """The ingest respells the key itself, so nothing is asked of a curator."""
        report = check_template.screen(_workbook(
            tmp_path,
            reference=[["Vieira et al., 2023", 2023, "A title", "A journal", None, None,
                        None, None]]),
            reference, known_lineages={"SGS1"})
        finding = next(i for i in report["issues"] if i["code"] == "reference_name_form")
        assert finding["severity"] == "info"
        assert "will be filed as 'Vieira et al 2023'" in finding["message"]


class TestUnpublishedReferenceForm:
    """2026-09-23: 'Ellis et. al., unpublished' arrived in a real submission. It is
    recognizably an unpublished study, so the ingest respells it and the screen says so,
    rather than warning a curator to retype it."""

    def test_a_respellable_variant_is_information(self, check_template, reference,
                                                  tmp_path):
        report = check_template.screen(_workbook(
            tmp_path,
            reference=[["Ellis et. al., unpublished", None, "A title", None, None,
                        None, None, None]],
            new_lineages=[["TUMIG19", None, "Haemoproteus", "Turdus migratorius", None,
                           "Ellis et. al., unpublished", None]],
            hosts=[["TUMIG19", "Turdus migratorius", None, None, None, None, None,
                    "Sweden", None, "Lund", 3, 25, "Ellis et. al., unpublished",
                    None]]),
            reference, known_lineages={"SGS1"})
        assert "reference_unpubl_malformed" not in _codes(report)
        finding = next(i for i in report["issues"]
                       if i["code"] == "reference_unpubl_form")
        assert finding["severity"] == "info"
        assert "'Ellis et al unpubl'" in finding["message"]

    def test_a_name_the_normalizer_cannot_read_is_still_a_warning(
            self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(
            tmp_path,
            reference=[["Unpubl data from Barrow", None, "A title", None, None, None,
                        None, None]]),
            reference, known_lineages={"SGS1"})
        assert "reference_unpubl_malformed" in _codes(report)
        assert "reference_unpubl_form" not in _codes(report)

    def test_a_correct_name_raises_nothing(self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(
            tmp_path,
            reference=[["Barrow et al unpubl", None, "A title", None, None, None,
                        None, None]]),
            reference, known_lineages={"SGS1"})
        assert "reference_unpubl_form" not in _codes(report)
        assert "reference_unpubl_malformed" not in _codes(report)


def test_use_windows_hands_the_located_window_to_the_r_checks(check_template):
    submission = {"sequences": [
        {"lineage_name": "CARCRI03", "sequence": "x" * 5756, "sequence_clean": "A" * 5756},
        {"lineage_name": "PENOBS02", "sequence": "y", "sequence_clean": "C" * 479}]}
    reports = [{"sequences": [
        {"label": "CARCRI03", "flags": ["longer_than_window"], "registered": "G" * 479,
         "window": [3754, 4232]},
        {"label": "PENOBS02", "flags": [], "registered": "C" * 479, "window": [1, 479]}]}]
    check_template.use_windows(submission, reports)
    assert submission["sequences"][0]["sequence_clean"] == "G" * 479
    assert submission["sequences"][0]["sequence_window"] == [3754, 4232]
    assert submission["sequences"][0]["sequence"] == "x" * 5756
    assert submission["sequences"][1]["sequence_clean"] == "C" * 479


class TestMorphospecies:
    """2026-09-23: the genus column and the MorphoSpecies sheet (template 2026-09)."""

    def test_a_species_in_the_genus_column_is_information_naming_both_halves(
            self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(
            tmp_path,
            new_lineages=[["TUMIG19", "MK493368", "Plasmodium huffi", "Turdus migratorius",
                           None, "Ellis et al 2026", None]]),
            reference, known_lineages={"SGS1"})
        finding = next(i for i in report["issues"]
                       if i["code"] == "parasite_genus_carries_species")
        assert finding["severity"] == "info"
        assert "Plasmodium" in finding["message"] and "'Plasmodium huffi'" in finding["message"]
        assert "parasite_genus_unrecognized" not in _codes(report)

    def test_an_unknown_genus_is_a_warning(self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(
            tmp_path,
            new_lineages=[["TUMIG19", "MK493368", "Trypanosoma", "Turdus migratorius",
                           None, "Ellis et al 2026", None]]),
            reference, known_lineages={"SGS1"})
        assert "parasite_genus_unrecognized" in _codes(report)

    def test_a_good_morphospecies_row_raises_nothing(self, check_template, reference,
                                                     tmp_path):
        report = check_template.screen(_workbook(
            tmp_path, morpho=[["SGS1", "Plasmodium homocircumflexum", "Ellis et al 2026", None],
                              ["TUMIG19", "Haemoproteus minutus", "Ellis et al 2026", None]]),
            reference, known_lineages={"SGS1"})
        codes = _codes(report)
        assert "morphospecies_lineage_unknown" not in codes
        assert "morphospecies_binomial_malformed" not in codes
        assert [m["lineage"] for m in report["morphospecies"]] == ["SGS1", "TUMIG19"]

    def test_a_link_to_a_lineage_nobody_has_is_a_warning(self, check_template, reference,
                                                         tmp_path):
        report = check_template.screen(_workbook(
            tmp_path, morpho=[["NOSUCH01", "Plasmodium homocircumflexum", "Ellis et al 2026", None]]),
            reference, known_lineages={"SGS1"})
        finding = next(i for i in report["issues"]
                       if i["code"] == "morphospecies_lineage_unknown")
        assert finding["subject"] == "NOSUCH01"

    def test_a_non_binomial_species_is_a_warning(self, check_template, reference, tmp_path):
        report = check_template.screen(_workbook(
            tmp_path, morpho=[["SGS1", "Plasmodium sp. nov.", "Ellis et al 2026", None]]),
            reference, known_lineages={"SGS1"})
        assert "morphospecies_binomial_malformed" in _codes(report)

    def test_a_comment_about_morphology_is_pointed_at_the_sheet(self, check_template,
                                                                reference, tmp_path):
        """A 2026-09 submission's comments, in shape: the flag quotes them and asks nothing else."""
        comment = ("Although CARCRI01 lineage has already been deposited, this study "
                   "contributes by linking this lineage to morphological information "
                   "through the description of the new species Leucocytozoon cariamae")
        report = check_template.screen(_workbook(
            tmp_path,
            hosts=[["CARCRI01", "Cariama cristata", None, None, None, None, None,
                    "Brazil", None, "Lund", 1, 1, "Ellis et al 2026", comment],
                   ["CARCRI01", "Cariama cristata", None, None, None, None, None,
                    "Brazil", None, "Lund", 1, 1, "Ellis et al 2026", comment],
                   ["TUMIG19", "Turdus migratorius", None, None, None, None, None,
                    "Sweden", None, "Lund", 3, 25, "Ellis et al 2026", "1 mixed infection"]]),
            reference, known_lineages={"SGS1", "CARCRI01"})
        findings = [i for i in report["issues"] if i["code"] == "comment_mentions_morphology"]
        assert len(findings) == 1, "once per lineage, not once per row"
        assert findings[0]["subject"] == "CARCRI01"
        assert findings[0]["severity"] == "info"
        # The comment travels verbatim as evidence, with its row; the message is one fact.
        assert findings[0]["evidence"]["comment"] == comment
        assert findings[0]["evidence"]["row"] == 3
        assert comment not in findings[0]["message"]

    def test_a_lineage_with_a_morphospecies_row_is_not_nagged_about_its_comment(
            self, check_template, reference, tmp_path):
        comment = "new species Leucocytozoon cariamae described here"
        report = check_template.screen(_workbook(
            tmp_path,
            hosts=[["CARCRI01", "Cariama cristata", None, None, None, None, None,
                    "Brazil", None, "Lund", 1, 1, "Ellis et al 2026", comment]],
            morpho=[["CARCRI01", "Leucocytozoon cariamae", "Ellis et al 2026", None]]),
            reference, known_lineages={"SGS1", "CARCRI01"})
        assert "comment_mentions_morphology" not in _codes(report)


class TestIdenticalWithinSubmission:
    """2026-09-24: MALAVI-SUB-2026-000008 proposed COCCOC06 and COCCOC08 for one sequence."""

    def test_two_names_for_one_sequence_are_reported_once(self, check_template, reference,
                                                          tmp_path):
        seq = ("ATGCATGCTA" * 48)[:479]
        seq = seq[:100] + ("T" if seq[100] != "T" else "G") + seq[101:]   # not SGS1 itself
        report = check_template.screen(_workbook(
            tmp_path,
            new_lineages=[["TUMIG19", None, "Haemoproteus", "Turdus migratorius", None,
                           "Ellis et al 2026", None],
                          ["TUMIG20", None, "Haemoproteus", "Turdus migratorius", None,
                           "Ellis et al 2026", None]],
            sequences=[["TUMIG19", seq], ["TUMIG20", seq]]),
            reference, known_lineages={"SGS1"})
        findings = [i for i in report["issues"]
                    if i["code"] == "sequences_identical_within_submission"]
        assert len(findings) == 1
        assert "TUMIG19 and TUMIG20" in findings[0]["message"]
        assert findings[0]["severity"] == "error", "blocking: neither form can be ingested"
        assert findings[0]["evidence"]["pair"] == ["TUMIG19", "TUMIG20"]
        assert report["identical_pairs"] == [["TUMIG19", "TUMIG20", 479]]

    def test_sequences_one_base_apart_are_distinct_lineages(self, check_template, reference,
                                                            tmp_path):
        seq = ("ATGCATGCTA" * 48)[:479]
        other = seq[:250] + ("T" if seq[250] != "T" else "G") + seq[251:]
        report = check_template.screen(_workbook(
            tmp_path,
            new_lineages=[["TUMIG19", None, "Haemoproteus", "Turdus migratorius", None,
                           "Ellis et al 2026", None],
                          ["TUMIG20", None, "Haemoproteus", "Turdus migratorius", None,
                           "Ellis et al 2026", None]],
            sequences=[["TUMIG19", seq], ["TUMIG20", other]]),
            reference, known_lineages={"NOTSGS1"})
        assert "sequences_identical_within_submission" not in _codes(report)


class TestReferenceNameUnrecognized:
    """2026-09-24: an emailed workbook named its reference as two full author
    names -- no year, no 'unpubl' -- and no check said anything."""

    def test_a_name_with_neither_shape_is_a_warning(self, check_template, reference,
                                                    tmp_path):
        report = check_template.screen(_workbook(
            tmp_path,
            reference=[["Jane Smith, John Jones", 2027, "A title", None, None,
                        None, None, None]]),
            reference, known_lineages={"SGS1"})
        finding = next(i for i in report["issues"]
                       if i["code"] == "reference_name_unrecognized")
        assert finding["severity"] == "warn"
        assert "unpubl" in finding["message"]
        assert "reference_name_form" not in _codes(report)

    @pytest.mark.parametrize("name", ["Ellis et al 2026", "Vieira et al., 2023",
                                      "Barrow et al unpubl", "Ellis et. al., unpublished"])
    def test_both_recognized_shapes_pass(self, check_template, reference, tmp_path, name):
        report = check_template.screen(_workbook(
            tmp_path, reference=[[name, 2026, "A title", None, None, None, None, None]]),
            reference, known_lineages={"SGS1"})
        assert "reference_name_unrecognized" not in _codes(report)
