"""Tests for the verdict fetch — the join between the responses sheet and the ledger.

The rules themselves are tested in ``test_ledger.py``; what is tested here is that this
program *reaches* them and does not quietly work around them. The failure modes worth
pinning are all of the same shape: a curator's decision goes missing, or a decision is
counted twice.

Every scenario below runs against a temporary curator registry with two curators and a
lead, so "another curator's hold" and "a lead may not approve their own correction" are
expressible without touching MalAvi's real registry.
"""
from __future__ import annotations

import importlib.util
import json

import pytest
import yaml

from malavi_curation import ledger, verdicts
from malavi_curation.config import repo_root


@pytest.fixture(scope="module")
def fetch():
    """The fetch program, loaded from the script it lives in."""
    path = repo_root() / "curation" / "fetch_verdicts.py"
    spec = importlib.util.spec_from_file_location("_fetch_verdicts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REGISTRY = [
    {"id": "lead", "name": "Lead Curator", "email": "lead@example.edu",
     "role": "lead", "active": True},
    {"id": "alice", "name": "Alice", "email": "alice@example.edu", "role": "curator"},
    {"id": "bob", "name": "Bob", "email": "bob@example.edu", "role": "curator"},
]

CONFIG = {"review": {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60}}

SUBMISSION = "MALAVI-SUB-2026-000001"


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "curators.yml"
    path.write_text(yaml.safe_dump({"curators": REGISTRY}), encoding="utf-8")
    return path


@pytest.fixture
def entries():
    """One submission, screened and sitting in front of a curator."""
    entries = {}
    entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-08-01T00:00:00+00:00")
    ledger.transition(entry, "ready_for_review", "intake", at="2026-08-01T12:00:00+00:00")
    return entries


def row(**overrides):
    """One well-formed 'Accept' response, as Google exports it."""
    base = {
        verdicts.COL_TIMESTAMP: "2026-08-02 09:00:00",
        verdicts.COL_EMAIL: "alice@example.edu",
        verdicts.COL_SUBMISSION: SUBMISSION,
        verdicts.COL_REVISION: "1",
        verdicts.COL_ACTION: verdicts.ACTION_VERDICT,
        verdicts.COL_VERDICT: "Accept",
        verdicts.COL_WHY: "Checked the names and the sequences.",
        verdicts.COL_CHECKED: "Proposed names, Sequences",
    }
    base.update(overrides)
    return base


def apply(fetch, entries, registry, **overrides):
    """Parse one row and apply it, returning the outcome record."""
    action = verdicts.parse_row(row(**overrides))
    assert action.ok, getattr(action, "reason", "")
    return fetch.apply_action(entries, action, CONFIG, registry_path=registry)


# ---------------------------------------------------------------- the happy path

def test_an_accept_approves_and_starts_the_publish_hold(fetch, entries, registry):
    outcome = apply(fetch, entries, registry)

    assert outcome["status"] == "applied"
    entry = entries[SUBMISSION]
    assert entry.state == "approved"
    # The hold is a clock, and a clock that never started never runs out.
    assert entry.approved_at == "2026-08-02T09:00:00+00:00"


def test_the_hold_is_measured_from_the_curators_timestamp_not_the_fetch(fetch, entries,
                                                                       registry):
    """The 24 hours run from when the curator decided, not from when we noticed.

    If the fetch job's own clock started the hold, a fetch that ran late would silently
    extend the window, and a backfill of an old sheet would restart it entirely.
    """
    apply(fetch, entries, registry, **{verdicts.COL_TIMESTAMP: "2026-08-02 09:00:00"})
    assert entries[SUBMISSION].approved_at == "2026-08-02T09:00:00+00:00"

    due = ledger.due_actions(entries, now="2026-08-03T08:59:00+00:00", config=CONFIG)
    assert due == []
    due = ledger.due_actions(entries, now="2026-08-03T09:00:00+00:00", config=CONFIG)
    assert [d.action for d in due] == ["release_eligible"]


# ---------------------------------------------------------------- blocking verdicts

def test_a_flag_holds_the_submission(fetch, entries, registry):
    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_VERDICT: "Flag for further review",
                       verdicts.COL_WHY: "The host name looks like a synonym."})
    assert outcome["status"] == "applied"
    assert entries[SUBMISSION].state == "held"


