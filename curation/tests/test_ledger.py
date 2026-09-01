"""Tests for the submission review ledger (ledger.py).

Each test here corresponds to a governance decision that was argued about. The point of
testing them is that a rule settled in a design document but broken in code is not settled,
and the way these rules break is quiet: a hold that stops blocking, an approval that
survives a change to the data, a clock that never runs out.

Several tests below are marked as regressions. They exist because an independent review on
2026-08-06 defeated the corresponding rule with an ordinary sequence of legitimate
operations, and because the test that was supposed to pin the rule passed for the wrong
reason. Where a test needs a second verdict to set up its scenario, take care that the
assertion cannot be satisfied by that second verdict alone.
"""
import pytest
import yaml

from malavi_curation import ledger
from malavi_curation.ledger import (
    LedgerError, agreed_names, approvals, blocking_holds, bump_revision, decision_record, due_actions,
    ensure_entry, is_approvable, load, open_ledger, override_hold, public_queue,
    record_verdict, retract_verdict, save, stale_live, standing_positions, transition, releasable, set_embargo,
)

# Two curators and a lead, so "another curator's hold" is expressible.
REGISTRY = [
    {"id": "lead", "name": "Lead Curator", "email": "lead@example.edu",
     "role": "lead", "active": True},
    {"id": "alice", "name": "Alice", "email": "alice@example.edu", "role": "curator"},
    {"id": "bob", "name": "Bob", "email": "bob@example.edu", "role": "curator"},
]

CLOCKS = {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60}


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "curators.yml"
    path.write_text(yaml.safe_dump({"curators": REGISTRY}), encoding="utf-8")
    return path


@pytest.fixture
def entry():
    entries = {}
    return ensure_entry(entries, "MALAVI-SUB-2026-000001", "A", "2026-08-01T00:00:00+00:00")


def approve(entry, registry, who="alice", at="2026-08-02T00:00:00+00:00", **kw):
    return record_verdict(entry, f"{who}@example.edu", "approve", at=at,
                          registry_path=registry, **kw)


def hold(entry, registry, who="bob", at="2026-08-02T01:00:00+00:00", text="Host synonym?"):
    return record_verdict(entry, f"{who}@example.edu", "hold",
                          reason_code="host_needs_review", reason_text=text,
                          at=at, registry_path=registry)


def to_review(entry, at="2026-08-01T12:00:00+00:00"):
    transition(entry, "ready_for_review", "intake", at=at)
    transition(entry, "in_review", "alice", at=at)


# ---------------------------------------------------------------------------- entries

def test_ensure_entry_is_idempotent(entry):
    entries = {entry.submission_id: entry}
    again = ensure_entry(entries, entry.submission_id, "A", "2026-09-09T00:00:00+00:00")
    assert again is entry
    assert len(entries) == 1


def test_resubmission_does_not_move_the_received_date(entry):
    """The name-reservation claim is earliest-timestamp-wins; re-filing must not cost it."""
    entries = {entry.submission_id: entry}
    ensure_entry(entries, entry.submission_id, "A", "2026-12-25T00:00:00+00:00")
    assert entry.received_at == "2026-08-01T00:00:00+00:00"


def test_unreadable_received_date_is_refused_at_creation():
    with pytest.raises(LedgerError, match="not a readable ISO 8601"):
        ensure_entry({}, "MALAVI-SUB-2026-000009", "A", "08/01/2026 12:00")


# ---------------------------------------------------------------------------- verdicts

def test_verdict_from_unknown_address_is_recorded_but_not_honored(entry, registry):
    result = record_verdict(entry, "stranger@example.com", "approve",
                            registry_path=registry)
    assert result is None
    assert entry.verdicts == []
    assert entry.unrecognized and entry.unrecognized[0]["address"] == "stranger@example.com"


def test_retired_curator_verdict_is_not_honored(tmp_path, entry):
    path = tmp_path / "curators.yml"
    path.write_text(yaml.safe_dump({"curators": [
        {"id": "past", "email": "past@example.edu", "role": "curator", "active": False}]}),
        encoding="utf-8")
    assert record_verdict(entry, "past@example.edu", "approve", registry_path=path) is None
    assert entry.unrecognized[0]["note"].startswith("curator is retired")


def test_hold_without_written_reasoning_is_refused(entry, registry):
    with pytest.raises(LedgerError, match="written reasoning"):
        record_verdict(entry, "bob@example.edu", "hold", registry_path=registry)


def test_approval_needs_no_written_reasoning(entry, registry):
    assert approve(entry, registry) is not None


def test_unreadable_verdict_timestamp_is_refused(entry, registry):
    """One bad stamp used to abort the promoter for every submission in the ledger."""
    with pytest.raises(LedgerError, match="not a readable ISO 8601"):
        approve(entry, registry, at="last Tuesday")


def test_latest_verdict_per_curator_stands_but_earlier_ones_are_kept(entry, registry):
    approve(entry, registry, "alice")
    record_verdict(entry, "alice@example.edu", "hold", reason_text="Changed my mind.",
                   at="2026-08-03T00:00:00+00:00", registry_path=registry)
    assert len(entry.verdicts) == 2                     # the history of the change survives
    assert [v.curator for v in blocking_holds(entry)] == ["alice"]


def test_standing_position_is_by_timestamp_not_append_order(entry, registry):
    """REGRESSION: a re-fetch or a backfill can append an older verdict after a newer one.

    Appended in the wrong order, Bob's later hold must still be his standing position — the
    alternative silently drops the objection from the gate while leaving it in the list
    looking recorded.
    """
    hold(entry, registry, "bob", at="2026-08-02T11:00:00+00:00")
    approve(entry, registry, "bob", at="2026-08-02T10:00:00+00:00")   # older, appended later
    assert standing_positions(entry)["bob"].verdict == "hold"
    assert [v.curator for v in blocking_holds(entry)] == ["bob"]


# ---------------------------------------------------------------------------- dissent

def test_a_hold_blocks_however_many_approvals_there_are(entry, registry):
    approve(entry, registry, "alice")
    approve(entry, registry, "lead")
    hold(entry, registry, "bob")
    approvable, why = is_approvable(entry)
    assert not approvable
    assert "bob" in why


def test_approval_alone_is_enough(entry, registry):
    approve(entry, registry, "alice")
    assert is_approvable(entry) == (True, "")


def test_no_verdicts_is_not_approvable(entry, registry):
    approvable, why = is_approvable(entry)
    assert not approvable and "no standing approval" in why


