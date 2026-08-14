"""Tests for the promoter — the job that runs the review clocks.

The clocks themselves are tested in ``test_ledger.py``. What is pinned here is the
promoter's judgment about *which* proposals it may act on, because the tempting mistake is
to apply them all: ``due_actions`` returns a tidy list and applying the whole list is one
line shorter than applying part of it.

The two that must not be applied are release-eligibility (only a real release makes a
submission released, and the state is terminal) and staleness (nobody agreed a submission
should be closed because a curator was slow).
"""
from __future__ import annotations

import importlib.util
import json

import pytest
import yaml

from malavi_curation import ledger
from malavi_curation.config import repo_root


@pytest.fixture(scope="module")
def promote():
    path = repo_root() / "curation" / "promote.py"
    spec = importlib.util.spec_from_file_location("_promote", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGISTRY = [
    {"id": "lead", "name": "Lead Curator", "email": "lead@example.edu",
     "role": "lead", "active": True},
    {"id": "alice", "name": "Alice", "email": "alice@example.edu", "role": "curator"},
]

CONFIG = {"review": {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60,
                     "stale_review_days": 30},
          "submissions": {"inbox_dir": "inbox"}}

SUBMISSION = "MALAVI-SUB-2026-000001"


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "curators.yml"
    path.write_text(yaml.safe_dump({"curators": REGISTRY}), encoding="utf-8")
    return path


@pytest.fixture
def workspace(promote, tmp_path, monkeypatch):
    """A promoter pointed at a temporary repo root and inbox."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(promote, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(promote, "load_config", lambda: CONFIG)
    return tmp_path, inbox


def seed(inbox, build):
    """Write a ledger built by ``build(entries)`` into ``inbox``."""
    with ledger.open_ledger(inbox) as entries:
        build(entries)


# ---------------------------------------------------------------- what it applies

def test_an_expired_awaiting_submitter_timeout_goes_dormant_and_releases_the_names(
        promote, workspace, registry):
    _, inbox = workspace

    def build(entries):
        entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-01-01T00:00:00+00:00")
        entry.reserved_names = ["TUMIG31"]
        ledger.transition(entry, "ready_for_review", "intake", at="2026-01-01T01:00:00+00:00")
        ledger.transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")
    seed(inbox, build)

    assert promote.main(["--now", "2026-04-01T00:00:00+00:00"]) == 0

    entries = ledger.load(inbox)
    assert entries[SUBMISSION].state == "dormant"
    # The names going back is the point of the timeout, not a side effect.
    assert entries[SUBMISSION].name_state == "released"


def test_the_timeout_does_not_fire_early(promote, workspace, registry):
    _, inbox = workspace

    def build(entries):
        entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-01-01T00:00:00+00:00")
        ledger.transition(entry, "ready_for_review", "intake", at="2026-01-01T01:00:00+00:00")
        ledger.transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")
    seed(inbox, build)

    promote.main(["--now", "2026-02-01T00:00:00+00:00"])
    assert ledger.load(inbox)[SUBMISSION].state == "awaiting_submitter"


def test_an_embargoed_submission_never_times_out(promote, workspace, registry):
    """Unpublished work holds its names indefinitely — a settled governance decision."""
    _, inbox = workspace

    def build(entries):
        entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-01-01T00:00:00+00:00")
        entry.embargoed = True
        ledger.transition(entry, "ready_for_review", "intake", at="2026-01-01T01:00:00+00:00")
        ledger.transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")
    seed(inbox, build)

    promote.main(["--now", "2027-01-01T00:00:00+00:00"])
    assert ledger.load(inbox)[SUBMISSION].state == "awaiting_submitter"


# ---------------------------------------------------------------- what it refuses to apply

def test_a_release_eligible_submission_is_reported_but_never_marked_released(
        promote, workspace, registry, monkeypatch, capsys):
    """``released`` means published. Only a release build can make that true, and the
    state is terminal, so a wrong one cannot be taken back."""
    _, inbox = workspace
    monkeypatch.setattr(ledger, "registry_path", lambda: registry, raising=False)

    def build(entries):
        entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-08-01T00:00:00+00:00")
        ledger.transition(entry, "ready_for_review", "intake", at="2026-08-01T01:00:00+00:00")
        ledger.transition(entry, "in_review", "alice", at="2026-08-01T02:00:00+00:00")
        ledger.record_verdict(entry, "alice@example.edu", "approve",
                              at="2026-08-02T00:00:00+00:00", registry_path=registry)
        ledger.transition(entry, "approved", "alice", at="2026-08-02T00:00:00+00:00",
                          config=CONFIG)
    seed(inbox, build)

    promote.main(["--now", "2026-08-05T00:00:00+00:00"])

    assert ledger.load(inbox)[SUBMISSION].state == "approved"
    assert "[ready" in capsys.readouterr().out


def test_a_stale_held_submission_is_reported_not_closed(promote, workspace, registry,
                                                        capsys):
    """Nobody agreed a submission should be closed because a curator went quiet."""
    _, inbox = workspace

    def build(entries):
        entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-01-01T00:00:00+00:00")
        entry.reserved_names = ["TUMIG31"]
        ledger.transition(entry, "ready_for_review", "intake", at="2026-01-01T01:00:00+00:00")
        ledger.transition(entry, "in_review", "alice", at="2026-01-01T02:00:00+00:00")
    seed(inbox, build)

    # Exit 2: something wants a person, which is not the same as the job failing.
    assert promote.main(["--now", "2026-06-01T00:00:00+00:00"]) == 2

    entries = ledger.load(inbox)
    assert entries[SUBMISSION].state == "in_review"
    assert entries[SUBMISSION].name_state == "claimed"
    assert "[stale" in capsys.readouterr().out


def test_a_submitter_who_replies_before_the_promoter_runs_is_not_made_dormant(
        promote, workspace, registry):
    """The scan proposes; the write re-checks. This is that gap, exercised."""
    _, inbox = workspace

    def build(entries):
        entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-01-01T00:00:00+00:00")
        ledger.transition(entry, "ready_for_review", "intake", at="2026-01-01T01:00:00+00:00")
        ledger.transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")
        # The reply landed: back into review, which clears awaiting_since.
        ledger.transition(entry, "in_review", "alice", at="2026-03-01T00:00:00+00:00")
    seed(inbox, build)

    promote.main(["--now", "2026-04-01T00:00:00+00:00"])
    assert ledger.load(inbox)[SUBMISSION].state == "in_review"


# ---------------------------------------------------------------- the decision record

def test_the_decision_record_is_written_and_holds_no_free_prose(promote, workspace,
                                                                registry):
    """Its whole premise is that it can be committed while the ledger cannot.

    A reason *code* is a closed vocabulary; the reason *text* quotes somebody's unpublished
    data. If the text ever reached this file, the one record meant to survive an erasure
    would be the one that made the erasure incomplete.

    The record holds *dispositions*, so this drives one all the way to a disposition — the
    promoter timing the submission out — rather than asserting against work in progress.
    """
    root, inbox = workspace

    def build(entries):
        entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-01-01T00:00:00+00:00")
        entry.reserved_names = ["TUMIG31"]
        ledger.transition(entry, "ready_for_review", "intake", at="2026-01-01T01:00:00+00:00")
        ledger.transition(entry, "in_review", "alice", at="2026-01-01T02:00:00+00:00")
        ledger.record_verdict(entry, "alice@example.edu", "hold",
                              reason_code="host_needs_review",
                              reason_text="Turdus migratoria is not a species name.",
                              at="2026-01-02T00:00:00+00:00", registry_path=registry)
        ledger.transition(entry, "awaiting_submitter", "alice",
                          at="2026-01-02T01:00:00+00:00")
    seed(inbox, build)

    promote.main(["--now", "2026-06-01T00:00:00+00:00"])
    assert ledger.load(inbox)[SUBMISSION].state == "dormant"

    written = (root / promote.DECISION_RECORD).read_text()
    record = json.loads(written)["decisions"][0]

    # What it must keep: the identity, the outcome, the reason code, who objected.
    assert record["submission_id"] == SUBMISSION
    assert record["disposition"] == "dormant"
    assert record["reason_code"] == "submitter_unresponsive"
    assert record["reserved_names"] == ["TUMIG31"]
    assert [v["curator"] for v in record["verdicts"]] == ["alice"]

    # What it must never keep.
    assert "Turdus migratoria" not in written
    assert "host_needs_review" not in written


def test_an_unchanged_decision_record_is_not_rewritten(promote, workspace, registry):
    """A daily no-op commit buries the commits that mean something."""
    root, inbox = workspace

    def build(entries):
        ledger.ensure_entry(entries, SUBMISSION, "A", "2026-01-01T00:00:00+00:00")
    seed(inbox, build)

    entries = ledger.load(inbox)
    path = root / promote.DECISION_RECORD
    assert promote.write_decision_record(path, entries) is True
    assert promote.write_decision_record(path, entries) is False


def test_dry_run_changes_nothing(promote, workspace, registry):
    root, inbox = workspace

    def build(entries):
        entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-01-01T00:00:00+00:00")
        ledger.transition(entry, "ready_for_review", "intake", at="2026-01-01T01:00:00+00:00")
        ledger.transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")
    seed(inbox, build)

    assert promote.main(["--dry-run", "--now", "2026-06-01T00:00:00+00:00"]) == 0
    assert ledger.load(inbox)[SUBMISSION].state == "awaiting_submitter"
    assert not (root / promote.DECISION_RECORD).exists()


def test_an_empty_ledger_is_not_an_error(promote, workspace):
    assert promote.main(["--now", "2026-06-01T00:00:00+00:00"]) == 0


# ------------------------------------------------- the record exists before it has to

def test_an_empty_ledger_still_writes_the_decision_record(promote, workspace, capsys):
    """C2: data/decisions.json did not exist at all, because nothing has been enrolled.

    The program returned early on an empty ledger, so every run so far took that path. An
    absent record is indistinguishable from "this program has never run", and it is the
    only *committed* thing that will resolve a submission id later -- both the review
    ledger and the id map are gitignored. Establishing it while it is empty means the first
    real decision arrives as a diff to a tracked file.
    """
    root, inbox = workspace
    seed(inbox, lambda entries: None)          # a ledger file with no entries in it

    assert promote.main([]) == 0
    output = capsys.readouterr().out

    record = root / promote.DECISION_RECORD
    assert record.is_file(), "the record must be written even with nothing to record"
    assert json.loads(record.read_text()) == {"schema": 1, "decisions": []}
    assert "empty" in output


def test_an_empty_ledger_dry_run_writes_nothing(promote, workspace, capsys):
    root, inbox = workspace
    seed(inbox, lambda entries: None)

    assert promote.main(["--dry-run"]) == 0
    capsys.readouterr()

    assert not (root / promote.DECISION_RECORD).is_file()