def test_a_reject_holds_rather_than_declining(fetch, entries, registry):
    """The curator instructions promise a lead can still overrule a rejection.

    An override acts on a *standing* objection, so a rejection that moved the submission
    straight to the terminal ``declined`` state would leave nothing to overrule and would
    have one curator's form submission end another researcher's submission outright.
    """
    apply(fetch, entries, registry,
          **{verdicts.COL_VERDICT: "Reject", verdicts.COL_WHY: "Not avian haemosporidia."})

    entry = entries[SUBMISSION]
    assert entry.state == "held"
    assert entry.state != "declined"
    # And it is genuinely overridable: the objection stands.
    assert [v.verdict for v in ledger.blocking_holds(entry)] == ["decline"]


def test_a_flag_recorded_late_in_the_publish_window_stops_the_release(fetch, entries,
                                                                     registry):
    """Hour 23 of 24. This is the entire reason the window exists."""
    apply(fetch, entries, registry)
    assert entries[SUBMISSION].state == "approved"

    apply(fetch, entries, registry,
          **{verdicts.COL_TIMESTAMP: "2026-08-03 08:00:00",
             verdicts.COL_EMAIL: "bob@example.edu",
             verdicts.COL_VERDICT: "Flag for further review",
             verdicts.COL_WHY: "The locality does not match the paper."})

    assert entries[SUBMISSION].state == "held"
    assert ledger.due_actions(entries, now="2026-08-04T00:00:00+00:00", config=CONFIG) == []


def test_an_approval_is_refused_while_an_objection_stands(fetch, entries, registry):
    """Dissent outranks approval, and the refusal is recorded rather than swallowed."""
    apply(fetch, entries, registry,
          **{verdicts.COL_EMAIL: "bob@example.edu",
             verdicts.COL_VERDICT: "Flag for further review",
             verdicts.COL_WHY: "Sequence is 460 bp."})

    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_TIMESTAMP: "2026-08-02 10:00:00"})

    entry = entries[SUBMISSION]
    assert entry.state == "held"
    # The approval is still IN the ledger — it is a curator's act and it is on the record.
    # What it did not do is move the submission.
    assert [v.verdict for v in ledger.verdicts_for_revision(entry)] == ["hold", "approve"]
    assert "objection" in outcome["detail"]


# ---------------------------------------------------------------- clearing a block

def test_a_curator_can_withdraw_their_own_flag(fetch, entries, registry):
    """The first of the two ways the curator instructions say a block ends."""
    apply(fetch, entries, registry,
          **{verdicts.COL_EMAIL: "bob@example.edu",
             verdicts.COL_VERDICT: "Flag for further review",
             verdicts.COL_WHY: "Thought the host was a synonym."})
    held = entries[SUBMISSION]
    assert held.state == "held"
    flag_id = ledger.blocking_holds(held)[0].id

    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_TIMESTAMP: "2026-08-02 12:00:00",
                       verdicts.COL_EMAIL: "bob@example.edu",
                       verdicts.COL_ACTION: verdicts.ACTION_RETRACT,
                       verdicts.COL_RETRACT_ID: flag_id,
                       verdicts.COL_RETRACT_WHY: "Checked Clements; the name is current."})

    assert outcome["status"] == "applied"
    assert entries[SUBMISSION].state == "in_review"
    assert ledger.blocking_holds(entries[SUBMISSION]) == []


def test_one_curator_cannot_withdraw_anothers_flag_through_the_retraction_route(
        fetch, entries, registry):
    """Otherwise the lead-only override is bypassed by choosing a different form branch."""
    apply(fetch, entries, registry,
          **{verdicts.COL_EMAIL: "bob@example.edu",
             verdicts.COL_VERDICT: "Flag for further review",
             verdicts.COL_WHY: "Locality is ambiguous."})
    flag_id = ledger.blocking_holds(entries[SUBMISSION])[0].id

    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_EMAIL: "alice@example.edu",
                       verdicts.COL_ACTION: verdicts.ACTION_RETRACT,
                       verdicts.COL_RETRACT_ID: flag_id})

    assert outcome["status"] == "refused"
    assert "override" in outcome["detail"]
    assert entries[SUBMISSION].state == "held"


