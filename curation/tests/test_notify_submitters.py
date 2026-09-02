"""Tests for the submitter notifications (notify_submitters.py).

This program emails strangers, and what it tells them goes into a manuscript and into
GenBank. So the tests are about the ways it could send the wrong thing, or send at the
wrong moment:

* sending before a curator has approved;
* sending inside the publish-hold window, when a second curator can still object;
* sending while an objection actually stands;
* sending twice;
* naming the proposed name instead of the granted one, which is the single most damaging
  thing this message could get wrong.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone

import pytest

from malavi_curation import ledger
from malavi_curation.config import repo_root

CONFIG = {"review": {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60}}


@pytest.fixture(scope="module")
def cn():
    path = repo_root() / "curation" / "notify_submitters.py"
    spec = importlib.util.spec_from_file_location("_notify_submitters", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_entry(state="approved", approved_hours_ago=48, names=("TUMIG19",),
               corrections=None):
    entry = ledger.Entry(submission_id="20260101T000000_X", track="A",
                         received_at=ledger.now_utc())
    entry.state = state
    if approved_hours_ago is not None:
        entry.approved_at = (datetime.now(timezone.utc)
                             - timedelta(hours=approved_hours_ago)).isoformat()
    entry.reserved_names = list(names)
    entry.name_corrections = dict(corrections or {})
    return entry


# --------------------------------------------------------------------------------------
# When it is allowed to send at all
# --------------------------------------------------------------------------------------

def test_a_settled_approval_is_ready(cn):
    ready, why = cn.settled(make_entry(), CONFIG)
    assert ready, why


@pytest.mark.parametrize("state", ["received", "in_review", "held",
                                   "awaiting_submitter"])
def test_anything_short_of_approved_is_not_ready(cn, state):
    ready, why = cn.settled(make_entry(state=state), CONFIG)
    assert not ready
    assert "nothing to tell" in why


def test_the_publish_hold_must_have_elapsed(cn):
    """The hold exists so a second curator can still object.

    A confirmation sent inside it can be contradicted, and by then the submitter may have
    put the name in a manuscript. This is the test that encodes the whole design decision.
    """
    ready, why = cn.settled(make_entry(approved_hours_ago=1), CONFIG)
    assert not ready
    assert "hold has" in why


def test_a_standing_objection_beats_an_elapsed_hold(cn, monkeypatch):
    """A hold recorded late in the window still wins, however long the clock has run."""
    entry = make_entry(approved_hours_ago=999)
    monkeypatch.setattr(ledger, "blocking_holds", lambda e: ["a hold"])
    ready, why = cn.settled(entry, CONFIG)
    assert not ready
    assert "blocking verdict" in why


def test_an_approval_with_no_timestamp_is_not_ready(cn):
    """Without a timestamp the hold cannot be shown to have elapsed, so it has not."""
    entry = make_entry(approved_hours_ago=None)
    entry.approved_at = ""
    ready, why = cn.settled(entry, CONFIG)
    assert not ready
    assert "timestamp" in why


# --------------------------------------------------------------------------------------
# Sending once
# --------------------------------------------------------------------------------------

def test_a_previous_send_is_detected(cn):
    entry = make_entry()
    assert cn.already_sent(entry, cn.CONFIRMATION_EVENT) is None
    entry.history.append({"event": cn.CONFIRMATION_EVENT, "at": "2026-01-01T00:00:00Z"})
    assert cn.already_sent(entry, cn.CONFIRMATION_EVENT) == "2026-01-01T00:00:00Z"
    # tracked per event: a decline notice is a different message
    assert cn.already_sent(entry, cn.DECLINE_EVENT) is None


# --------------------------------------------------------------------------------------
# What the message says
# --------------------------------------------------------------------------------------

def test_the_granted_name_is_sent_not_the_proposed_one(cn):
    """The submitter proposed TUMIG06 and was granted TUMIG25.

    Sending TUMIG06 would put a name in a paper that MalAvi has given to somebody else.
    `agreed_names` is what applies the correction, so this asserts the join between the
    ledger and the message.
    """
    entry = make_entry(names=("TUMIG06",), corrections={"TUMIG06": "TUMIG25"})
    assert ledger.agreed_names(entry) == ["TUMIG25"]


def test_corrections_are_reported_separately(cn):
    """A changed name has to be unmissable, not merely present in a list."""
    from malavi_curation.report_delivery import build_notice_payload
    payload = build_notice_payload(
        "20260101T000000_X", to="a@example.edu", submitter_name="A Researcher",
        names=["TUMIG25"], corrections={"TUMIG06": "TUMIG25"}, reference="A paper 2026")
    assert payload["action"] == "confirm_names"
    assert payload["names"] == ["TUMIG25"]
    assert payload["corrections"] == {"TUMIG06": "TUMIG25"}


def test_refuses_to_send_without_a_usable_address(cn):
    from malavi_curation.report_delivery import DeliveryError, build_notice_payload
    with pytest.raises(DeliveryError, match="submitter address"):
        build_notice_payload("20260101T000000_X", to="", submitter_name="",
                             names=["TUMIG25"], corrections={})


def test_refuses_to_send_with_nothing_to_confirm(cn):
    """A records-only submission has no names. An email saying so would confuse."""
    from malavi_curation.report_delivery import DeliveryError, build_notice_payload
    with pytest.raises(DeliveryError, match="nothing to confirm"):
        build_notice_payload("20260101T000000_X", to="a@example.edu", submitter_name="",
                             names=[], corrections={})


def test_submitter_details_come_from_the_submission(cn, tmp_path):
    inbox = tmp_path / "submissions"
    (inbox / "20260101T000000_X").mkdir(parents=True)
    (inbox / "20260101T000000_X" / "submission.json").write_text(json.dumps({
        "submitter": {"email": "person@example.edu", "name": "A Researcher"},
        "reference": {"title": "A study of things", "year": 2026},
    }), encoding="utf-8")

    email, who, label = cn.submitter_of(inbox, "20260101T000000_X")
    assert email == "person@example.edu"
    assert who == "A Researcher"
    assert "A study of things" in label


def test_a_missing_submission_json_is_an_error_not_a_skip(cn, tmp_path):
    """An approved submission we cannot answer is something a maintainer must see."""
    from malavi_curation.report_delivery import DeliveryError
    inbox = tmp_path / "submissions"
    (inbox / "20260101T000000_X").mkdir(parents=True)
    with pytest.raises(DeliveryError, match="no submission.json"):
        cn.submitter_of(inbox, "20260101T000000_X")


# --------------------------------------------------------------------------------------
# The decline notice
# --------------------------------------------------------------------------------------

def declined_entry(hours_ago=48):
    entry = ledger.Entry(submission_id="20260101T000000_D", track="A",
                         received_at=ledger.now_utc())
    entry.state = "declined"
    entry.history.append({
        "event": "transition", "to": "declined",
        "at": (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(),
    })
    return entry


def test_a_settled_decline_is_ready(cn):
    ready, why = cn.settled(declined_entry(), CONFIG)
    assert ready, why


def test_a_fresh_decline_waits(cn):
    """The same hold applies to bad news, so a mis-click can be undone before it is sent."""
    ready, why = cn.settled(declined_entry(hours_ago=1), CONFIG)
    assert not ready
    assert "hold has" in why


def test_a_decline_with_no_recorded_moment_never_sends(cn):
    """Falling back to 'now' would send immediately, defeating the wait entirely."""
    entry = declined_entry()
    entry.history = []
    ready, why = cn.settled(entry, CONFIG)
    assert not ready
    assert "timestamp" in why


def test_the_decline_message_carries_no_reason(cn):
    """A curator's reasoning quotes the submission and invites a question nobody can answer.

    The automatic message says what happened and hands the conversation to a person; the
    reasoning travels in that person's reply.
    """
    from malavi_curation.report_delivery import build_decline_payload
    payload = build_decline_payload("20260101T000000_D", to="a@example.edu",
                                    submitter_name="A Researcher",
                                    reference="A study 2026")
    assert payload["action"] == "decline_notice"
    assert not any(k in payload for k in ("reason", "reasons", "verdict", "notes"))


def test_the_decline_message_still_needs_an_address(cn):
    from malavi_curation.report_delivery import DeliveryError, build_decline_payload
    with pytest.raises(DeliveryError, match="submitter address"):
        build_decline_payload("20260101T000000_D", to="", submitter_name="")


def test_approved_and_declined_are_the_only_states_that_notify(cn):
    assert set(cn.OUTCOMES) == {"approved", "declined"}


# --------------------------------------------------------------------------------------
# One failed delivery must not cost the run its record of the successful ones
#
# The bug these pin: history records are appended in memory inside
# `with ledger.open_ledger(...)`, and that context manager only save()s on a CLEAN exit.
# An exception escaping the send therefore threw away the "already told them" records of
# everyone already emailed in the same run -- so the next run emailed them a second time.
# The mail is irreversible; the guard against repeating it was the thing being discarded.
# --------------------------------------------------------------------------------------

class _Delivered:
    """Enough of report_delivery.Delivered for main() to print a line about it."""
    notified = 1


def _two_approved_submissions(tmp_path):
    """An inbox with two approved, settled submissions, each answerable by email."""
    inbox = tmp_path / "submissions"
    entries = {}
    for suffix in ("A", "B"):
        submission_id = f"20260101T000000_{suffix}"
        directory = inbox / submission_id
        directory.mkdir(parents=True)
        (directory / "submission.json").write_text(json.dumps({
            "submitter": {"email": f"{suffix.lower()}@example.edu", "name": f"Dr {suffix}"},
            "reference": {"title": f"Study {suffix}", "year": 2026},
        }), encoding="utf-8")
        entry = make_entry(names=(f"TUMIG1{suffix == 'B' and 9 or 8}",))
        entry.submission_id = submission_id
        entries[submission_id] = entry
    ledger.save(inbox, entries)
    return inbox


def test_a_failed_delivery_keeps_the_records_of_the_ones_that_went(cn, tmp_path,
                                                                   monkeypatch):
    from malavi_curation.report_delivery import DeliveryError

    inbox = _two_approved_submissions(tmp_path)
    monkeypatch.setattr(cn, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cn, "load_config", lambda: CONFIG)

    # A is emailed; the endpoint then fails for B, exactly as an HTTP 500 would.
    def flaky(submission_id, **_):
        if submission_id.endswith("_B"):
            raise DeliveryError("the endpoint returned 500")
        return _Delivered()

    monkeypatch.setattr(cn, "deliver_name_confirmation", flaky)

    assert cn.main([]) == 1, "a partial run must not report success"

    written = ledger.load(inbox)
    sent_events = {sid: [h for h in e.history if h.get("event") == "name_confirmation_sent"]
                   for sid, e in written.items()}
    assert len(sent_events["20260101T000000_A"]) == 1, (
        "A was emailed, so the ledger must say so -- otherwise the next run emails Dr A "
        "a second confirmation")
    assert sent_events["20260101T000000_B"] == [], "B was never emailed; do not claim it was"


def test_the_failed_one_is_retried_and_the_sent_one_is_not(cn, tmp_path, monkeypatch):
    """The run after a partial failure: B gets its email, A is left alone."""
    from malavi_curation.report_delivery import DeliveryError

    inbox = _two_approved_submissions(tmp_path)
    monkeypatch.setattr(cn, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cn, "load_config", lambda: CONFIG)

    attempted = []

    def flaky(submission_id, **_):
        attempted.append(submission_id)
        if submission_id.endswith("_B") and len(attempted) == 2:
            raise DeliveryError("the endpoint returned 500")
        return _Delivered()

    monkeypatch.setattr(cn, "deliver_name_confirmation", flaky)
    assert cn.main([]) == 1

    attempted.clear()
    assert cn.main([]) == 0, "the retry should succeed and report success"
    assert attempted == ["20260101T000000_B"], (
        "only the one that failed should be attempted again")


# --------------------------------------------------------------------------------------
# One name, one submitter
#
# The email tells its reader to put the name in GenBank. Two people told that about one
# name is the failure the reservation system exists to prevent, and until 2026-09-02 the
# only collision check ran at ingest -- after both emails had gone.
# --------------------------------------------------------------------------------------

def test_a_name_another_submission_holds_is_not_confirmed(cn):
    mine = make_entry(names=("TUMIG19",))
    mine.submission_id = "20260101T000000_A"
    theirs = make_entry(names=("TUMIG19",))
    theirs.submission_id = "20260101T000000_B"
    entries = {mine.submission_id: mine, theirs.submission_id: theirs}

    ready, why = cn.settled(mine, CONFIG, entries=entries)
    assert not ready
    assert "20260101T000000_B" in why and "TUMIG19" in why, "name the other submission"


def test_a_declined_holder_no_longer_blocks_the_confirmation(cn):
    mine = make_entry(names=("TUMIG19",))
    mine.submission_id = "20260101T000000_A"
    theirs = make_entry(state="declined", names=("TUMIG19",))
    theirs.submission_id = "20260101T000000_B"
    entries = {mine.submission_id: mine, theirs.submission_id: theirs}

    ready, why = cn.settled(mine, CONFIG, entries=entries)
    assert ready, why


def test_a_clashing_name_is_refused_loudly_and_nothing_is_sent(cn, tmp_path, monkeypatch,
                                                              capsys):
    """Both settled, both claiming TUMIG18: neither is emailed, the run fails, and the
    refusal names the other submission on stderr rather than being a quiet skip."""
    inbox = _two_approved_submissions(tmp_path)
    entries = ledger.load(inbox)
    entries["20260101T000000_B"].reserved_names = ["TUMIG18"]      # same as A's
    ledger.save(inbox, entries)
    monkeypatch.setattr(cn, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cn, "load_config", lambda: CONFIG)

    delivered = []
    monkeypatch.setattr(cn, "deliver_name_confirmation",
                        lambda submission_id, **_: delivered.append(submission_id)
                        or _Delivered())

    assert cn.main(["--no-publish"]) == 1
    assert delivered == [], "a name two submissions claim must reach nobody"
    err = capsys.readouterr().err
    assert "REFUSED" in err and "TUMIG18" in err
    assert "20260101T000000_A" in err and "20260101T000000_B" in err

    written = ledger.load(inbox)
    for entry in written.values():
        assert not any(h.get("event") == cn.CONFIRMATION_EVENT for h in entry.history), \
            "nothing was sent, so nothing may claim it was"


# --------------------------------------------------------------------------------------
# Sent once, the endpoint's half
#
# The history record is written only after the endpoint replies. A request that timed out
# here after the mail had gone is therefore retried next run, and the endpoint has to be
# the one that remembers. It answers `already_sent`; this side records it and moves on.
# --------------------------------------------------------------------------------------

def test_an_endpoint_that_already_sent_it_is_recorded_not_retried(cn, tmp_path,
                                                                 monkeypatch):
    class _AlreadySent:
        notified = 0
        action = "already_sent"

    inbox = _two_approved_submissions(tmp_path)
    monkeypatch.setattr(cn, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cn, "load_config", lambda: CONFIG)
    monkeypatch.setattr(cn, "deliver_name_confirmation", lambda *_a, **_k: _AlreadySent())

    assert cn.main(["--no-publish"]) == 0
    written = ledger.load(inbox)
    for entry in written.values():
        assert any(h.get("event") == cn.CONFIRMATION_EVENT for h in entry.history), \
            "the endpoint's earlier send is the send; record it so it is not tried again"


def test_the_endpoint_checks_for_a_previous_send_before_mailing():
    """The Apps Script half, checked at the level this repository can: the confirmation
    handler must consult its memory BEFORE composing the mail and write to it only AFTER
    the mail has gone. A key recorded before the send would suppress the retry that is
    supposed to fix a failed one. (The live deployment has to be updated by hand for any
    of this to apply -- see the RE-DEPLOYING note at the top of the script.)"""
    source = (repo_root() / "curation" / "apps_script" / "publish_report.gs").read_text(
        encoding="utf-8")
    body = source[source.index("function confirmNames_("):]
    body = body[:body.index("\n}\n")]
    assert (body.index("alreadyConfirmed_(") < body.index("GmailApp.sendEmail(")
            < body.index("rememberConfirmation_(")), body
    assert "function alreadyConfirmed_(" in source
    assert "function rememberConfirmation_(" in source
    assert "PropertiesService" in source
