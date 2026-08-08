"""Tests for the opaque identifiers and the issue digest.

These are privacy tests before they are anything else. A GitHub issue body is emailed to
every watcher, and that copy cannot be deleted, redacted or recalled by anyone. So the
question these tests answer is not "does the digest look right" but **"can anything from
the submission reach a mail server?"**

The central test feeds a submission whose every field is a distinctive string and asserts
that none of them survives into the title, the body or the metadata. It uses
``sensitive_values()`` rather than a hand-written list, so a new sensitive field added to
the submission schema is covered by this test the day it appears, instead of waiting for
somebody to remember.
"""
from __future__ import annotations

import json
import re

import pytest

from malavi_curation import submission_id as sid
from malavi_curation.checks import (
    CHECKS, CheckResult, CheckRun, Finding, Outcome, Severity,
)
from malavi_curation.issue_digest import (
    CHECKLIST, DIGEST_FIELDS, build_digest, render_issue_body, render_issue_title,
    sensitive_values,
)

OPAQUE = "MALAVI-SUB-2026-000123"

# A submission in which every value is unmistakable, so a leak cannot hide in a common
# word. Deliberately shaped like a real unpublished submission: that is the case that
# matters, because published data leaking is an embarrassment and unpublished data
# leaking is somebody's paper.
LOUD = {
    "schema_version": "1.0.0",
    "submitter": {"name": "Zzsubmitter Qqname", "email": "zzleak@example.invalid",
                  "institution": "Qqinstitute of Zzscience"},
    "reference": {"title": "Zztitle of an unpublished manuscript", "doi": "10.9999/zzdoi",
                  "year": 2026},
    "accessions": ["ZZ999888"],
    "records": [{"lineage_name": "ZZLIN01", "host_species": "Zzbirdus qqspecies",
                 "country": "Zzcountria", "site": "Qqvillage",
                 "notes": "zznote", "number_found": 9.0, "number_tested": 3.0}],
    "vectors": [{"lineage_name": "ZZLIN01", "vector_species": "Qqmosquito zzpipiens"}],
    "sequences": [{"lineage_name": "ZZLIN01", "sequence": "ACGTZZACGT",
                   "sequence_clean": "ACGTZZACGT"}],
    "proposed_lineages": [{"lineage_name": "ZZLIN01", "host_species": "Zzbirdus qqspecies",
                           "accessions": ["ZZ999888"]}],
    "provenance": {"source": "template", "workbook": "Zzworkbook Qqname.xlsx"},
}


def _run(*results):
    return CheckRun(results=tuple(results), provenance={"release": "2026-03-23"})


def _finding_run():
    """A run with a blocking finding whose subject and message are both sensitive."""
    return _run(
        CheckResult(check_id="sequence_is_known_lineage", outcome=Outcome.FINDING,
                    evaluated=1,
                    findings=(Finding(subject="ZZLIN01",
                                      message="ZZLIN01: sequence is IDENTICAL to ZZKNOWN1",
                                      severity=Severity.BLOCKING),)),
        CheckResult(check_id="prevalence_sanity", outcome=Outcome.FINDING, evaluated=1,
                    findings=(Finding(subject="ZZLIN01 x Zzbirdus qqspecies",
                                      message="9 found of 3 tested",
                                      severity=Severity.BLOCKING),)),
        CheckResult(check_id="host_name_resolves", outcome=Outcome.SKIP,
                    skip_reason="Rscript is not on PATH"),
        CheckResult(check_id="sequence_qc", outcome=Outcome.PASS, evaluated=1, passed=1),
    )


# ---------------------------------------------------------------------------------------
# Opaque identifiers
# ---------------------------------------------------------------------------------------

class TestSubmissionIds:
    def test_minted_ids_carry_no_identity(self, tmp_path):
        minted = sid.submission_id_for(tmp_path, "20260727T233146_Vincenzo_Ellis")
        assert sid.is_opaque(minted)
        for fragment in ("Vincenzo", "Ellis", "20260727"):
            assert fragment not in minted

    def test_the_same_directory_always_gets_the_same_id(self, tmp_path):
        # The daily job runs over the same submissions every morning. Minting a new id
        # per run would detach every decision, reservation and issue that references it.
        first = sid.submission_id_for(tmp_path, "20260727T233146_A_Person")
        second = sid.submission_id_for(tmp_path, "20260727T233146_A_Person")
        assert first == second

    def test_different_directories_get_different_ids(self, tmp_path):
        a = sid.submission_id_for(tmp_path, "dir_a")
        b = sid.submission_id_for(tmp_path, "dir_b")
        assert a != b

    def test_ids_are_sortable_as_text(self, tmp_path):
        ids = [sid.submission_id_for(tmp_path, f"dir_{i}") for i in range(12)]
        assert ids == sorted(ids), "fixed-width numbering should keep ids text-sortable"

    def test_the_reverse_lookup_is_the_only_way_back(self, tmp_path):
        minted = sid.submission_id_for(tmp_path, "20260727T233146_A_Person")
        assert sid.directory_for(tmp_path, minted) == "20260727T233146_A_Person"
        assert sid.directory_for(tmp_path, "MALAVI-SUB-2026-999999") is None

    def test_issue_numbers_are_remembered(self, tmp_path):
        # This is what makes issue creation idempotent; without it the job opens a fresh
        # issue every morning and notifies every curator again each time.
        minted = sid.submission_id_for(tmp_path, "dir_a")
        assert sid.issue_link(tmp_path, minted) is None
        sid.record_issue(tmp_path, minted, 42)
        assert sid.issue_link(tmp_path, minted) == 42

    def test_an_unreadable_ledger_refuses_rather_than_re_minting(self, tmp_path):
        # Silently starting a fresh ledger would re-issue ids that existing records
        # already point at, which is worse than stopping.
        sid.submission_id_for(tmp_path, "dir_a")
        (tmp_path / "submission_ids.json").write_text("{ not json")
        with pytest.raises(ValueError, match="unreadable"):
            sid.submission_id_for(tmp_path, "dir_b")

    def test_recording_an_issue_for_an_unknown_id_raises(self, tmp_path):
        with pytest.raises(KeyError):
            sid.record_issue(tmp_path, "MALAVI-SUB-2026-000999", 1)