def test_a_lead_override_clears_the_hold_and_returns_it_to_review(fetch, entries, registry):
    apply(fetch, entries, registry,
          **{verdicts.COL_EMAIL: "bob@example.edu",
             verdicts.COL_VERDICT: "Flag for further review",
             verdicts.COL_WHY: "Prevalence looks wrong."})
    flag_id = ledger.blocking_holds(entries[SUBMISSION])[0].id

    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_TIMESTAMP: "2026-08-04 09:00:00",
                       verdicts.COL_EMAIL: "lead@example.edu",
                       verdicts.COL_ACTION: verdicts.ACTION_OVERRIDE,
                       verdicts.COL_HOLD_ID: flag_id,
                       verdicts.COL_CONSULTED: "Bob; Alice",
                       verdicts.COL_CONSULTED_ON: "2026-08-03",
                       verdicts.COL_CONSULTED_HOW: "Video call",
                       verdicts.COL_RESOLVED: "Prevalence was as reported in the paper."})

    assert outcome["status"] == "applied"
    # Back to review, NOT to approved: an override removed an obstacle, it did not
    # express approval.
    assert entries[SUBMISSION].state == "in_review"


def test_a_non_lead_cannot_override(fetch, entries, registry):
    apply(fetch, entries, registry,
          **{verdicts.COL_EMAIL: "bob@example.edu",
             verdicts.COL_VERDICT: "Flag for further review",
             verdicts.COL_WHY: "Prevalence looks wrong."})
    flag_id = ledger.blocking_holds(entries[SUBMISSION])[0].id

    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_EMAIL: "alice@example.edu",
                       verdicts.COL_ACTION: verdicts.ACTION_OVERRIDE,
                       verdicts.COL_HOLD_ID: flag_id,
                       verdicts.COL_CONSULTED: "Bob",
                       verdicts.COL_CONSULTED_ON: "2026-08-03",
                       verdicts.COL_CONSULTED_HOW: "Email"})

    assert outcome["status"] == "refused"
    assert entries[SUBMISSION].state == "held"


# ---------------------------------------------------------------- corrections

def _flag_and_propose(fetch, entries, registry, by="bob@example.edu"):
    apply(fetch, entries, registry,
          **{verdicts.COL_EMAIL: by, verdicts.COL_VERDICT: "Flag for further review",
             verdicts.COL_WHY: "Host is listed under a synonym."})
    return apply(fetch, entries, registry,
                 **{verdicts.COL_TIMESTAMP: "2026-08-02 14:00:00",
                    verdicts.COL_EMAIL: by,
                    verdicts.COL_ACTION: verdicts.ACTION_CORRECTION,
                    verdicts.COL_CORRECTION_KIND: "Judgment — confirmed with another curator",
                    verdicts.COL_CONFIRMED_BY: "Alice",
                    verdicts.COL_CONFIRMED_ON: "2026-08-02",
                    verdicts.COL_CHANGE: "Turdus migratorius, not Turdus migratoria."})


def test_a_correction_is_proposed_and_waits_for_a_lead(fetch, entries, registry):
    outcome = _flag_and_propose(fetch, entries, registry)
    assert outcome["status"] == "applied"

    entry = entries[SUBMISSION]
    assert len(entry.corrections) == 1
    # Proposed is not approved, and nothing may apply it yet.
    assert ledger.pending_corrections(entry) == []


def test_a_lead_can_approve_a_correction(fetch, entries, registry):
    """The gate the curator instructions promise, which no interface could reach before."""
    _flag_and_propose(fetch, entries, registry)

    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_TIMESTAMP: "2026-08-03 09:00:00",
                       verdicts.COL_EMAIL: "lead@example.edu",
                       verdicts.COL_ACTION: verdicts.ACTION_APPROVE_CORRECTION,
                       verdicts.COL_CORRECTION_ID: "C1",
                       verdicts.COL_DISCUSSED_WITH: "Bob; the submitting authors"})

    assert outcome["status"] == "applied"
    assert [c.id for c in ledger.pending_corrections(entries[SUBMISSION])] == ["C1"]


def test_a_lead_cannot_approve_their_own_correction(fetch, entries, registry):
    _flag_and_propose(fetch, entries, registry, by="lead@example.edu")

    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_TIMESTAMP: "2026-08-03 09:00:00",
                       verdicts.COL_EMAIL: "lead@example.edu",
                       verdicts.COL_ACTION: verdicts.ACTION_APPROVE_CORRECTION,
                       verdicts.COL_CORRECTION_ID: "C1",
                       verdicts.COL_DISCUSSED_WITH: "the submitting authors"})

    assert outcome["status"] == "refused"
    assert "cannot also approve" in outcome["detail"]
    assert ledger.pending_corrections(entries[SUBMISSION]) == []


# ---------------------------------------------------------------- filing, not losing

