"""Tests for the check registry.

The registry's value is not that it finds new problems -- it wraps checks that already
existed. Its value is that a curator can tell what ran, what did not, and why. So these
tests are mostly about the *reporting* contract:

* a check that could not run reports a skip, with a reason, and never a pass;
* a check that broke reports an error, and the run says validation is incomplete;
* every finding names a registered check and carries something a curator can act on;
* nothing submitted leaks into output meant for a log.

All fixtures are synthetic.
"""
from __future__ import annotations

import json

import pytest

from malavi_curation import checks
from malavi_curation.checks import (
    CHECKS, CheckResult, CheckRun, Finding, Outcome, Severity, render_console, run_checks,
)


def _submission(**overrides):
    """A minimal submission with one clean record."""
    submission = {
        "schema_version": "1.0.0",
        "submitter": {"name": "a submitter"},
        "reference": {"doi": "10.1234/abc", "pmid": None, "title": "A title",
                      "year": 2026},
        "accessions": ["MK493368"],
        "records": [{
            "lineage_name": "TUMIG19",
            "host_species": "Turdus migratorius",
            "country": "Sweden",
            "site": "Lund",
            "number_tested": 25.0,
            "number_found": 3.0,
            "source": {"sheet": "Hosts_and_Sites", "row": 3, "file": "fixture.xlsx"},
        }],
        "vectors": [],
        "sequences": [],
        "proposed_lineages": [],
        "provenance": {"source": "template", "normalizations": [], "header_repairs": []},
    }
    submission.update(overrides)
    return submission


def _run(**kwargs):
    """Run the checks without touching R -- the R validators have their own tests."""
    kwargs.setdefault("run_r", False)
    return run_checks(_submission(**kwargs.pop("submission_overrides", {})), **kwargs)


# ---------------------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------------------

class TestRegistry:
    def test_every_check_states_what_it_asserts(self):
        # The assertion is what a curator reads to understand a finding. A check with a
        # blank or unpunctuated one is a check nobody can act on.
        for check_id, check in CHECKS.items():
            assert check.asserts, f"{check_id} does not say what it asserts"
            assert check.asserts.endswith("."), f"{check_id}: assertion is not a sentence"
            assert check.title, f"{check_id} has no title"

    def test_severities_and_scopes_are_from_the_vocabulary(self):
        for check_id, check in CHECKS.items():
            assert isinstance(check.severity, Severity), check_id
            assert check.scope in ("submission", "record", "vector", "sequence", "name"), (
                f"{check_id} has an unknown scope {check.scope!r}")

    def test_row_flag_descriptions_come_from_row_flags(self):
        """The registry must not restate what a flag means; drift is the failure mode."""
        from malavi_curation.row_flags import FLAGS

        for code, meta in FLAGS.items():
            assert code in CHECKS, f"row flag {code} is not registered as a check"
            assert meta["description"] in CHECKS[code].asserts

    def test_check_ids_are_unique_and_stable_shaped(self):
        for check_id in CHECKS:
            assert check_id == check_id.lower().replace(" ", "_"), (
                f"{check_id} is not a stable snake_case id")


# ---------------------------------------------------------------------------------------
# Outcomes: the distinction the whole module exists for
# ---------------------------------------------------------------------------------------