# ------------------------------------------------------------------- retract / override

def test_curator_may_retract_their_own_hold(entry, registry):
    approve(entry, registry, "alice")
    objection = hold(entry, registry, "bob")
    retract_verdict(entry, objection.id, "bob@example.edu", registry_path=registry)
    assert is_approvable(entry)[0]
    assert entry.verdicts[-1].retracted_by == "bob"     # kept, not deleted


def test_curator_may_not_retract_another_curators_hold(entry, registry):
    objection = hold(entry, registry, "bob")
    with pytest.raises(LedgerError, match="requires a lead"):
        retract_verdict(entry, objection.id, "alice@example.edu", registry_path=registry)


def test_an_approval_cannot_be_retracted(entry, registry):
    """REGRESSION: retracting an approval used to succeed and the approval kept counting."""
    given = approve(entry, registry, "alice")
    with pytest.raises(LedgerError, match="not an objection"):
        retract_verdict(entry, given.id, "alice@example.edu", registry_path=registry)


def test_a_resolved_approval_does_not_count(entry, registry):
    """REGRESSION: approvals() ignored `resolved` while blocking_holds() honored it."""
    given = approve(entry, registry, "alice")
    given.retracted_at = "2026-08-02T05:00:00+00:00"    # however it came to be resolved
    given.retracted_by = "alice"
    assert approvals(entry) == []
    assert not is_approvable(entry)[0]


def test_non_lead_cannot_override_a_hold(entry, registry):
    """If any curator could clear any hold, dissent would stop outranking approval."""
    objection = hold(entry, registry, "bob")
    with pytest.raises(LedgerError, match="not an active lead"):
        override_hold(entry, objection.id, "alice@example.edu", consulted=["bob"],
                      consulted_on="2026-08-03", consulted_how="email",
                      registry_path=registry)


def test_override_requires_a_consultation_record(entry, registry):
    objection = hold(entry, registry, "bob")
    with pytest.raises(LedgerError, match="who was consulted"):
        override_hold(entry, objection.id, "lead@example.edu", consulted=[],
                      consulted_on="", consulted_how="", registry_path=registry)


def test_lead_override_clears_the_hold_and_is_recorded(entry, registry):
    approve(entry, registry, "alice")
    objection = hold(entry, registry, "bob")
    override_hold(entry, objection.id, "lead@example.edu", consulted=["bob"],
                  consulted_on="2026-08-03", consulted_how="email",
                  note="Agreed the synonym is correct.", registry_path=registry)
    assert is_approvable(entry)[0]
    assert entry.overrides[0].by == "lead"
    assert entry.verdicts[-1].overridden_by == "lead"


def test_an_approval_cannot_be_overridden(entry, registry):
    given = approve(entry, registry, "alice")
    with pytest.raises(LedgerError, match="only an objection can be overridden"):
        override_hold(entry, given.id, "lead@example.edu", consulted=["alice"],
                      consulted_on="2026-08-03", consulted_how="email",
                      registry_path=registry)


def test_lead_must_retract_rather_than_override_their_own_hold(entry, registry):
    objection = hold(entry, registry, "lead")
    with pytest.raises(LedgerError, match="retract it rather than"):
        override_hold(entry, objection.id, "lead@example.edu", consulted=["alice"],
                      consulted_on="2026-08-03", consulted_how="email",
                      registry_path=registry)


# ---------------------------------------------------------------------------- revisions

def test_bumping_a_revision_clears_approvals(entry, registry):
    approve(entry, registry, "alice")
    bump_revision(entry, reason="Corrected host species.")
    approvable, why = is_approvable(entry)
    assert not approvable and "no standing approval" in why
    assert len(entry.verdicts) == 1          # the old approval is kept, just not standing


def test_a_hold_survives_a_revision_bump(entry, registry):
    """REGRESSION — the most important test in this file.

    An earlier version recorded a *second* hold on the new revision before asserting, so it
    passed even though the bump had silently dropped the first. Here nothing is recorded
    after the bump: the objection must still block on its own.
    """
    hold(entry, registry, "bob")
    bump_revision(entry, reason="Corrected a country spelling.")
    assert [v.curator for v in blocking_holds(entry)] == ["bob"]
    assert not is_approvable(entry)[0]


def test_a_hold_surviving_a_bump_still_blocks_release(entry, registry):
    """The full path the review walked: hold, correct, approve, wait, release."""
    hold(entry, registry, "bob")
    bump_revision(entry, reason="Corrected a country spelling.")
    approve(entry, registry, "alice", at="2026-08-03T00:00:00+00:00")
    to_review(entry)
    with pytest.raises(LedgerError, match="unresolved objection from bob"):
        transition(entry, "approved", "alice", at="2026-08-03T01:00:00+00:00")


def test_speaking_about_the_new_revision_supersedes_an_earlier_hold(entry, registry):
    """A carried objection is not permanent — it is superseded when its author re-engages."""
    hold(entry, registry, "bob")
    bump_revision(entry, reason="Fixed exactly what Bob asked about.")
    record_verdict(entry, "bob@example.edu", "approve", at="2026-08-03T00:00:00+00:00",
                   registry_path=registry)
    assert blocking_holds(entry) == []
    assert is_approvable(entry)[0]


def test_curator_typed_revision_records_who_and_on_whose_authority(entry, registry):
    revision = bump_revision(entry, reason="Host synonym corrected.",
                             revised_by="alice@example.edu", authority="curator",
                             consulted=["bob"], registry_path=registry)
    assert revision.revised_by == "alice"          # stored as the registry id
    assert revision.authority == "curator"
    assert revision.consulted == ["bob"]


def test_curator_typed_revision_cannot_claim_submitter_authority(entry, registry):
    with pytest.raises(LedgerError, match="cannot claim the submitter's authority"):
        bump_revision(entry, reason="x", revised_by="alice@example.edu",
                      authority="submitter", registry_path=registry)


def test_consulted_authority_requires_naming_the_reviser(entry, registry):
    with pytest.raises(LedgerError, match="revised_by must name that curator"):
        bump_revision(entry, reason="x", authority="author", registry_path=registry)


def test_unknown_authority_is_refused(entry, registry):
    with pytest.raises(LedgerError, match="Unknown authority"):
        bump_revision(entry, reason="x", authority="autor", registry_path=registry)


def test_unresolvable_reviser_is_refused(entry, registry):
    """REGRESSION: an unresolvable reviser used to disable the self-approval rule."""
    with pytest.raises(LedgerError, match="does not resolve to an active curator"):
        bump_revision(entry, reason="x", revised_by="Alice", authority="curator",
                      registry_path=registry)