def test_an_unknown_submission_id_is_filed_not_invented(fetch, entries, registry):
    """A verdict must never be what brings a submission into existence."""
    action = verdicts.parse_row(row(**{verdicts.COL_SUBMISSION: "MALAVI-SUB-2026-999999"}))
    outcome = fetch.apply_action(entries, action, CONFIG, registry_path=registry)

    assert outcome["status"] == "unknown_submission"
    assert "MALAVI-SUB-2026-999999" not in entries


def test_an_address_outside_the_registry_is_filed_on_the_entry(fetch, entries, registry):
    """Not honored — anyone can reach the form link — and not discarded either."""
    outcome = apply(fetch, entries, registry,
                    **{verdicts.COL_EMAIL: "stranger@example.com"})

    assert outcome["status"] == "filed_unrecognized"
    assert entries[SUBMISSION].state == "ready_for_review"
    assert entries[SUBMISSION].unrecognized[0]["address"] == "stranger@example.com"


def test_an_unparseable_row_does_not_stop_the_ones_behind_it(fetch, entries, registry):
    """One curator typing an id by hand must not cost every other curator their verdict."""
    bad = verdicts.parse_row(row(**{verdicts.COL_SUBMISSION: "testing1,2,3"}))
    assert not bad.ok

    outcome = apply(fetch, entries, registry)
    assert outcome["status"] == "applied"
    assert entries[SUBMISSION].state == "approved"


# ---------------------------------------------------------------- idempotence

def test_the_same_response_fingerprints_the_same_and_a_changed_one_does_not(fetch):
    assert fetch.fingerprint(row()) == fetch.fingerprint(dict(reversed(list(row().items()))))
    assert fetch.fingerprint(row()) != fetch.fingerprint(row(**{verdicts.COL_WHY: "x"}))


def test_applying_one_response_twice_really_does_record_it_twice(fetch, entries, registry):
    """The applied-ledger is the only thing stopping this, so pin what it stops.

    The damage is *not* a phantom second approver — ``standing_positions`` keys by curator,
    so one curator's two approvals still count as one. It is the duplicate record itself,
    and the next test shows why that is not cosmetic.
    """
    apply(fetch, entries, registry)
    apply(fetch, entries, registry)

    entry = entries[SUBMISSION]
    assert len(entry.verdicts) == 2
    assert len(ledger.approvals(entry)) == 1


def test_a_duplicated_flag_cannot_be_withdrawn_in_one_act(fetch, entries, registry):
    """Why duplicate application matters, stated as the harm rather than the count.

    Retraction names one verdict id. If the same objection was recorded twice, withdrawing
    the one the curator was shown leaves the duplicate standing, and the submission stays
    blocked by an objection its author believes they have withdrawn.
    """
    flag = {verdicts.COL_EMAIL: "bob@example.edu",
            verdicts.COL_VERDICT: "Flag for further review",
            verdicts.COL_WHY: "Host is listed under a synonym."}
    apply(fetch, entries, registry, **flag)
    apply(fetch, entries, registry, **flag)

    entry = entries[SUBMISSION]
    first_id = [v for v in entry.verdicts if v.verdict == "hold"][0].id
    apply(fetch, entries, registry,
          **{verdicts.COL_TIMESTAMP: "2026-08-02 12:00:00",
             verdicts.COL_EMAIL: "bob@example.edu",
             verdicts.COL_ACTION: verdicts.ACTION_RETRACT,
             verdicts.COL_RETRACT_ID: first_id})

    assert entry.state == "held"
    assert ledger.blocking_holds(entry) != []


def test_the_applied_ledger_round_trips_a_fingerprint(fetch, tmp_path):
    path = tmp_path / "verdicts_applied.json"
    fetch.save_applied(path, {fetch.fingerprint(row()): {"status": "applied"}})
    assert fetch.fingerprint(row()) in fetch.load_applied(path)


def test_a_corrupt_applied_ledger_stops_the_run(fetch, tmp_path):
    """Treating it as empty would re-apply every response in the sheet."""
    path = tmp_path / "verdicts_applied.json"
    path.write_text("{not json")
    with pytest.raises(SystemExit):
        fetch.load_applied(path)


def test_the_applied_ledger_survives_a_round_trip(fetch, tmp_path):
    path = tmp_path / "verdicts_applied.json"
    fetch.save_applied(path, {"abc123": {"status": "applied", "submission": SUBMISSION}})
    assert fetch.load_applied(path)["abc123"]["submission"] == SUBMISSION
    assert json.loads(path.read_text())["schema"] == 1


# ------------------------------------------- closing a submission from the form

