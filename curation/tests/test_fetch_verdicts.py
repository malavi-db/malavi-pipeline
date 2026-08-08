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
                    verdicts.COL_CHANGE: "Turdus migratorius, not Turdus migratoria.",
                    verdicts.COL_FLAGGED: "Yes"})


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