def test_curator_cannot_approve_a_revision_they_typed(entry, registry):
    """Otherwise one person is corrector and approver, and the gate is one judgment."""
    bump_revision(entry, reason="Corrected after consulting the authors.",
                  revised_by="alice@example.edu", authority="author",
                  consulted=["the authors"], registry_path=registry)
    with pytest.raises(LedgerError, match="cannot also approve it"):
        record_verdict(entry, "alice@example.edu", "approve", registry_path=registry)


def test_self_approval_is_checked_against_the_revision_being_judged(entry, registry):
    """REGRESSION: the guard consulted the *current* revision's author, not the target's."""
    bump_revision(entry, reason="Alice typed this one.", revised_by="alice@example.edu",
                  authority="author", consulted=["the authors"], registry_path=registry)
    bump_revision(entry, reason="Submitter sent a replacement.")   # revised_by = ""
    with pytest.raises(LedgerError, match="authored revision 2"):
        record_verdict(entry, "alice@example.edu", "approve", revision=2,
                       registry_path=registry)


def test_curator_may_still_hold_a_revision_they_typed(entry, registry):
    bump_revision(entry, reason="Corrected.", revised_by="alice@example.edu",
                  authority="author", consulted=["the authors"], registry_path=registry)
    assert record_verdict(entry, "alice@example.edu", "hold",
                          reason_text="On reflection this needs another look.",
                          registry_path=registry) is not None


def test_a_verdict_cannot_name_a_revision_that_does_not_exist(entry, registry):
    """REGRESSION: a pre-recorded approval became standing at the next bump."""
    with pytest.raises(LedgerError, match="has no revision 2"):
        approve(entry, registry, "alice", revision=2)
    with pytest.raises(LedgerError, match="has no revision 99"):
        approve(entry, registry, "alice", revision=99)


def test_a_non_numeric_revision_is_filed_not_crashed(entry, registry):
    assert approve(entry, registry, "alice", revision="two") is None
    assert entry.unrecognized[0]["note"].startswith("revision is not a number")


def test_bumping_during_awaiting_submitter_stops_the_timeout(entry, registry):
    """REGRESSION: the submitter answered on day 59 and lost their names on day 60."""
    transition(entry, "ready_for_review", "intake", at="2026-01-02T00:00:00+00:00")
    transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")
    bump_revision(entry, reason="Submitter replied with a corrected workbook.",
                  at="2026-03-01T00:00:00+00:00")
    assert entry.awaiting_since is None
    assert entry.state == "in_review"
    assert due_actions({entry.submission_id: entry}, now="2026-06-01T00:00:00+00:00",
                       config=CLOCKS) == []


def test_revision_returns_an_approved_submission_to_review(entry, registry):
    approve(entry, registry, "alice")
    to_review(entry)
    transition(entry, "approved", "alice", at="2026-08-05T00:00:00+00:00")
    bump_revision(entry, reason="Replacement workbook from the submitter.")
    assert entry.state == "in_review"
    assert entry.approved_at is None


# ------------------------------------------------------------------------- transitions

def test_disallowed_transition_is_refused(entry):
    with pytest.raises(LedgerError, match="not an allowed transition"):
        transition(entry, "released", "lead")


def test_cannot_approve_over_a_standing_hold(entry, registry):
    approve(entry, registry, "alice")
    hold(entry, registry, "bob")
    to_review(entry)
    with pytest.raises(LedgerError, match="unresolved objection"):
        transition(entry, "approved", "alice")


def test_transition_reason_must_be_a_known_code(entry):
    """It reaches the committed record, which must carry no unpublished science."""
    to_review(entry)
    with pytest.raises(LedgerError, match="reason must be one of"):
        transition(entry, "declined", "lead",
                   reason="Sequence is a chimera of the submitter's Tanzanian isolate")


def _approved(registry, sid="MALAVI-SUB-2026-000002", at="2026-08-05T00:00:00+00:00"):
    entries = {}
    entry = ensure_entry(entries, sid, "A", "2026-08-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG19"]
    record_verdict(entry, "alice@example.edu", "approve", at=at, registry_path=registry)
    transition(entry, "ready_for_review", "intake", at=at)
    transition(entry, "in_review", "alice", at=at)
    transition(entry, "approved", "alice", at=at)
    return entries, entry


def test_release_is_refused_before_the_publish_hold_elapses(registry):
    """REGRESSION: released had no checks at all, and it is terminal."""
    _, entry = _approved(registry)
    with pytest.raises(LedgerError, match="publish hold has not elapsed"):
        transition(entry, "released", "lead", at="2026-08-05T12:00:00+00:00",
                   config=CLOCKS)


def test_release_is_refused_when_an_objection_arrived_after_the_scan(registry):
    """REGRESSION: the promoter's decision used to win over a later hold.

    This is the exact race the 24-hour window exists to permit: due_actions proposes a
    release, an objection lands seconds later, and the write must lose.
    """
    entries, entry = _approved(registry)
    assert [d.action for d in due_actions(entries, now="2026-08-06T06:00:00+00:00",
                                          config=CLOCKS)] == ["release_eligible"]
    record_verdict(entry, "bob@example.edu", "hold", reason_text="Wait — the locality.",
                   at="2026-08-06T06:00:30+00:00", registry_path=registry)
    with pytest.raises(LedgerError, match="unresolved objection from bob"):
        transition(entry, "released", "promoter", at="2026-08-06T06:01:00+00:00",
                   config=CLOCKS)


def test_release_confirms_the_reserved_names(registry):
    _, entry = _approved(registry)
    assert entry.name_state == "held"
    transition(entry, "released", "lead", at="2026-08-06T01:00:00+00:00",
               reason="released_in_build", config=CLOCKS)
    assert entry.state == "released"
    assert entry.name_state == "confirmed"
    assert entry.approved_at == "2026-08-05T00:00:00+00:00"   # what the release rested on


def test_declining_releases_the_reserved_names(entry, registry):
    entry.reserved_names = ["TUMIG19"]
    to_review(entry)
    transition(entry, "declined", "lead", reason="duplicate")
    assert entry.name_state == "released"
    assert entry.final_disposition["disposition"] == "declined"