def _reject_then_close(fetch, entries, registry, closer="lead@example.edu"):
    """The real sequence: somebody rejects, which holds it; a lead then closes it."""
    apply(fetch, entries, registry, **{
        verdicts.COL_EMAIL: "bob@example.edu",
        verdicts.COL_VERDICT: "Reject",
        verdicts.COL_WHY: "The records cannot be checked against the paper.",
    })
    assert entries[SUBMISSION].state == "held", "Reject holds; it does not end anything"

    return apply(fetch, entries, registry, **{
        verdicts.COL_EMAIL: closer,
        verdicts.COL_ACTION: verdicts.ACTION_CLOSE,
        verdicts.COL_CLOSE_REASON: "A flag on it was never answered",
        verdicts.COL_CLOSE_NOTE: "No reply in six weeks.",
    })


def test_a_lead_can_finish_a_rejected_submission_from_the_form(fetch, entries, registry):
    """B2's remaining half: a curator's own route to `declined`, with no shell involved."""
    entries[SUBMISSION].reserved_names = ["TUMIG19"]

    outcome = _reject_then_close(fetch, entries, registry)

    assert outcome["status"] == "applied"
    entry = entries[SUBMISSION]
    assert entry.state == "declined"
    assert entry.final_disposition["reason_code"] == "unresolved_objection"
    assert entry.final_disposition["by"] == "lead"
    assert entry.name_state == "released"
    assert "TUMIG19" in outcome["detail"], "it says which names went back"


def test_a_non_lead_closing_is_refused_and_filed(fetch, entries, registry):
    """Refused, not lost. The curator has already submitted and gone by now."""
    outcome = _reject_then_close(fetch, entries, registry, closer="alice@example.edu")

    assert outcome["status"] == "refused"
    assert "not an active lead curator" in outcome["detail"]
    assert entries[SUBMISSION].state == "held", "nothing moved"


def test_closing_an_approved_submission_is_refused(fetch, entries, registry):
    """A decline follows an objection rather than replacing one."""
    apply(fetch, entries, registry)
    assert entries[SUBMISSION].state == "approved"

    outcome = apply(fetch, entries, registry, **{
        verdicts.COL_EMAIL: "lead@example.edu",
        verdicts.COL_ACTION: verdicts.ACTION_CLOSE,
        verdicts.COL_CLOSE_REASON: "Not avian haemosporidian data",
    })

    assert outcome["status"] == "refused"
    assert "not an allowed transition" in outcome["detail"]
    assert entries[SUBMISSION].state == "approved"


def test_the_decline_notice_becomes_due_after_the_close(fetch, entries, registry):
    """The whole point: a rejected submitter is finally told something."""
    import importlib.util
    from datetime import datetime, timedelta, timezone

    spec = importlib.util.spec_from_file_location(
        "_notify", repo_root() / "curation" / "notify_submitters.py")
    notify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notify)

    _reject_then_close(fetch, entries, registry)
    entry = entries[SUBMISSION]

    closed = notify._closed_at(entry)
    assert closed, "notify_submitters reads the history event this writes"
    at = datetime.fromisoformat(closed)
    assert notify.settled(entry, CONFIG, now=at + timedelta(hours=6))[0] is False
    assert notify.settled(entry, CONFIG, now=at + timedelta(hours=48))[0] is True


# ------------------------------------------------------- repeated columns in the sheet
#
# The responses sheet keeps a column for every question the form has ever had. A question
# deleted, or re-created rather than renamed during a hand edit, leaves its column behind
# forever. Most of that is harmless clutter nothing reads.
#
# It stops being harmless when the leftover shares a name with a live column, because
# csv.DictReader keeps the RIGHTMOST of a repeated name -- and the leftover is usually the
# one on the right, and always empty. Found on the live sheet 2026-08-14: two columns
# titled "Why is it being closed?", one live and one orphaned, which would have made every
# close request parse as `unknown closing reason ''`.

def test_a_repeated_column_the_parser_reads_is_refused(fetch):
    text = ("Timestamp,Submission id,Why is it being closed?,Why is it being closed?\n"
            "2026-08-14 10:00:00,MALAVI-SUB-2026-000001,It is already in MalAvi,\n")
    with pytest.raises(fetch.DuplicateColumns) as raised:
        fetch.read_rows(text)
    assert "Why is it being closed?" in str(raised.value)


def test_a_repeated_column_nothing_reads_is_ignored(fetch):
    """Orphan columns are the normal state of a form-linked sheet. Only refuse over the
    ones that would actually be misread, or the guard cries wolf and gets switched off."""
    text = ("Timestamp,Submission id,Some old question,Some old question\n"
            "2026-08-14 10:00:00,MALAVI-SUB-2026-000001,a,b\n")
    rows = fetch.read_rows(text)
    assert len(rows) == 1
    assert rows[0]["Submission id"] == "MALAVI-SUB-2026-000001"


