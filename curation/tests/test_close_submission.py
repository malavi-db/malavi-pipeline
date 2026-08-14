"""Tests for close_submission.py — the program that finishes a submission.

Before 2026-08-13 nothing in the repository produced ``declined``, ``withdrawn`` or
``awaiting_submitter``, so a whole tier of the ledger's rules had no way to run: reserved
names were never given back, the 60-day clock never started, and the decline notice in
``notify_submitters`` was unreachable code. What is pinned here is that this program can
reach those states and, just as importantly, that it does not reach them by going around
the rules — an approved submission still cannot be declined without somebody flagging it
first.
"""
from __future__ import annotations

import importlib.util

import pytest
import yaml

from malavi_curation import ledger
from malavi_curation.config import repo_root


@pytest.fixture(scope="module")
def close_submission():
    path = repo_root() / "curation" / "close_submission.py"
    spec = importlib.util.spec_from_file_location("_close_submission", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGISTRY = [
    {"id": "lead", "name": "Lead Curator", "email": "lead@example.edu",
     "role": "lead", "active": True},
    {"id": "bob", "name": "Bob", "email": "bob@example.edu", "role": "curator"},
]

CONFIG = {"review": {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60},
          "submissions": {"inbox_dir": "inbox"}}

SUBMISSION = "MALAVI-SUB-2026-000001"


@pytest.fixture
def registry_path(tmp_path):
    path = tmp_path / "curators.yml"
    path.write_text(yaml.safe_dump({"curators": REGISTRY}), encoding="utf-8")
    return path


def build(state="in_review", names=("TUMIG19",), registry_path=None):
    """A submission carrying a reserved name, moved to ``state`` the legitimate way."""
    entries = {}
    entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-08-01T00:00:00+00:00")
    entry.reserved_names = list(names)
    ledger.transition(entry, "ready_for_review", "intake", at="2026-08-01T12:00:00+00:00")
    if state == "ready_for_review":
        return entries, entry
    ledger.transition(entry, "in_review", "lead", at="2026-08-02T00:00:00+00:00")
    if state == "held":
        ledger.record_verdict(entry, "bob@example.edu", "hold",
                              reason_code="host_needs_review", reason_text="Host synonym?",
                              at="2026-08-02T01:00:00+00:00", registry_path=registry_path)
        ledger.transition(entry, "held", "bob", at="2026-08-02T02:00:00+00:00")
    elif state == "approved":
        ledger.record_verdict(entry, "lead@example.edu", "approve",
                              at="2026-08-02T01:00:00+00:00", registry_path=registry_path)
        ledger.transition(entry, "approved", "lead", at="2026-08-02T02:00:00+00:00")
    return entries, entry


# ------------------------------------------------------------------ declining

def test_a_held_submission_can_finally_be_declined(close_submission, registry_path):
    """The gap this program was written to close.

    "Reject" on the verdict form lands on ``held`` on purpose, so a rejection gets a second
    look. Nothing then moved it any further, so a rejected submission stayed live forever.
    """
    _, entry = build("held", registry_path=registry_path)

    moved, lines = close_submission.close(entry, "decline", "unresolved_objection",
                                          actor="maintainer",
                                          at="2026-08-10T00:00:00+00:00", config=CONFIG)

    assert moved
    assert entry.state == "declined"
    assert entry.final_disposition["reason_code"] == "unresolved_objection"
    assert entry.final_disposition["by"] == "maintainer"
    assert "held -> declined" in "\n".join(lines)


def test_declining_gives_the_reserved_names_back(close_submission, registry_path):
    _, entry = build("held", names=("TUMIG19", "ALCPOI02"), registry_path=registry_path)

    _, lines = close_submission.close(entry, "decline", "duplicate", actor="maintainer",
                                      at="2026-08-10T00:00:00+00:00", config=CONFIG)

    assert entry.name_state == "released"
    assert "ALCPOI02, TUMIG19" in "\n".join(lines)
    # And it stops being advertised, because the public queue lists live submissions only.
    assert ledger.public_queue({SUBMISSION: entry}) == []


def test_an_approved_submission_cannot_be_declined_behind_a_curators_back(
        close_submission, registry_path):
    """A decline follows an objection; it does not replace one.

    The ledger allows ``approved -> held`` and ``held -> declined`` but not
    ``approved -> declined``, so somebody has to flag it first and that flag is attributed.
    This program must not paper over that by holding it silently on the way past.
    """
    _, entry = build("approved", registry_path=registry_path)

    moved, lines = close_submission.close(entry, "decline", "out_of_scope",
                                          actor="maintainer",
                                          at="2026-08-10T00:00:00+00:00", config=CONFIG)

    assert not moved
    assert entry.state == "approved", "nothing moved"
    assert "REFUSED" in lines[0] and "not an allowed transition" in lines[0]


def test_a_decline_notice_becomes_due_only_after_the_wait(close_submission,
                                                          registry_path):
    """The 24-hour wait applies to a decline exactly as it does to an approval.

    ``notify_submitters`` reads the decline time out of the history this program writes, so
    if the history event were missing or misnamed the notice would never be sent — which is
    the state the whole system was in before today.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_notify", repo_root() / "curation" / "notify_submitters.py")
    notify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notify)

    _, entry = build("held", registry_path=registry_path)
    close_submission.close(entry, "decline", "unresolved_objection", actor="maintainer",
                           at="2026-08-10T00:00:00+00:00", config=CONFIG)

    assert notify._closed_at(entry) == "2026-08-10T00:00:00+00:00", \
        "the history event this program writes is what notify_submitters reads"

    from datetime import datetime, timezone
    too_soon = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
    later = datetime(2026, 8, 11, 6, 0, tzinfo=timezone.utc)
    assert notify.settled(entry, CONFIG, now=too_soon)[0] is False
    assert notify.settled(entry, CONFIG, now=later)[0] is True


# ------------------------------------------------------------------ withdrawing

def test_a_withdrawal_is_terminal(close_submission, registry_path):
    _, entry = build("in_review", registry_path=registry_path)

    moved, _ = close_submission.close(entry, "withdraw",
                                      close_submission.WITHDRAW_REASON,
                                      actor="maintainer",
                                      at="2026-08-10T00:00:00+00:00", config=CONFIG)

    assert moved and entry.state == "withdrawn"
    assert entry.name_state == "released"
    assert ledger.ALLOWED_TRANSITIONS["withdrawn"] == (), \
        "nothing revives a submission its author took back"


def test_a_withdrawal_reason_is_not_the_maintainers_to_choose(close_submission):
    code, complaint = close_submission.reason_for("withdraw", "out_of_scope")
    assert not code and "not ours to characterize" in complaint

    code, complaint = close_submission.reason_for("withdraw", "")
    assert code == close_submission.WITHDRAW_REASON and not complaint


# ------------------------------------------------------------------ waiting on them

def test_asking_the_submitter_starts_the_clock(close_submission, registry_path):
    _, entry = build("in_review", registry_path=registry_path)

    moved, lines = close_submission.close(entry, "ask", "", actor="maintainer",
                                          at="2026-08-10T00:00:00+00:00", config=CONFIG)

    assert moved and entry.state == "awaiting_submitter"
    assert entry.awaiting_since == "2026-08-10T00:00:00+00:00"
    assert "60-day clock" in "\n".join(lines)


def test_the_clock_this_starts_actually_expires(close_submission, registry_path):
    """``dormant`` was unreachable, because its only source state had no producer."""
    entries, entry = build("in_review", registry_path=registry_path)
    close_submission.close(entry, "ask", "", actor="maintainer",
                           at="2026-08-10T00:00:00+00:00", config=CONFIG)

    due = ledger.due_actions(entries, now="2026-11-10T00:00:00+00:00", config=CONFIG)

    assert [action.action for action in due] == ["timeout_dormant"]


def test_a_reason_makes_no_sense_when_only_waiting(close_submission):
    code, complaint = close_submission.reason_for("ask", "duplicate")
    assert not code and "does not apply to --ask" in complaint


# ------------------------------------------------------------------ the reason vocabulary

def test_a_decline_needs_a_reason(close_submission):
    code, complaint = close_submission.reason_for("decline", "")
    assert not code and "--decline needs --reason" in complaint


def test_only_a_declines_own_reasons_are_accepted(close_submission):
    """The codes belonging to other paths would be false in a decision record.

    ``released_in_build`` is a release's, ``submitter_unresponsive`` is the timeout's. Both
    are in the ledger's vocabulary and neither describes a decline.
    """
    for wrong in ("released_in_build", "submitter_unresponsive", "reopened", "invented"):
        code, complaint = close_submission.reason_for("decline", wrong)
        assert not code, f"{wrong} should not be accepted as a decline reason"
        assert "is not a reason a submission may be declined" in complaint

    for right in close_submission.DECLINE_REASONS:
        assert close_submission.reason_for("decline", right) == (right, "")


def test_every_decline_reason_is_in_the_ledgers_vocabulary(close_submission):
    """The narrowing must stay a subset; a code only this file knows would be refused."""
    assert set(close_submission.DECLINE_REASONS) <= set(ledger.DISPOSITION_REASON_CODES)
    assert close_submission.WITHDRAW_REASON in ledger.DISPOSITION_REASON_CODES


# ------------------------------------------------------------------ the program itself

def _workspace(close_submission, monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    monkeypatch.setattr(close_submission, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(close_submission, "load_config", lambda: CONFIG)
    return inbox


def test_a_dry_run_writes_nothing(close_submission, registry_path, tmp_path, monkeypatch,
                                  capsys):
    inbox = _workspace(close_submission, monkeypatch, tmp_path)
    entries, _ = build("held", registry_path=registry_path)
    ledger.save(inbox, entries)

    assert close_submission.main(["--submission", SUBMISSION, "--decline",
                                  "--reason", "duplicate"]) == 0
    output = capsys.readouterr().out

    assert "held -> declined" in output
    assert "[dry-run] nothing was written" in output
    assert ledger.load(inbox)[SUBMISSION].state == "held"


def test_apply_writes_the_close(close_submission, registry_path, tmp_path, monkeypatch,
                                capsys):
    inbox = _workspace(close_submission, monkeypatch, tmp_path)
    entries, _ = build("held", registry_path=registry_path)
    ledger.save(inbox, entries)

    assert close_submission.main(["--submission", SUBMISSION, "--decline",
                                  "--reason", "unresolved_objection",
                                  "--actor", "vaellis@udel.edu", "--apply"]) == 0
    capsys.readouterr()

    reloaded = ledger.load(inbox)[SUBMISSION]
    assert reloaded.state == "declined"
    assert reloaded.final_disposition["by"] == "vaellis@udel.edu"
    assert reloaded.name_state == "released"


def test_an_unknown_submission_is_refused_not_invented(close_submission, registry_path,
                                                       tmp_path, monkeypatch, capsys):
    inbox = _workspace(close_submission, monkeypatch, tmp_path)
    entries, _ = build("held", registry_path=registry_path)
    ledger.save(inbox, entries)

    code = close_submission.main(["--submission", "MALAVI-SUB-2026-999999", "--decline",
                                  "--reason", "duplicate", "--apply"])
    captured = capsys.readouterr()

    assert code == 2
    assert "no submission MALAVI-SUB-2026-999999" in captured.err
    assert SUBMISSION in captured.err, "it says which submissions are live"
    assert list(ledger.load(inbox)) == [SUBMISSION], "nothing was created"


def test_a_refused_move_exits_nonzero(close_submission, registry_path, tmp_path,
                                      monkeypatch, capsys):
    """An operator scripting this must be able to tell a refusal from a success."""
    inbox = _workspace(close_submission, monkeypatch, tmp_path)
    entries, _ = build("approved", registry_path=registry_path)
    ledger.save(inbox, entries)

    code = close_submission.main(["--submission", SUBMISSION, "--decline",
                                  "--reason", "duplicate", "--apply"])

    assert code == 1
    assert "REFUSED" in capsys.readouterr().out
    assert ledger.load(inbox)[SUBMISSION].state == "approved"


def test_the_three_actions_are_mutually_exclusive(close_submission):
    with pytest.raises(SystemExit):
        close_submission.main(["--submission", SUBMISSION, "--decline", "--withdraw"])
    with pytest.raises(SystemExit):
        close_submission.main(["--submission", SUBMISSION])