def test_reopening_a_declined_submission_reclaims_its_names(entry, registry):
    """REGRESSION: a live submission advertised names its own name_state said were gone."""
    entry.reserved_names = ["TUMIG19"]
    to_review(entry)
    transition(entry, "declined", "lead", reason="duplicate")
    transition(entry, "in_review", "lead", reason="reopened")
    assert entry.name_state == "claimed"
    assert entry.final_disposition is None
    assert any(h["event"] == "reopened" for h in entry.history)


# ------------------------------------------------------------------------------ clocks

def test_publish_hold_not_yet_elapsed(registry):
    entries, _ = _approved(registry)
    assert due_actions(entries, now="2026-08-05T23:00:00+00:00", config=CLOCKS) == []


def test_publish_hold_elapsed_makes_it_release_eligible(registry):
    entries, _ = _approved(registry)
    due = due_actions(entries, now="2026-08-06T00:00:00+00:00", config=CLOCKS)
    assert [d.action for d in due] == ["release_eligible"]


def test_a_hold_inside_the_window_stops_the_release(registry):
    """The whole reason the 24-hour wait exists rather than being ceremonial."""
    entries, entry = _approved(registry)
    record_verdict(entry, "bob@example.edu", "hold", reason_text="Wait — the locality.",
                   at="2026-08-05T20:00:00+00:00", registry_path=registry)
    assert due_actions(entries, now="2026-08-07T00:00:00+00:00", config=CLOCKS) == []


def test_a_withdrawn_approval_stops_the_release(registry):
    """REGRESSION: due_actions checked only for holds, never for a standing approval."""
    entries, entry = _approved(registry)
    entry.verdicts[0].retracted_at = "2026-08-05T02:00:00+00:00"
    entry.verdicts[0].retracted_by = "alice"
    assert due_actions(entries, now="2026-08-07T00:00:00+00:00", config=CLOCKS) == []


def test_awaiting_submitter_times_out_after_sixty_days(registry):
    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000003", "A",
                         "2026-01-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG19"]
    transition(entry, "ready_for_review", "intake", at="2026-01-02T00:00:00+00:00")
    transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")

    assert due_actions(entries, now="2026-03-01T00:00:00+00:00", config=CLOCKS) == []
    due = due_actions(entries, now="2026-03-03T00:00:00+00:00", config=CLOCKS)
    assert [d.action for d in due] == ["timeout_dormant"]

    transition(entry, "dormant", "promoter", at="2026-03-03T00:00:00+00:00",
               reason="submitter_unresponsive")
    # CHANGED 2026-08-20: the timeout no longer takes the name back. Dormancy records that
    # we have stopped expecting a prompt answer, not that the submitter forfeited their
    # claim -- most often they are away resequencing because a curator asked them to.
    assert entry.name_state == "claimed"


def test_dormancy_does_not_release_a_reservation(registry):
    """The rule a resequencing submitter depends on.

    A curator rejects a sequence for an indel and asks for it to be re-read. That is bench
    work; it can easily outrun a 60-day clock. If the clock took NECMON01 back and somebody
    else were issued it, MalAvi would end up holding two different sequences under one name
    -- and the submitter, who was doing exactly what we asked, would be the one penalized.
    Only a decline returns a name now.
    """
    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000033", "A",
                         "2026-01-01T00:00:00+00:00")
    entry.reserved_names = ["NECMON01"]
    transition(entry, "ready_for_review", "intake")
    transition(entry, "awaiting_submitter", "alice")
    assert entry.name_state == "claimed"

    transition(entry, "dormant", "promoter", reason="submitter_unresponsive")
    assert entry.state == "dormant"
    assert entry.name_state == "claimed"
    assert entry.reserved_names == ["NECMON01"]

    # And the escape hatch: declining is what finally gives it back, so a name is not
    # locked away forever by a submission nobody will ever hear about again.
    transition(entry, "declined", "lead", reason="data_not_verifiable")
    assert entry.name_state == "released"


def test_answering_the_submitter_stops_the_timeout_clock(registry):
    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000004", "A",
                         "2026-01-01T00:00:00+00:00")
    transition(entry, "ready_for_review", "intake", at="2026-01-02T00:00:00+00:00")
    transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")
    transition(entry, "in_review", "alice", at="2026-01-10T00:00:00+00:00")
    assert entry.awaiting_since is None
    assert due_actions(entries, now="2026-06-01T00:00:00+00:00", config=CLOCKS) == []


def test_a_dormant_submission_can_be_revived_and_reclaims_its_names(registry):
    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000005", "A",
                         "2026-01-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG19"]
    transition(entry, "ready_for_review", "intake")
    transition(entry, "awaiting_submitter", "alice")
    transition(entry, "dormant", "promoter", reason="submitter_unresponsive")
    # Held throughout, not released and re-granted: see
    # test_dormancy_does_not_release_a_reservation.
    assert entry.name_state == "claimed"
    transition(entry, "in_review", "alice", reason="reopened")
    assert entry.state == "in_review"
    assert entry.name_state == "claimed"
    assert entry.final_disposition is None


def test_one_malformed_timestamp_does_not_stop_every_other_clock(registry):
    """REGRESSION: a single bad value aborted the whole promoter scan."""
    entries, good = _approved(registry, sid="MALAVI-SUB-2026-000006")
    bad = ensure_entry(entries, "MALAVI-SUB-2026-000007", "A",
                       "2026-08-01T00:00:00+00:00")
    bad.state = "approved"
    bad.approved_at = "whenever"              # as a hand edit could leave it
    due = due_actions(entries, now="2026-08-07T00:00:00+00:00", config=CLOCKS)
    assert {d.action for d in due} == {"release_eligible", "malformed"}


def test_zero_publish_hold_is_refused_rather_than_disabling_the_wait(registry):
    entries, _ = _approved(registry)
    with pytest.raises(LedgerError, match="must be positive"):
        due_actions(entries, config={"publish_hold_hours": 0,
                                     "awaiting_submitter_timeout_days": 60})


def test_stale_live_surfaces_a_forgotten_hold(registry):
    """The 60-day clock covers awaiting_submitter only; held has no clock of its own."""
    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000008", "A",
                         "2026-01-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG19"]
    transition(entry, "ready_for_review", "intake", at="2026-01-02T00:00:00+00:00")
    transition(entry, "in_review", "alice", at="2026-01-02T00:00:00+00:00")
    transition(entry, "held", "bob", at="2026-01-02T00:00:00+00:00")
    assert stale_live(entries, days=90, now="2026-02-01T00:00:00+00:00") == []
    stale = stale_live(entries, days=90, now="2026-06-01T00:00:00+00:00")
    assert [d.action for d in stale] == ["stale"]
    assert "TUMIG19" not in stale[0].because       # the names, not the name, are reported