class TestOutcomes:
    def test_every_result_names_a_registered_check(self):
        for result in _run().results:
            assert result.check_id in CHECKS or result.check_id == "screen_uncoded"

    def test_every_skip_carries_a_reason(self):
        # A skip without a reason is indistinguishable from a pass to a reader, which
        # is exactly how a validation gate quietly stops being one.
        for result in _run().results:
            if result.outcome is Outcome.SKIP:
                assert result.skip_reason, f"{result.check_id} skipped without a reason"

    def test_every_error_carries_a_message(self):
        for result in _run().results:
            if result.outcome is Outcome.ERROR:
                assert result.error, f"{result.check_id} errored without a message"

    def test_r_validators_report_a_skip_not_a_pass_when_not_run(self):
        run = _run(run_r=False)
        by_id = {r.check_id: r for r in run.results}
        for check_id in ("host_name_resolves", "host_geography_plausible"):
            assert by_id[check_id].outcome is Outcome.SKIP
            assert by_id[check_id].skip_reason

    def test_a_broken_check_is_an_error_and_makes_the_run_incomplete(self, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("the gate fell over")

        monkeypatch.setattr("malavi_curation.gate.run_gate", explode)
        run = _run()

        errors = [r for r in run.results if r.outcome is Outcome.ERROR]
        assert errors, "a check that raised must be reported as an error"
        assert "the gate fell over" in errors[0].error
        assert run.incomplete is True

    def test_a_clean_run_is_not_incomplete(self):
        assert _run().incomplete is False

    def test_r_failure_is_an_error_and_r_absence_is_a_skip(self, monkeypatch):
        from malavi_curation.validate import RValidation

        monkeypatch.setattr("malavi_curation.validate.run_validation",
                            lambda *a, **k: RValidation("skip", "Rscript is not on PATH"))
        by_id = {r.check_id: r for r in run_checks(_submission(), run_r=True).results}
        assert by_id["host_name_resolves"].outcome is Outcome.SKIP

        monkeypatch.setattr("malavi_curation.validate.run_validation",
                            lambda *a, **k: RValidation("error", "R exited 1"))
        by_id = {r.check_id: r for r in run_checks(_submission(), run_r=True).results}
        assert by_id["host_name_resolves"].outcome is Outcome.ERROR
        assert run_checks(_submission(), run_r=True).incomplete is True

    def test_prevalence_failure_is_found_and_is_blocking(self):
        run = _run(submission_overrides={"records": [{
            "lineage_name": "TUMIG19", "host_species": "Turdus migratorius",
            "country": "Sweden", "number_tested": 3.0, "number_found": 9.0,
        }]})
        by_id = {r.check_id: r for r in run.results}
        assert by_id["prevalence_sanity"].outcome is Outcome.FINDING
        assert CHECKS["prevalence_sanity"].severity is Severity.BLOCKING
        assert run.blocking, "a blocking finding must appear in run.blocking"

    def test_evaluated_counts_distinguish_nothing_checked_from_nothing_wrong(self):
        # "0 findings" over 0 rows and over 47 rows mean completely different things.
        run = _run()
        by_id = {r.check_id: r for r in run.results}
        assert by_id["prevalence_sanity"].evaluated == 1
        assert by_id["prevalence_sanity"].passed == 1

    def test_counts_add_up(self):
        run = _run()
        counts = run.counts()
        assert sum(counts[o.value] for o in Outcome) == len(run.results)
        assert counts["findings"] == sum(len(r.findings) for r in run.results)


# ---------------------------------------------------------------------------------------
# Screen translation
# ---------------------------------------------------------------------------------------

class TestScreenTranslation:
    SCREEN = [{
        "workbook": "fixture.xlsx",
        "sequences": [{"label": "TUMIG19"}],
        "lineages": {"TUMIG19": {}},
        "issues": [
            {"severity": "error", "code": "name_already_in_malavi",
             "subject": "TUMIG19", "message": "proposed name TUMIG19 is ALREADY taken."},
        ],
    }]

    def test_a_coded_issue_becomes_its_check(self):
        run = _run(screen=self.SCREEN)
        by_id = {r.check_id: r for r in run.results}
        assert by_id["name_already_in_malavi"].outcome is Outcome.FINDING
        assert by_id["name_already_in_malavi"].findings[0].subject == "TUMIG19"

    def test_a_screen_with_no_issues_still_reports_its_checks_as_passed(self):
        # "We looked and it was fine" is information. A check that vanishes when it
        # passes leaves a curator unable to tell it ever ran.
        run = _run(screen=[{"workbook": "fixture.xlsx", "sequences": [{"label": "X"}],
                            "lineages": {"X": {}}, "issues": []}])
        by_id = {r.check_id: r for r in run.results}
        assert by_id["sequence_is_known_lineage"].outcome is Outcome.PASS

    def test_an_uncoded_issue_is_reported_not_dropped(self):
        # An unrecognized finding is still a finding. Silently discarding one would be
        # the worst failure available to this module.
        run = _run(screen=[{"workbook": "fixture.xlsx", "sequences": [], "lineages": {},
                            "issues": [{"severity": "warn",
                                        "message": "something new and unclassified"}]}])
        by_id = {r.check_id: r for r in run.results}
        assert "screen_uncoded" in by_id
        assert by_id["screen_uncoded"].findings[0].message == (
            "something new and unclassified")

    def test_every_code_the_screen_emits_is_registered(self):
        """Regression guard against adding a screen issue with no matching check.

        Reads the codes straight out of check_template.py's source so a new one cannot
        be added without either registering it or failing here.
        """
        import re
        from pathlib import Path

        from malavi_curation.config import repo_root

        source = (repo_root() / "curation" / "check_template.py").read_text()
        emitted = set(re.findall(r'issue\(\s*"[a-z]+",\s*"([a-z_]+)"', source))
        assert emitted, "no issue codes found -- has check_template.py changed shape?"
        unregistered = emitted - set(CHECKS)
        assert not unregistered, f"screen codes with no registered check: {unregistered}"


# ---------------------------------------------------------------------------------------
# The R-backed checks
# ---------------------------------------------------------------------------------------

class TestRChecks:
    """Translation of what validate_record.R returns. R itself is not invoked here."""

    def _with_r_result(self, monkeypatch, result):
        from malavi_curation.validate import RValidation

        monkeypatch.setattr("malavi_curation.validate.run_validation",
                            lambda *a, **k: RValidation("ok", result=result))
        return {r.check_id: r for r in run_checks(_submission(), run_r=True).results}

    def test_a_previously_recorded_lineage_is_not_a_finding(self, monkeypatch):
        # Reporting every row that is perfectly ordinary would bury the ones that
        # are not. "Previously recorded" is the expected case.
        by_id = self._with_r_result(monkeypatch, {
            "plausibility": [{"lineage": "SGS1", "host": "Parus major",
                              "call": "previously_recorded", "n_studies": 40}],
        })
        assert by_id["lineage_previously_recorded"].outcome is Outcome.PASS

    def test_a_novel_host_is_reported_with_how_well_known_the_lineage_is(self, monkeypatch):
        # A new host family for a lineage known from 40 studies is unremarkable; one
        # for a lineage known from a single study is worth a hard look. The count is
        # what lets a curator tell those apart.
        by_id = self._with_r_result(monkeypatch, {
            "plausibility": [{"lineage": "SGS1", "host": "Necrosyrtes monachus",
                              "call": "new_host_and_location",
                              "flags": "new_host_species", "n_studies": 40,
                              "n_countries": 60}],
        })
        result = by_id["lineage_previously_recorded"]
        assert result.outcome is Outcome.FINDING
        assert "40 study/studies" in result.findings[0].message

    def test_no_na_counts_for_a_lineage_malavi_does_not_hold(self, monkeypatch):
        # The counts are undefined there; printing "NA studies" looks like a fault.
        by_id = self._with_r_result(monkeypatch, {
            "plausibility": [{"lineage": "NECMON01", "host": "Necrosyrtes monachus",
                              "call": "lineage_not_in_malavi", "flags": "",
                              "n_studies": None, "n_countries": None}],
        })
        assert "NA" not in by_id["lineage_previously_recorded"].findings[0].message

    def test_plausibility_stays_advisory(self):
        # It describes sampling, not biology. If this ever becomes blocking, it can
        # stop an ingest on the grounds that nobody has looked before -- which is the
        # misuse that got the function removed from malaviR in the first place.
        assert CHECKS["lineage_previously_recorded"].severity is Severity.INFO
        assert "SAMPLING" in CHECKS["lineage_previously_recorded"].asserts

    def test_a_clean_sequence_is_not_a_finding(self, monkeypatch):
        by_id = self._with_r_result(monkeypatch, {
            "sequence_qc": [{"lineage_name": "TUMIG19", "call": "plausible_new_lineage",
                             "score": 0.98}],
        })
        assert by_id["sequence_qc"].outcome is Outcome.PASS

    def test_an_identical_sequence_is_not_reported_twice(self, monkeypatch):
        # The screen already reports this as a blocking issue. Saying it again in a
        # second vocabulary is how a report becomes something a curator skims.
        by_id = self._with_r_result(monkeypatch, {
            "sequence_qc": [{"lineage_name": "TUMIG19", "call": "known_lineage"}],
        })
        assert by_id["sequence_qc"].outcome is Outcome.PASS

    def test_the_frame_explanation_leads_the_flag_list(self, monkeypatch):
        """A padded sequence produces nine alarming flags that share one cause.

        malaviR writes a message saying exactly that, and it must come first: the
        flags are consequences of the padding, not independent evidence.
        """
        explanation = ("The query translates with 2 stop codon(s) in frame 1, but is "
                       "stop-free in frame 2.")
        by_id = self._with_r_result(monkeypatch, {
            "sequence_qc": [{"lineage_name": "NECMON01",
                             "call": "invalid_or_strong_warning",
                             "flags": "contains_stop_codon; possible_chimera",
                             "message": explanation}],
        })
        message = by_id["sequence_qc"].findings[0].message
        assert message.startswith(explanation)
        assert message.index(explanation) < message.index("contains_stop_codon")

    def test_a_failure_inside_r_is_an_error_on_that_check_alone(self, monkeypatch):
        by_id = self._with_r_result(monkeypatch, {
            "host_taxonomy": [],
            "plausibility": [{"error": "lineage_plausibility failed: boom"}],
            "sequence_qc": [],
        })
        assert by_id["lineage_previously_recorded"].outcome is Outcome.ERROR
        # The other R-backed checks still reached the curator.
        assert by_id["host_name_resolves"].outcome is not Outcome.ERROR

    def test_every_r_backed_check_is_covered_by_the_skip_path(self):
        """Adding an R check without adding it to _R_CHECK_IDS would make it vanish
        exactly when R is unavailable -- the moment its absence matters most."""
        run = run_checks(_submission(), run_r=False)
        skipped = {r.check_id for r in run.results if r.outcome is Outcome.SKIP}
        assert set(checks._R_CHECK_IDS) <= skipped


# ---------------------------------------------------------------------------------------
# Adapter checks
# ---------------------------------------------------------------------------------------

class TestAdapterChecks:
    def test_a_normalization_is_reported(self):
        run = _run(submission_overrides={"provenance": {
            "source": "template",
            "normalizations": [{"field": "lineage_name", "submitted": "tumig19",
                                "normalized": "TUMIG19"}],
            "header_repairs": [],
        }})
        by_id = {r.check_id: r for r in run.results}
        assert by_id["values_normalized"].outcome is Outcome.FINDING

    def test_a_header_repair_is_reported(self):
        run = _run(submission_overrides={"provenance": {
            "source": "template", "normalizations": [],
            "header_repairs": ["NewLineages: column 4 had no header"],
        }})
        by_id = {r.check_id: r for r in run.results}
        assert by_id["headers_intact"].outcome is Outcome.FINDING


# ---------------------------------------------------------------------------------------
# Provenance and determinism
# ---------------------------------------------------------------------------------------

class TestProvenance:
    def test_the_run_records_what_it_read(self):
        provenance = _run().provenance
        for key in ("generated", "schema_version", "release", "tool_version", "online",
                    "submission_source"):
            assert key in provenance, f"provenance is missing {key}"
        assert provenance["schema_version"] == "1.0.0"

    def test_online_is_false_when_not_requested(self):
        assert _run().provenance["online"] is False

    def test_deterministic_apart_from_the_timestamp(self):
        first, second = _run().as_dict(), _run().as_dict()
        for payload in (first, second):
            payload["provenance"].pop("generated")
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_results_are_ordered_with_what_needs_attention_first(self):
        run = _run(submission_overrides={"records": [{
            "lineage_name": "TUMIG19", "host_species": "Turdus migratorius",
            "country": "Sweden", "number_tested": 3.0, "number_found": 9.0,
        }]})
        outcomes = [r.outcome for r in run.results]
        # No pass may appear before a finding: a curator reads from the top.
        first_pass = outcomes.index(Outcome.PASS) if Outcome.PASS in outcomes else len(outcomes)
        last_finding = max((i for i, o in enumerate(outcomes) if o is Outcome.FINDING),
                           default=-1)
        assert last_finding < first_pass


# ---------------------------------------------------------------------------------------
# Console output, and what must never reach a log
# ---------------------------------------------------------------------------------------

class TestConsoleRendering:
    def test_findings_name_their_subjects_by_default(self):
        run = _run(submission_overrides={"records": [{
            "lineage_name": "TUMIG19", "host_species": "Turdus migratorius",
            "country": "Sweden", "number_tested": 3.0, "number_found": 9.0,
        }]})
        assert "prevalence_sanity" in render_console(run)

    def test_redaction_withholds_every_submitted_value(self):
        """Actions logs are readable by every collaborator while a submission is not.

        The secret-bearing values here are the submitter's own science: an unpublished
        host species, locality and lineage name. None may appear in redacted output.
        """
        run = _run(submission_overrides={"records": [{
            "lineage_name": "SECRETLIN01",
            "host_species": "Zosterops borbonicus",
            "country": "Reunion",
            "site": "Le Maido",
            "number_tested": 3.0,
            "number_found": 9.0,
        }]})
        rendered = render_console(run, redact=True)
        for secret in ("SECRETLIN01", "Zosterops", "borbonicus", "Reunion", "Le Maido"):
            assert secret not in rendered, f"{secret!r} leaked into redacted output"
        # It must still be usable as an alarm.
        assert "prevalence_sanity" in rendered

    def test_redaction_withholds_error_detail_but_still_announces_it(self):
        # An R traceback can echo the records it was handed.
        run = CheckRun(
            results=(CheckResult(check_id="host_name_resolves", outcome=Outcome.ERROR,
                                 error="R exited 1: could not find Zosterops borbonicus"),),
            provenance={},
        )
        rendered = render_console(run, redact=True)
        assert "Zosterops" not in rendered
        assert "ERROR" in rendered and "host_name_resolves" in rendered

    def test_an_incomplete_run_says_so_at_the_top(self):
        run = CheckRun(
            results=(CheckResult(check_id="host_name_resolves", outcome=Outcome.ERROR,
                                 error="R exited 1"),),
            provenance={},
        )
        assert "INCOMPLETE" in render_console(run).splitlines()[0]

    def test_skip_reasons_are_shown_because_they_are_ours_not_the_submitter_s(self):
        rendered = render_console(_run(), redact=True)
        assert "skipped:" in rendered


# ---------------------------------------------------------------------------------
# Regressions from the independent review of 2026-08-06: three ways a check that could
# not run reported itself as a pass. "MalAvi does not have this" and "nobody looked"
# are opposite statements, and a green check asserts the first.
# ---------------------------------------------------------------------------------

def test_screen_checks_are_skipped_not_absent_when_no_screen_ran():
    from malavi_curation.checks import Outcome, _screen_checks, _SCREEN_CHECK_IDS

    results = _screen_checks(None)
    assert {r.check_id for r in results} == set(_SCREEN_CHECK_IDS)
    assert all(r.outcome is Outcome.SKIP for r in results)
    assert all(r.skip_reason for r in results), "a skip must always carry a reason"


def test_the_blocking_screen_checks_are_among_those_skipped():
    """The four that matter: absent, they read as a clean intake."""
    from malavi_curation.checks import _screen_checks

    skipped = {r.check_id for r in _screen_checks(None)}
    for check_id in ("sequence_is_known_lineage", "sequence_unplaceable",
                     "lineage_without_sequence", "reference_missing"):
        assert check_id in skipped


def test_index_backed_flags_are_skipped_when_the_release_is_not_on_disk(monkeypatch):
    """REGRESSION: an empty release index made four MalAvi lookups report PASS."""
    from malavi_curation import checks as checks_module
    from malavi_curation.checks import Outcome, _row_flag_checks
    from malavi_curation.row_flags import INDEX_BACKED_FLAGS

    def no_index(rows, kind="records"):
        return {"release_index_available": False}

    monkeypatch.setattr("malavi_curation.row_flags.flag_rows", no_index)

    submission = {"records": [{"lineage_name": "TUMIG19", "host_species": "Turdus migratorius"}]}
    results = {r.check_id: r for r in _row_flag_checks(submission)}
    for code in INDEX_BACKED_FLAGS:
        assert results[code].outcome is Outcome.SKIP, \
            f"{code} cannot pass when nothing was compared against MalAvi"
        assert "release tables" in (results[code].skip_reason or "")