# ---------------------------------------------------------------------------------------
# The digest: nothing from the submission may reach it
# ---------------------------------------------------------------------------------------

class TestDigestPrivacy:
    def test_no_submitted_value_reaches_the_issue(self):
        """The test this module exists for.

        Every value in the submission is checked against the whole rendered issue —
        title, body and metadata. A hit here means that value would be mailed to every
        watcher, permanently, beyond any deletion.
        """
        digest = build_digest(OPAQUE, _finding_run(), received="2026-07-27",
                              report_url="https://drive.example/report")
        rendered = render_issue_title(digest) + "\n" + render_issue_body(digest)

        leaked = [value for value in sensitive_values(LOUD) if value in rendered]
        assert leaked == [], f"these submitted values reached the issue body: {leaked}"

    def test_finding_values_do_not_reach_the_issue(self):
        # The findings in the run name a lineage and a host. Only the check *id* may
        # appear; "one name collision" is triage, "ZZLIN01 is taken" is disclosure.
        body = render_issue_body(build_digest(OPAQUE, _finding_run()))
        assert "ZZLIN01" not in body
        assert "Zzbirdus" not in body
        assert "sequence_is_known_lineage" in body

    def test_an_unpublished_title_is_withheld(self):
        # An unpublished title is the submitter's to announce. Published is different.
        unpublished = build_digest(OPAQUE, _run(), reference_key="Zzsubmitter 2026",
                                   published=False)
        assert unpublished["reference"] is None
        published = build_digest(OPAQUE, _run(), reference_key="Ellis et al 2026",
                                 published=True)
        assert published["reference"] == "Ellis et al 2026"

    def test_the_received_time_is_trimmed_to_a_date(self):
        # The hour someone submitted is not triage information, and it narrows down who
        # they are more than a date does.
        digest = build_digest(OPAQUE, _run(), received="2026-07-27T23:31:46Z")
        assert digest["received"] == "2026-07-27"

    def test_an_intake_directory_name_is_refused_outright(self):
        # Coercing it would hide the mistake. Raising makes it impossible to ship.
        with pytest.raises(ValueError, match="not a minted submission id"):
            build_digest("20260727T233146_Vincenzo_Ellis", _run())

    def test_the_digest_has_exactly_the_allowed_fields(self):
        # An allowlist only works if nothing can be added without touching it.
        digest = build_digest(OPAQUE, _run())
        assert set(digest) == set(DIGEST_FIELDS)


# ---------------------------------------------------------------------------------------
# The digest: it must still be useful
# ---------------------------------------------------------------------------------------

class TestDigestContent:
    def test_counts_reach_the_curator(self):
        body = render_issue_body(build_digest(OPAQUE, _finding_run()))
        assert "Blocking findings" in body and "| 2 |" in body

    def test_blocking_checks_are_named_by_id_with_their_fixed_title(self):
        body = render_issue_body(build_digest(OPAQUE, _finding_run()))
        assert "`sequence_is_known_lineage`" in body
        assert CHECKS["sequence_is_known_lineage"].title in body

    def test_a_run_with_errors_warns_before_anything_else(self):
        run = _run(CheckResult(check_id="sequence_qc", outcome=Outcome.ERROR,
                               error="R exited 1"))
        body = render_issue_body(build_digest(OPAQUE, run))
        assert "could not run" in body
        assert body.index("could not run") < body.index("| Blocking findings")

    def test_the_error_text_itself_is_not_included(self):
        # An R traceback can echo the records it was handed.
        run = _run(CheckResult(check_id="sequence_qc", outcome=Outcome.ERROR,
                               error="R exited 1: could not find Zzbirdus qqspecies"))
        assert "Zzbirdus" not in render_issue_body(build_digest(OPAQUE, run))

    def test_the_checklist_is_progress_not_a_verdict(self):
        body = render_issue_body(build_digest(OPAQUE, _run()))
        for item in CHECKLIST:
            assert f"- [ ] {item}" in body

        # No checklist *item* may read as a decision. A shared checkbox carries no actor,
        # so an item called "Approve" would let anyone accept a submission anonymously.
        for item in CHECKLIST:
            assert not any(word in item.lower()
                           for word in ("approve", "accept", "decline", "reject", "hold"))

        # And the body has to say so, because a curator will otherwise assume the last
        # tick means they are done.
        assert "They do not approve anything" in body

    def test_the_metadata_carries_only_the_opaque_id(self):
        body = render_issue_body(build_digest(OPAQUE, _run(), report_revision=3))
        comment = re.search(r"<!--(.*?)-->", body, re.S).group(1)
        assert OPAQUE in comment
        assert "report-revision: 3" in comment
        assert "Vincenzo" not in comment

    def test_the_title_is_the_id_and_a_count(self):
        assert render_issue_title(build_digest(OPAQUE, _run())) == OPAQUE
        assert render_issue_title(build_digest(OPAQUE, _finding_run())) == (
            f"{OPAQUE} — 2 blocking")

    def test_the_report_link_appears_when_there_is_one(self):
        body = render_issue_body(build_digest(OPAQUE, _run(),
                                              report_url="https://drive.example/x"))
        assert "https://drive.example/x" in body
        without = render_issue_body(build_digest(OPAQUE, _run()))
        assert "not available yet" in without