# ----------------------------------------------------------------- the retained record

def test_decision_record_carries_no_reason_text(entry, registry):
    """It is committed; verdict prose quotes unpublished data and is not."""
    hold(entry, registry, "bob", text="The host looks like a synonym.")
    to_review(entry)
    transition(entry, "declined", "lead", reason="withdrawn_by_submitter")
    record = decision_record({entry.submission_id: entry})[0]
    assert "The host looks like a synonym." not in repr(record)
    assert record["verdicts"][0] == {"curator": "bob", "verdict": "hold", "revision": 1,
                                     "at": "2026-08-02T01:00:00+00:00", "resolved": False}


def test_decision_record_skips_undecided_submissions(entry):
    assert decision_record({entry.submission_id: entry}) == []


def test_decision_record_keeps_the_consultation_behind_an_override(registry):
    """REGRESSION: the evidence for an override lived only in the erasable copy."""
    entries, entry = _approved(registry)
    objection = record_verdict(entry, "bob@example.edu", "hold", reason_text="Locality?",
                               at="2026-08-05T01:00:00+00:00", registry_path=registry)
    override_hold(entry, objection.id, "lead@example.edu", consulted=["bob"],
                  consulted_on="2026-08-05", consulted_how="email",
                  note="Bob agreed on a call.", at="2026-08-05T02:00:00+00:00",
                  registry_path=registry)
    transition(entry, "released", "lead", at="2026-08-06T02:00:00+00:00",
               reason="released_in_build", config=CLOCKS)

    record = decision_record({entry.submission_id: entry})[0]
    assert record["disposition"] == "released"
    assert {v["verdict"] for v in record["verdicts"]} == {"approve", "hold"}
    assert record["overrides"][0]["consulted"] == ["bob"]
    assert record["overrides"][0]["consulted_on"] == "2026-08-05"
    assert record["reserved_names"] == ["TUMIG19"]
    assert "Bob agreed on a call." not in repr(record)      # the free-text note stays behind


def test_public_queue_coarsens_the_state(entry):
    """REGRESSION: publishing `held` announces dissent about a named person's claim."""
    entry.reserved_names = ["TUMIG19"]
    to_review(entry)
    transition(entry, "held", "bob")
    row = public_queue({entry.submission_id: entry})[0]
    assert set(row) == {"submission_id", "status", "received_at", "reserved_names",
                        "name_state"}
    assert row["status"] == "in_progress"


def test_public_queue_omits_finished_submissions(entry):
    transition(entry, "withdrawn", "submitter", reason="withdrawn_by_submitter")
    assert public_queue({entry.submission_id: entry}) == []


# ------------------------------------------------------------------------ persistence

def test_ledger_round_trips(tmp_path, entry, registry):
    approve(entry, registry, "alice")
    objection = hold(entry, registry, "bob")
    override_hold(entry, objection.id, "lead@example.edu", consulted=["bob"],
                  consulted_on="2026-08-03", consulted_how="email",
                  registry_path=registry)
    bump_revision(entry, reason="Corrected.", revised_by="alice@example.edu",
                  authority="author", registry_path=registry)
    entry.reserved_names = ["TUMIG19"]

    save(tmp_path, {entry.submission_id: entry})
    reloaded = load(tmp_path)[entry.submission_id]

    assert reloaded.revision == 2
    assert reloaded.received_at == entry.received_at
    assert reloaded.state == entry.state
    assert reloaded.name_state == entry.name_state
    assert reloaded.reserved_names == ["TUMIG19"]
    assert [v.id for v in reloaded.verdicts] == [v.id for v in entry.verdicts]
    # Verdict.resolved is derived from these three; a serialization regression would
    # silently un-resolve a hold, which is the failure this whole module guards against.
    assert [(v.retracted_at, v.retracted_by, v.overridden_by) for v in reloaded.verdicts] \
        == [(v.retracted_at, v.retracted_by, v.overridden_by) for v in entry.verdicts]
    assert reloaded.overrides[0].consulted == ["bob"]
    assert reloaded.revisions[-1].authority == "author"
    assert reloaded.history == entry.history


def test_missing_ledger_loads_empty(tmp_path):
    assert load(tmp_path) == {}


def test_unreadable_ledger_raises_rather_than_starting_fresh(tmp_path):
    """Starting fresh would discard every verdict, and nobody would notice until later."""
    ledger.ledger_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(LedgerError, match="would discard every verdict"):
        load(tmp_path)


def test_a_json_file_without_entries_is_corruption_not_an_empty_ledger(tmp_path):
    ledger.ledger_path(tmp_path).write_text("{}", encoding="utf-8")
    with pytest.raises(LedgerError, match="no 'entries' key"):
        load(tmp_path)


def test_a_future_schema_version_is_refused(tmp_path):
    ledger.ledger_path(tmp_path).write_text('{"version": 99, "entries": {}}',
                                            encoding="utf-8")
    with pytest.raises(LedgerError, match="schema version"):
        load(tmp_path)


def test_an_unknown_state_on_disk_is_refused(tmp_path, entry):
    save(tmp_path, {entry.submission_id: entry})
    path = ledger.ledger_path(tmp_path)
    path.write_text(path.read_text().replace('"received"', '"recieved"'), encoding="utf-8")
    with pytest.raises(LedgerError, match="unknown state"):
        load(tmp_path)


def test_duplicate_verdict_ids_on_disk_are_refused(tmp_path, entry, registry):
    approve(entry, registry, "alice")
    hold(entry, registry, "bob")
    save(tmp_path, {entry.submission_id: entry})
    path = ledger.ledger_path(tmp_path)
    path.write_text(path.read_text().replace('"V2"', '"V1"'), encoding="utf-8")
    with pytest.raises(LedgerError, match="duplicate verdict ids"):
        load(tmp_path)


def test_verdict_ids_come_from_the_highest_in_use(entry, registry):
    """A hand edit that deletes a row must not make the next write re-issue its id."""
    approve(entry, registry, "alice")
    hold(entry, registry, "bob")
    del entry.verdicts[0]
    new = record_verdict(entry, "lead@example.edu", "hold", reason_text="Also unsure.",
                         at="2026-08-04T00:00:00+00:00", registry_path=registry)
    assert new.id == "V3"


