"""Tests for enrollment — putting a fetched submission into the review ledger.

This step had no code at all until now, which is why it is worth being explicit about what
it must not do. Enrolling runs on a schedule, over the same submissions, forever. The ways
it can go wrong are all quiet:

* creating a second entry, splitting one submission's verdicts across two records;
* re-reading the screen over a name a curator already agreed to;
* resetting the arrival time, which is what name-reservation priority is decided on;
* moving a submission a curator has already picked up back to the queue.
"""
from __future__ import annotations

import json

import pytest

from malavi_curation import enrollment, ledger

SUBMISSION = "MALAVI-SUB-2026-000001"


def make_submission(tmp_path, name="20260806T210800_Testsubmission", *, screen=True,
                    workbook=True, lineages=("TUMIG31",), suggestions=None,
                    timestamp="08/06/2026 21:08:00"):
    """A submission directory as the intake leaves one."""
    sub_dir = tmp_path / name
    sub_dir.mkdir()
    (sub_dir / "metadata.json").write_text(json.dumps({"Timestamp": timestamp}))
    if screen:
        report = {
            "workbook": "ImportMalavi_Test.xlsx" if workbook else None,
            "lineages": {n: {} for n in lineages},
            "name_suggestions": suggestions or {},
        }
        (sub_dir / "screen.json").write_text(json.dumps([report]))
    return sub_dir


# ---------------------------------------------------------------- the ordinary path

def test_a_screened_submission_is_enrolled_and_put_in_front_of_a_curator(tmp_path):
    sub_dir = make_submission(tmp_path)
    entries = {}

    outcome = enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    assert outcome["created"] is True
    assert entries[SUBMISSION].state == "ready_for_review"
    assert entries[SUBMISSION].reserved_names == ["TUMIG31"]


def test_an_unscreened_submission_stays_in_the_queue(tmp_path):
    """It has not failed anything; nothing has looked at it yet."""
    sub_dir = make_submission(tmp_path, screen=False)
    entries = {}

    enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    assert entries[SUBMISSION].state == "received"
    assert entries[SUBMISSION].reserved_names == []


def test_a_screen_that_read_no_workbook_is_a_failure_not_a_pass(tmp_path):
    """A submission whose checks could not run must not look like one that passed them."""
    sub_dir = make_submission(tmp_path, workbook=False)
    entries = {}

    enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    assert entries[SUBMISSION].state == "screening_failed"


def test_a_suggested_name_is_carried_as_a_correction(tmp_path):
    """What the curator was shown is what the ledger must reserve."""
    sub_dir = make_submission(tmp_path, lineages=("TUMIG10",),
                              suggestions={"TUMIG10": "TUMIG32"})
    entries = {}

    enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    entry = entries[SUBMISSION]
    assert entry.reserved_names == ["TUMIG10"]
    # agreed_names is what a release and the public feed read: the corrected name.
    assert ledger.agreed_names(entry) == ["TUMIG32"]


# ---------------------------------------------------------------- idempotence

def test_re_enrolling_does_not_create_a_second_entry(tmp_path):
    sub_dir = make_submission(tmp_path)
    entries = {}

    enrollment.enroll_one(entries, SUBMISSION, sub_dir)
    second = enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    assert second["created"] is False
    assert len(entries) == 1
    assert len(entries[SUBMISSION].revisions) == 1


def test_re_enrolling_does_not_move_a_submission_a_curator_has_picked_up(tmp_path):
    """The daily job must not undo curation by walking the same folder again."""
    sub_dir = make_submission(tmp_path)
    entries = {}
    enrollment.enroll_one(entries, SUBMISSION, sub_dir)
    ledger.transition(entries[SUBMISSION], "in_review", "alice",
                      at="2026-08-07T00:00:00+00:00")

    enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    assert entries[SUBMISSION].state == "in_review"


def test_re_enrolling_does_not_rewrite_names_a_curator_has_agreed_to(tmp_path):
    """Once approved, the reserved name is an agreement, not a reading of a file.

    A re-screen against a newer release can suggest a different free name. Letting that
    overwrite an agreed name would change what MalAvi publishes with nobody deciding it.
    """
    sub_dir = make_submission(tmp_path)
    entries = {}
    enrollment.enroll_one(entries, SUBMISSION, sub_dir)
    entry = entries[SUBMISSION]
    entry.name_state = "held"

    # A later screen, against a newer release, proposes something different.
    (sub_dir / "screen.json").write_text(json.dumps(
        [{"workbook": "x.xlsx", "lineages": {"TUMIG99": {}}, "name_suggestions": {}}]))
    enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    assert entry.reserved_names == ["TUMIG31"]


# ---------------------------------------------------------------- the arrival time

def test_the_arrival_time_keeps_its_full_precision(tmp_path):
    """Priority is decided by the earliest Form timestamp.

    Truncating to a date would tie every submission filed on one day, and the tie would
    then be broken by whichever directory happened to sort first — a priority claim
    decided by a filename.
    """
    sub_dir = make_submission(tmp_path, timestamp="08/06/2026 21:08:00")
    assert enrollment.received_at(sub_dir) == "2026-08-06T21:08:00+00:00"


def test_an_iso_timestamp_is_read_too(tmp_path):
    sub_dir = make_submission(tmp_path, timestamp="2026-08-06 21:08:00")
    assert enrollment.received_at(sub_dir) == "2026-08-06T21:08:00+00:00"


def test_a_missing_timestamp_falls_back_to_the_directory_name_not_to_now(tmp_path):
    """Falling back to "now" would reset every submission's clock on every run."""
    sub_dir = make_submission(tmp_path, name="20260806T210800_Test", timestamp="")
    assert enrollment.received_at(sub_dir) == "2026-08-06T21:08:00+00:00"


def test_re_enrolling_does_not_move_the_arrival_time(tmp_path):
    sub_dir = make_submission(tmp_path)
    entries = {}
    enrollment.enroll_one(entries, SUBMISSION, sub_dir)
    original = entries[SUBMISSION].received_at

    (sub_dir / "metadata.json").write_text(json.dumps({"Timestamp": "09/01/2026 10:00:00"}))
    enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    assert entries[SUBMISSION].received_at == original


# ---------------------------------------------------------------- damaged input

def test_an_unreadable_screen_is_a_failed_screen_not_a_crash(tmp_path):
    sub_dir = make_submission(tmp_path)
    (sub_dir / "screen.json").write_text("{not json")

    entries = {}
    enrollment.enroll_one(entries, SUBMISSION, sub_dir)

    assert entries[SUBMISSION].state == "screening_failed"
    assert entries[SUBMISSION].reserved_names == []


def test_the_shared_readers_agree_with_the_reservation_feed(tmp_path):
    """The ledger and the public feed must reserve the same names.

    Both read screen.json, and they must read it the same way — a submission that claims
    TUMIG31 publicly and something else internally is the failure this shared module
    exists to prevent.
    """
    import importlib.util

    from malavi_curation.config import repo_root
    path = repo_root() / "curation" / "build_name_reservations.py"
    spec = importlib.util.spec_from_file_location("_build_reservations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    sub_dir = make_submission(tmp_path, lineages=("TUMIG10", "TUMIG11"),
                              suggestions={"TUMIG10": "TUMIG32"})

    assert sorted(module.claims_in(sub_dir)) == sorted(enrollment.claimed_names(sub_dir))
    assert module.suggestions_in(sub_dir) == enrollment.name_suggestions(sub_dir)