def test_a_clean_header_parses(fetch):
    text = ("Timestamp,Submission id,Why is it being closed?\n"
            "2026-08-14 10:00:00,MALAVI-SUB-2026-000001,It is already in MalAvi\n")
    rows = fetch.read_rows(text)
    assert rows[0]["Why is it being closed?"] == "It is already in MalAvi"


def test_every_column_the_parser_reads_is_in_the_guarded_set():
    """COLUMNS_READ is derived from the COL_ constants, so a new column joins it for free.
    Stated as a test anyway: if that derivation is ever replaced by a hand-written list,
    this is what notices the first time somebody forgets to extend it."""
    from malavi_curation import verdicts
    declared = {value for name, value in vars(verdicts).items()
                if name.startswith("COL_") and isinstance(value, str)}
    assert declared == set(verdicts.COLUMNS_READ)


# ------------------------------------------------- the lead's consultation is kept

def test_a_lead_approval_keeps_who_was_consulted(fetch, entries, registry):
    """REGRESSION (A5, 2026-08-14 review): parsed into Action.consulted, then dropped on
    the way to the ledger. The form promises the lead approves "after discussing with
    you and potentially the author"; the ledger has to show that they did."""
    _flag_and_propose(fetch, entries, registry)
    apply(fetch, entries, registry,
          **{verdicts.COL_TIMESTAMP: "2026-08-03 09:00:00",
             verdicts.COL_EMAIL: "lead@example.edu",
             verdicts.COL_ACTION: verdicts.ACTION_APPROVE_CORRECTION,
             verdicts.COL_CORRECTION_ID: "C1",
             verdicts.COL_DISCUSSED_WITH: "Bob; the submitting authors",
             verdicts.COL_CONCLUDED: "Bob agrees the host was a synonym."})

    correction = entries[SUBMISSION].corrections[0]
    assert correction.approved_by == "lead"
    assert correction.approval_consulted == ["Bob", "the submitting authors"]
    assert correction.approval_note == "Bob agrees the host was a synonym."
    event = [h for h in entries[SUBMISSION].history
             if h["event"] == "correction_approved"][-1]
    assert event["consulted"] == ["Bob", "the submitting authors"]


# --------------------------------------------- an accept over a name somebody else holds

def test_an_accept_is_refused_when_another_submission_holds_the_name(fetch, entries,
                                                                    registry):
    """The verdict is recorded -- the curator did say Accept -- but the state does not
    move, and the outcome says why, naming the other submission."""
    entry = entries[SUBMISSION]
    entry.reserved_names = ["TUMIG19"]
    other = ledger.ensure_entry(entries, "MALAVI-SUB-2026-000002", "A",
                                "2026-07-01T00:00:00+00:00")
    other.reserved_names = ["TUMIG19"]
    ledger.transition(other, "ready_for_review", "intake", at="2026-07-01T12:00:00+00:00")

    outcome = apply(fetch, entries, registry)

    assert [v.verdict for v in entry.verdicts] == ["approve"]
    assert entry.state == "in_review", "picked up, but not approved"
    assert entry.approved_at is None
    assert "MALAVI-SUB-2026-000002" in str(outcome), outcome


# --------------------------------------------------- --from-csv goes through read_rows

def test_from_csv_refuses_a_repeated_header_too(fetch, tmp_path, monkeypatch):
    """REGRESSION (2026-09-02 review, finding 6): the local-CSV path used csv.DictReader
    directly, so the guard that protects the live sheet did not protect a saved export."""
    responses = tmp_path / "responses.csv"
    responses.write_text(
        "Timestamp,Submission id,Why is it being closed?,Why is it being closed?\n"
        "2026-08-14 10:00:00,MALAVI-SUB-2026-000001,It is already in MalAvi,\n",
        encoding="utf-8")
    monkeypatch.setattr(fetch, "load_config",
                        lambda: {**CONFIG, "submissions": {"inbox_dir": "inbox"}})
    monkeypatch.setattr(fetch, "repo_root", lambda: tmp_path)

    assert fetch.main(["--from-csv", str(responses), "--no-publish"]) == 1
    assert not (tmp_path / "inbox" / fetch.APPLIED_LEDGER_NAME).exists(), \
        "refused before anything was applied"