def test_save_refuses_to_overwrite_a_concurrent_write(tmp_path, entry, registry):
    """REGRESSION: the promoter used to silently delete a hold the fetcher had just made."""
    save(tmp_path, {entry.submission_id: entry})
    stale_stamp = ledger._stamp_of(ledger.ledger_path(tmp_path))

    other = load(tmp_path)
    hold(other[entry.submission_id], registry, "bob")
    save(tmp_path, other)                       # the fetcher's write lands

    with pytest.raises(LedgerError, match="changed on disk"):
        save(tmp_path, {entry.submission_id: entry}, expect=stale_stamp)

    assert load(tmp_path)[entry.submission_id].verdicts, "the hold must survive"


def test_open_ledger_writes_on_success(tmp_path, registry):
    with open_ledger(tmp_path) as entries:
        e = ensure_entry(entries, "MALAVI-SUB-2026-000010", "A",
                         "2026-08-01T00:00:00+00:00")
        approve(e, registry, "alice")
    assert load(tmp_path)["MALAVI-SUB-2026-000010"].verdicts


def test_open_ledger_writes_nothing_when_the_body_raises(tmp_path, registry):
    with pytest.raises(LedgerError):
        with open_ledger(tmp_path) as entries:
            ensure_entry(entries, "MALAVI-SUB-2026-000011", "A",
                         "2026-08-01T00:00:00+00:00")
            raise LedgerError("something went wrong part-way through")
    assert load(tmp_path) == {}


# ---------------------------------------------------------- name corrections

def test_agreed_names_applies_the_correction(entry):
    entry.reserved_names = ["TUMIG06", "TUMIG31"]
    entry.name_corrections = {"TUMIG06": "TUMIG25"}
    assert agreed_names(entry) == ["TUMIG25", "TUMIG31"]


def test_approval_adopts_the_corrected_name(registry):
    """Approving a submission approves the correction offered with it."""
    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000020", "A",
                         "2026-08-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG06"]
    entry.name_corrections = {"TUMIG06": "TUMIG25"}
    approve(entry, registry, "alice")
    to_review(entry)
    transition(entry, "approved", "alice", at="2026-08-05T00:00:00+00:00")

    assert entry.reserved_names == ["TUMIG25"], \
        "the name that is held must be the one that was agreed, not the taken one"
    assert entry.name_state == "held"


def test_a_correction_survives_into_the_release_and_the_record(registry):
    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000021", "A",
                         "2026-08-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG06"]
    entry.name_corrections = {"TUMIG06": "TUMIG25"}
    approve(entry, registry, "alice")
    to_review(entry)
    transition(entry, "approved", "alice", at="2026-08-05T00:00:00+00:00")
    transition(entry, "released", "lead", at="2026-08-06T01:00:00+00:00",
               reason="released_in_build", config=CLOCKS)

    assert entry.name_state == "confirmed"
    record = decision_record({entry.submission_id: entry})[0]
    assert record["reserved_names"] == ["TUMIG25"]


def test_corrections_round_trip(tmp_path, entry, registry):
    entry.reserved_names = ["TUMIG06"]
    entry.name_corrections = {"TUMIG06": "TUMIG25"}
    save(tmp_path, {entry.submission_id: entry})
    assert load(tmp_path)[entry.submission_id].name_corrections == {"TUMIG06": "TUMIG25"}


# ------------------------------------------------- unpublished work, held indefinitely

def test_an_embargoed_submission_is_approved_but_not_releasable(registry):
    """A submitter waiting on a journal must not be scooped with their own data."""
    from malavi_curation.ledger import releasable

    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000030", "A",
                         "2026-08-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG31"]
    entry.embargoed = True
    approve(entry, registry, "alice")
    to_review(entry)
    transition(entry, "approved", "alice", at="2026-08-05T00:00:00+00:00")

    assert entry.state == "approved"
    assert entry.name_state == "held", "the name stays reserved while the paper is pending"
    assert releasable(entries) == [], "an embargoed submission must not enter a release"


def test_lifting_the_embargo_makes_it_releasable(registry):
    from malavi_curation.ledger import releasable

    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000031", "A",
                         "2026-08-01T00:00:00+00:00")
    entry.embargoed = True
    approve(entry, registry, "alice")
    to_review(entry)
    transition(entry, "approved", "alice", at="2026-08-05T00:00:00+00:00")
    assert releasable(entries) == []

    entry.embargoed = False          # the paper came out
    assert releasable(entries) == ["MALAVI-SUB-2026-000031"]


def test_the_sixty_day_clock_does_not_run_on_embargoed_work(registry):
    """The timeout is for a submitter who has gone quiet, not one awaiting a journal.

    Applying it here would release somebody's reserved name while their paper is in
    review, which is the specific harm the reservation exists to prevent.
    """
    entries = {}
    entry = ensure_entry(entries, "MALAVI-SUB-2026-000032", "A",
                         "2026-01-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG31"]
    entry.embargoed = True
    transition(entry, "ready_for_review", "intake", at="2026-01-02T00:00:00+00:00")
    transition(entry, "awaiting_submitter", "alice", at="2026-01-02T00:00:00+00:00")

    assert due_actions(entries, now="2026-12-01T00:00:00+00:00", config=CLOCKS) == [], \
        "an embargoed submission must not go dormant, however long it waits"


def test_the_embargo_survives_a_round_trip(tmp_path, entry):
    entry.embargoed = True
    save(tmp_path, {entry.submission_id: entry})
    assert load(tmp_path)[entry.submission_id].embargoed is True


# ------------------------------------------------------------------- corrections

def test_a_correction_requires_a_standing_flag(entry, registry):
    from malavi_curation.ledger import record_correction

    with pytest.raises(LedgerError, match="requires a standing flag"):
        record_correction(entry, "alice@example.edu", "Host is Turdus merula.",
                          "author", ["the authors"], registry_path=registry)


def test_a_flagged_submission_can_take_a_correction(entry, registry):
    from malavi_curation.ledger import record_correction

    hold(entry, registry, "bob")
    c = record_correction(entry, "alice@example.edu", "Host is Turdus merula.",
                          "author", ["the authors"], registry_path=registry)
    assert c.id == "C1" and c.by == "alice" and not c.approved


def test_a_correction_is_not_applied_until_a_lead_approves(entry, registry):
    from malavi_curation.ledger import record_correction, pending_corrections

    hold(entry, registry, "bob")
    record_correction(entry, "alice@example.edu", "Host is Turdus merula.",
                      "author", ["the authors"], registry_path=registry)
    assert pending_corrections(entry) == [], "nothing may be applied without a lead"


def test_a_lead_approval_makes_it_applicable(entry, registry):
    from malavi_curation.ledger import (record_correction, approve_correction,
                                        pending_corrections)

    hold(entry, registry, "bob")
    c = record_correction(entry, "alice@example.edu", "Host is Turdus merula.",
                          "author", ["the authors"], registry_path=registry)
    approve_correction(entry, c.id, "lead@example.edu", registry_path=registry)
    assert [x.id for x in pending_corrections(entry)] == [c.id]


def test_a_plain_curator_cannot_approve_a_correction(entry, registry):
    from malavi_curation.ledger import record_correction, approve_correction

    hold(entry, registry, "bob")
    c = record_correction(entry, "alice@example.edu", "Host is Turdus merula.",
                          "author", ["the authors"], registry_path=registry)
    with pytest.raises(LedgerError, match="not an active lead"):
        approve_correction(entry, c.id, "bob@example.edu", registry_path=registry)


def test_a_lead_cannot_approve_their_own_correction(entry, registry):
    """Otherwise one person describes a change and has it applied unreviewed."""
    from malavi_curation.ledger import record_correction, approve_correction

    hold(entry, registry, "bob")
    c = record_correction(entry, "lead@example.edu", "Host is Turdus merula.",
                          "author", ["the authors"], registry_path=registry)
    with pytest.raises(LedgerError, match="cannot also approve it"):
        approve_correction(entry, c.id, "lead@example.edu", registry_path=registry)


def test_corrections_round_trip(tmp_path, entry, registry):
    from malavi_curation.ledger import record_correction, approve_correction

    hold(entry, registry, "bob")
    c = record_correction(entry, "alice@example.edu", "Host is Turdus merula.",
                          "author", ["the authors"], consulted_on="2026-08-06",
                          registry_path=registry)
    approve_correction(entry, c.id, "lead@example.edu", registry_path=registry)
    save(tmp_path, {entry.submission_id: entry})
    back = load(tmp_path)[entry.submission_id].corrections[0]
    assert (back.by, back.approved_by, back.authority) == ("alice", "lead", "author")


# ------------------------------------------------------------------- embargo at the write
#
# transition() promises "every blocking rule is re-checked here". The embargo was not
# among them until 2026-08-10, which made that promise untrue of the one rule protecting
# a submitter's unpublished data.

def test_an_embargoed_submission_cannot_be_released(entry, registry):
    to_review(entry)
    approve(entry, registry)
    transition(entry, "approved", "alice", at="2026-08-03T00:00:00+00:00", config=CLOCKS)
    entry.embargoed = True
    with pytest.raises(LedgerError, match="embargoed"):
        transition(entry, "released", "maintainer", at="2026-08-05T00:00:00+00:00",
                   reason="released_in_build", config=CLOCKS)
    assert entry.state == "approved"


def test_lifting_the_embargo_lets_it_release(entry, registry):
    """The refusal is about the flag, not about the submission."""
    to_review(entry)
    approve(entry, registry)
    transition(entry, "approved", "alice", at="2026-08-03T00:00:00+00:00", config=CLOCKS)
    entry.embargoed = True
    set_embargo(entry, False, actor="maintainer", at="2026-08-04T00:00:00+00:00")
    transition(entry, "released", "maintainer", at="2026-08-05T00:00:00+00:00",
               reason="released_in_build", config=CLOCKS)
    assert entry.state == "released"


def test_due_actions_does_not_offer_an_embargoed_submission(entry, registry):
    """It disagreed with releasable(), so promote.py told the operator it was ready."""
    to_review(entry)
    approve(entry, registry)
    transition(entry, "approved", "alice", at="2026-08-03T00:00:00+00:00", config=CLOCKS)
    entry.embargoed = True
    entries = {entry.submission_id: entry}
    actions = [a.action for a in due_actions(entries, now="2026-08-05T00:00:00+00:00",
                                             config=CLOCKS)]
    assert "release_eligible" not in actions
    assert releasable(entries) == []


# --------------------------------------------------------------- id minting after a delete
#
# The ledger is a hand-editable file. Corrections minted ids by counting until 2026-08-10,
# in the same module that explains why counting is wrong for verdicts.

def test_a_correction_id_is_not_reissued_after_a_hand_delete(entry, registry):
    """approve_correction returns the FIRST match, so a duplicate id gets the wrong one."""
    to_review(entry)
    hold(entry, registry)
    from malavi_curation.ledger import record_correction
    first = record_correction(entry, "alice@example.edu", "Host is Turdus merula.",
                              "curator", ["bob"], registry_path=registry)
    second = record_correction(entry, "alice@example.edu", "Country is Sweden.",
                               "curator", ["bob"], registry_path=registry)
    assert (first.id, second.id) == ("C1", "C2")

    # Somebody edits the file and removes the first correction.
    entry.corrections = [c for c in entry.corrections if c.id != "C1"]
    third = record_correction(entry, "alice@example.edu", "Site is Krankesjon.",
                              "curator", ["bob"], registry_path=registry)
    assert third.id == "C3"
    assert len({c.id for c in entry.corrections}) == len(entry.corrections)


def test_a_verdict_id_is_not_reissued_after_a_hand_delete(entry, registry):
    """The rule the correction path was missing, still holding for verdicts.

    Deleting the LAST id is not the hazard -- there is then nothing to collide with, and
    reusing it is correct. The hazard is deleting an earlier one while a higher id
    survives, which is what counting would step on.
    """
    to_review(entry)
    first = approve(entry, registry, who="alice")
    second = hold(entry, registry, who="bob")
    assert (first.id, second.id) == ("V1", "V2")

    entry.verdicts = [v for v in entry.verdicts if v.id != first.id]
    third = approve(entry, registry, who="lead")
    assert third.id == "V3"
    assert len({v.id for v in entry.verdicts}) == len(entry.verdicts)


def test_a_resubmission_records_the_state_it_moved_from(entry, registry):
    """Anything reconstructing the timeline from history must see the step.

    Without it, an audit reads a submission jumping from approved to held with nothing
    between, which looks like a lost record rather than a revision.
    """
    to_review(entry)
    approve(entry, registry)
    transition(entry, "approved", "alice", at="2026-08-03T00:00:00+00:00", config=CLOCKS)

    bump_revision(entry, authority="submitter", reason="corrected the host",
                  at="2026-08-04T00:00:00+00:00")

    assert entry.state == "in_review"
    moves = [e for e in entry.history if e["event"] == "state"]
    assert moves[-1]["from"] == "approved" and moves[-1]["to"] == "in_review"


def test_a_resubmission_that_changes_no_state_records_no_move(entry, registry):
    to_review(entry)
    before = len([e for e in entry.history if e["event"] == "state"])
    bump_revision(entry, authority="submitter", reason="more data",
                  at="2026-08-04T00:00:00+00:00")
    assert entry.state == "in_review"
    assert len([e for e in entry.history if e["event"] == "state"]) == before


# ------------------------------------------------------------------------ the clocks

def test_the_clocks_accept_the_shape_every_caller_actually_passes():
    """Regression, 2026-08-13: the review clocks were never configurable.

    ``_review_config`` understood only the ``review`` *section* of config/project.yml, and
    every caller in the repository passed the **whole config** — promote.py,
    fetch_verdicts.py, notify_submitters.py, release_gate.py. So every lookup missed and
    every caller silently got the defaults back.

    It was invisible because the defaults, 24 hours and 60 days, are exactly what
    config/project.yml says. The day anyone lengthened the publish hold as a governance
    decision, the release gate and the submitter notices would have carried on using 24
    hours with nothing anywhere saying so. Hence a non-default value here.
    """
    whole = {"review": {"publish_hold_hours": 48, "awaiting_submitter_timeout_days": 90},
             "submissions": {"inbox_dir": "inbox"}}

    assert ledger._review_config(whole) == {"publish_hold_hours": 48,
                                            "awaiting_submitter_timeout_days": 90}
    # Both shapes are in use — the tests here pass the section, the programs pass the
    # whole config — and the two cannot be confused: a review section never contains a
    # key called "review".
    assert ledger._review_config(whole["review"]) == ledger._review_config(whole)


def test_a_bad_clock_is_refused_through_either_shape():
    """The validation must not be skipped by the shape that unwraps."""
    for supplied in ({"publish_hold_hours": 0}, {"review": {"publish_hold_hours": 0}}):
        with pytest.raises(LedgerError, match="must be positive"):
            ledger._review_config(supplied)
    for supplied in ({"publish_hold_hours": "soon"},
                     {"review": {"publish_hold_hours": "soon"}}):
        with pytest.raises(LedgerError, match="must be a whole number"):
            ledger._review_config(supplied)


def test_a_configured_hold_really_reaches_the_release_check(entry, registry):
    """End to end: the number transition() enforces is the number the caller configured.

    The unit test above would pass on a fix that repaired ``_review_config`` and nothing
    else. This one fails unless the value survives the whole way to the rule that uses it.
    """
    to_review(entry)
    approve(entry, registry)
    transition(entry, "approved", "alice", at="2026-08-03T00:00:00+00:00", config=CLOCKS)
    longer = {"review": {"publish_hold_hours": 48,
                         "awaiting_submitter_timeout_days": 60}}

    # 30 hours later: past the default 24, short of the configured 48.
    with pytest.raises(LedgerError, match="48h publish hold has not elapsed"):
        transition(entry, "released", "promoter", at="2026-08-04T06:00:00+00:00",
                   reason="released_in_build", config=longer)

    transition(entry, "released", "promoter", at="2026-08-05T06:00:00+00:00",
               reason="released_in_build", config=longer)
    assert entry.state == "released"


# ------------------------------------------------------------------ closing for good

def test_only_a_lead_can_close_a_submission(entry, registry):
    """A rejection already gets a second look by landing on 'held'.

    Ending it there should not rest on the same single judgment, and ``transition`` alone
    would not have asked: it takes ``actor`` as free text and never resolves it, which is
    right for the promoter and wrong for this.
    """
    to_review(entry)
    hold(entry, registry, "bob")
    transition(entry, "held", "bob", at="2026-08-02T02:00:00+00:00")

    with pytest.raises(LedgerError, match="not an active lead curator"):
        ledger.decline(entry, "alice@example.edu", reason="unresolved_objection",
                       at="2026-08-10T00:00:00+00:00", registry_path=registry)
    assert entry.state == "held", "nothing moved"

    ledger.decline(entry, "lead@example.edu", reason="unresolved_objection",
                   at="2026-08-10T00:00:00+00:00", registry_path=registry)
    assert entry.state == "declined"
    assert entry.final_disposition["by"] == "lead", "attributed to the id, not the address"
    assert entry.final_disposition["reason_code"] == "unresolved_objection"
    assert entry.name_state == "released"


def test_a_close_reason_outside_the_decline_vocabulary_is_refused(entry, registry):
    """The codes left out describe other ways a submission ends and would be false here."""
    to_review(entry)
    hold(entry, registry, "bob")
    transition(entry, "held", "bob", at="2026-08-02T02:00:00+00:00")

    for wrong in ("submitter_unresponsive", "released_in_build", "withdrawn_by_submitter",
                  "reopened", ""):
        with pytest.raises(LedgerError, match="reason must be one of"):
            ledger.decline(entry, "lead@example.edu", reason=wrong,
                           at="2026-08-10T00:00:00+00:00", registry_path=registry)
        assert entry.state == "held"


def test_an_approved_submission_cannot_be_closed_without_a_flag(entry, registry):
    """A decline follows an objection; it does not replace one.

    The state machine allows approved -> held and held -> declined, but not
    approved -> declined, so somebody has to flag it and that flag is attributed to them.
    """
    to_review(entry)
    approve(entry, registry)
    transition(entry, "approved", "alice", at="2026-08-03T00:00:00+00:00", config=CLOCKS)

    with pytest.raises(LedgerError, match="not an allowed transition"):
        ledger.decline(entry, "lead@example.edu", reason="out_of_scope",
                       at="2026-08-10T00:00:00+00:00", registry_path=registry)
    assert entry.state == "approved"


def test_a_closing_note_stays_out_of_the_decision_record(entry, registry):
    """The note is free text. The decision record must carry no unpublished science."""
    to_review(entry)
    hold(entry, registry, "bob")
    transition(entry, "held", "bob", at="2026-08-02T02:00:00+00:00")

    ledger.decline(entry, "lead@example.edu", reason="duplicate",
                   note="Same records as Barrow et al, already in MalAvi.",
                   at="2026-08-10T00:00:00+00:00", registry_path=registry)

    notes = [e for e in entry.history if e.get("event") == "decline_note"]
    assert len(notes) == 1 and "Barrow" in notes[0]["note"]

    record = ledger.decision_record({entry.submission_id: entry})
    assert record, "the submission should appear in the decision record"
    serialized = str(record)
    assert "Barrow" not in serialized, "the note must not reach the committed record"
    assert "duplicate" in serialized
