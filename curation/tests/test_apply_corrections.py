"""Tests for apply_corrections.py — the step that turns an approved correction into a
revision.

Until 2026-08-13 this program ran on a hard-coded empty list, so nothing here was ever
exercised against a real ledger. The tests below are written around the failure modes that
matters most for a program that edits what MalAvi will publish about somebody else's study:
applying a correction twice, applying one nobody approved, applying one whose flag has
since been withdrawn, and — the specific shape of the original stub — marking several
corrections applied on the strength of a single revision.
"""
from __future__ import annotations

import importlib.util

import pytest
import yaml

from malavi_curation import curators as curators_mod
from malavi_curation import ledger
from malavi_curation.config import repo_root


@pytest.fixture(scope="module")
def apply_corrections():
    """The operator program, loaded from its path — it is a script, not an installed module."""
    path = repo_root() / "curation" / "apply_corrections.py"
    spec = importlib.util.spec_from_file_location("_apply_corrections", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A lead (who may approve a correction), the curator who proposes it, and a second curator
# whose flag is what makes a correction admissible in the first place.
REGISTRY = [
    {"id": "lead", "name": "Lead Curator", "email": "lead@example.edu",
     "role": "lead", "active": True},
    {"id": "alice", "name": "Alice", "email": "alice@example.edu", "role": "curator"},
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


@pytest.fixture
def registry(registry_path):
    """The ``{curator id: Curator}`` mapping the program resolves proposers through."""
    return curators_mod.load_registry(registry_path)


@pytest.fixture
def flagged(registry_path):
    """A submission in review with Bob's flag standing on it.

    A flag is the precondition for every correction, so almost every test needs one. Bob
    places it rather than Alice, because Alice is the one proposing the correction and a
    curator correcting their own flag would not exercise the rule that a fix and an
    acceptance are separate acts.
    """
    entries = {}
    entry = ledger.ensure_entry(entries, SUBMISSION, "A", "2026-08-01T00:00:00+00:00")
    ledger.transition(entry, "ready_for_review", "intake", at="2026-08-01T12:00:00+00:00")
    ledger.transition(entry, "in_review", "alice", at="2026-08-02T00:00:00+00:00")
    ledger.record_verdict(entry, "bob@example.edu", "hold",
                          reason_code="host_needs_review", reason_text="Host synonym?",
                          at="2026-08-02T01:00:00+00:00", registry_path=registry_path)
    return entry


def propose(entry, registry_path, change="Host is Turdus merula.", by="alice",
            authority="author", consulted=("the authors",),
            at="2026-08-03T00:00:00+00:00"):
    return ledger.record_correction(entry, f"{by}@example.edu", change, authority,
                                    list(consulted), at=at, registry_path=registry_path)


def approve(entry, correction, registry_path, by="lead", at="2026-08-04T00:00:00+00:00"):
    return ledger.approve_correction(entry, correction.id, f"{by}@example.edu", at=at,
                                     registry_path=registry_path)


# ------------------------------------------------------------------ the happy path

def test_an_approved_correction_becomes_a_revision(apply_corrections, flagged, registry,
                                                   registry_path):
    correction = propose(flagged, registry_path)
    approve(flagged, correction, registry_path)
    before = flagged.revision

    line = apply_corrections.apply_correction(flagged, correction, registry,
                                              at="2026-08-05T00:00:00+00:00",
                                              registry_path=registry_path)

    assert flagged.revision == before + 1
    assert flagged.revisions[-1].reason == "Host is Turdus merula."
    assert flagged.revisions[-1].revised_by == "alice", \
        "the revision is attributed to the curator who proposed the correction"
    assert flagged.revisions[-1].authority == "author"
    assert flagged.revisions[-1].consulted == ["the authors"]
    assert correction.applied_at == "2026-08-05T00:00:00+00:00"
    assert "now revision 2" in line


def test_applying_a_correction_clears_standing_approvals(apply_corrections, flagged,
                                                         registry, registry_path):
    """The rule the whole revision mechanism exists to enforce.

    A curator who approved revision 1 did not approve the corrected revision 2, however
    small the change looks. If this ever stopped being true, a correction could ride into a
    release on an approval given before it existed.
    """
    ledger.record_verdict(flagged, "lead@example.edu", "approve",
                          at="2026-08-03T06:00:00+00:00", registry_path=registry_path)
    assert ledger.approvals(flagged), "the scenario needs a standing approval to clear"

    correction = propose(flagged, registry_path)
    approve(flagged, correction, registry_path)
    apply_corrections.apply_correction(flagged, correction, registry,
                                       at="2026-08-05T00:00:00+00:00",
                                       registry_path=registry_path)

    assert ledger.approvals(flagged) == []


# ------------------------------------------------------------------ not applied twice

def test_an_applied_correction_is_not_pending_again(apply_corrections, flagged, registry,
                                                    registry_path):
    """Idempotency comes from the ledger, not from a second applied-file to keep in step.

    ``applied_at`` is set by this program and read by ``pending_corrections``, so a re-run
    finds nothing to do. That is the whole mechanism; if it broke, every run would bump the
    revision again and clear approvals that had just been given.
    """
    correction = propose(flagged, registry_path)
    approve(flagged, correction, registry_path)
    entries = {SUBMISSION: flagged}

    first = apply_corrections.pending_work(entries)
    assert len(first) == 1
    apply_corrections.apply_correction(flagged, correction, registry,
                                       at="2026-08-05T00:00:00+00:00",
                                       registry_path=registry_path)

    assert apply_corrections.pending_work(entries) == []
    assert flagged.revision == 2, "a second run must not bump the revision again"


def test_two_corrections_get_two_revisions_each_with_its_own_authority(
        apply_corrections, flagged, registry, registry_path):
    """The bug the original stub would have shipped.

    It marked *every* approved correction applied from a single revision, so the second and
    third on a submission would have been recorded as done and never applied. They also
    disagree on authority — one confirmed with the authors, one settled between curators —
    which is why they cannot be merged into one revision even in principle.
    """
    first = propose(flagged, registry_path, change="Host is Turdus merula.",
                    authority="author", consulted=("the authors",))
    second = propose(flagged, registry_path, change="Country spelled Tanzania.",
                     authority="curator", consulted=("Bob",),
                     at="2026-08-03T01:00:00+00:00")
    approve(flagged, first, registry_path)
    approve(flagged, second, registry_path, at="2026-08-04T01:00:00+00:00")

    entries = {SUBMISSION: flagged}
    work = apply_corrections.pending_work(entries)
    assert [c.id for _, c in work] == ["C1", "C2"], "applied in the order proposed"

    for entry, correction in work:
        apply_corrections.apply_correction(entry, correction, registry,
                                           registry_path=registry_path)

    assert flagged.revision == 3, "one revision per correction"
    assert [r.reason for r in flagged.revisions[-2:]] == ["Host is Turdus merula.",
                                                          "Country spelled Tanzania."]
    assert [r.authority for r in flagged.revisions[-2:]] == ["author", "curator"]
    assert first.applied and second.applied


# ------------------------------------------------------------------ what it refuses

def test_a_correction_no_lead_approved_is_never_applied(apply_corrections, flagged,
                                                        registry_path):
    correction = propose(flagged, registry_path)
    entries = {SUBMISSION: flagged}

    assert apply_corrections.pending_work(entries) == []
    waiting = apply_corrections.awaiting_approval(entries)
    assert [c.id for _, c in waiting] == [correction.id], \
        "an unapproved correction is reported, so it cannot sit invisible"


def test_a_correction_is_refused_once_its_flag_is_withdrawn(apply_corrections, flagged,
                                                            registry, registry_path):
    """A flag can be retracted in the days between a lead's approval and this run.

    At that point the submission is back in ordinary review and a curator may already have
    accepted it. Revising it now would change a version somebody has signed off on.
    """
    correction = propose(flagged, registry_path)
    approve(flagged, correction, registry_path)
    held = ledger.blocking_holds(flagged)[0]
    ledger.retract_verdict(flagged, held.id, "bob@example.edu",
                           at="2026-08-04T12:00:00+00:00", registry_path=registry_path)

    line = apply_corrections.apply_correction(flagged, correction, registry,
                                              at="2026-08-05T00:00:00+00:00",
                                              registry_path=registry_path)

    assert "REFUSED" in line and "no flag is standing" in line
    assert flagged.revision == 1, "nothing was revised"
    assert not correction.applied, "and it stays pending rather than being consumed"


def test_a_proposer_who_left_the_registry_is_reported_not_applied(apply_corrections,
                                                                  flagged, registry_path):
    """A revision must have somebody accountable for it.

    ``bump_revision`` refuses an unresolvable author, and it would raise — which on a batch
    of fifty corrections would abandon the forty-nine behind it. So the lookup happens here
    and an unknown proposer is a printed outcome.
    """
    correction = propose(flagged, registry_path)
    approve(flagged, correction, registry_path)
    registry_without_alice = {curator_id: curator
                              for curator_id, curator
                              in curators_mod.load_registry(registry_path).items()
                              if curator_id != "alice"}

    line = apply_corrections.apply_correction(flagged, correction,
                                              registry_without_alice,
                                              registry_path=registry_path)

    assert "SKIPPED" in line and "not in config/curators.yml" in line
    assert flagged.revision == 1 and not correction.applied


def test_a_deactivated_proposer_is_refused_rather_than_raising(apply_corrections, flagged,
                                                               registry, tmp_path):
    """Alice is still listed but no longer active — ``bump_revision`` raises on that."""
    deactivated = tmp_path / "curators_deactivated.yml"
    deactivated.write_text(yaml.safe_dump({"curators": [
        dict(curator, active=False) if curator["id"] == "alice" else curator
        for curator in REGISTRY]}), encoding="utf-8")
    # The correction itself has to be recorded while she was still active, which is the
    # real sequence: she proposed it, then left.
    active = tmp_path / "curators.yml"
    correction = propose(flagged, active)
    approve(flagged, correction, active)

    line = apply_corrections.apply_correction(flagged, correction, registry,
                                              registry_path=deactivated)

    assert "REFUSED" in line
    assert flagged.revision == 1 and not correction.applied


# ------------------------------------------------------------------ the program itself

def _workspace(apply_corrections, monkeypatch, tmp_path, registry_path):
    """Point the program at a temporary repo root, inbox and curator registry."""
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    monkeypatch.setattr(apply_corrections, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(apply_corrections, "load_config", lambda: CONFIG)
    # Only `registry_path` is redirected, not `load_registry`. The program and this test
    # hold the *same* module object, so patching load_registry with a lambda that calls
    # load_registry makes it call itself. Redirecting where the registry lives is both
    # sufficient — load_registry(None) consults registry_path() — and honest about what
    # differs in a test: the file, not the loader.
    monkeypatch.setattr(curators_mod, "registry_path", lambda: registry_path)
    return inbox


def test_a_dry_run_writes_nothing(apply_corrections, flagged, registry_path, tmp_path,
                                  monkeypatch, capsys):
    inbox = _workspace(apply_corrections, monkeypatch, tmp_path, registry_path)
    correction = propose(flagged, registry_path)
    approve(flagged, correction, registry_path)
    ledger.save(inbox, {SUBMISSION: flagged})

    assert apply_corrections.main([]) == 0
    output = capsys.readouterr().out

    assert "now revision 2" in output, "a dry run still shows the revision it would create"
    assert "[dry-run] nothing was written" in output
    reloaded = ledger.load(inbox)[SUBMISSION]
    assert reloaded.revision == 1 and not reloaded.corrections[0].applied


def test_apply_writes_the_revision(apply_corrections, flagged, registry_path, tmp_path,
                                   monkeypatch, capsys):
    inbox = _workspace(apply_corrections, monkeypatch, tmp_path, registry_path)
    correction = propose(flagged, registry_path)
    approve(flagged, correction, registry_path)
    ledger.save(inbox, {SUBMISSION: flagged})

    assert apply_corrections.main(["--apply"]) == 0
    capsys.readouterr()

    reloaded = ledger.load(inbox)[SUBMISSION]
    assert reloaded.revision == 2
    assert reloaded.corrections[0].applied
    assert reloaded.revisions[-1].reason == "Host is Turdus merula."

    # And a second run finds nothing, which is the property that matters most here.
    assert apply_corrections.main(["--apply"]) == 0
    assert "No corrections are waiting." in capsys.readouterr().out
    assert ledger.load(inbox)[SUBMISSION].revision == 2


def test_the_submission_filter_leaves_the_others_alone(apply_corrections, flagged,
                                                       registry_path, tmp_path,
                                                       monkeypatch, capsys):
    inbox = _workspace(apply_corrections, monkeypatch, tmp_path, registry_path)
    other_entries = {}
    other = ledger.ensure_entry(other_entries, "MALAVI-SUB-2026-000002", "A",
                                "2026-08-01T00:00:00+00:00")
    ledger.transition(other, "ready_for_review", "intake", at="2026-08-01T12:00:00+00:00")
    ledger.transition(other, "in_review", "alice", at="2026-08-02T00:00:00+00:00")
    ledger.record_verdict(other, "bob@example.edu", "hold", reason_code="host_needs_review",
                          reason_text="?", at="2026-08-02T01:00:00+00:00",
                          registry_path=registry_path)
    for entry in (flagged, other):
        correction = propose(entry, registry_path)
        approve(entry, correction, registry_path)
    ledger.save(inbox, {SUBMISSION: flagged, other.submission_id: other})

    assert apply_corrections.main(["--apply", "--submission", SUBMISSION]) == 0
    capsys.readouterr()

    reloaded = ledger.load(inbox)
    assert reloaded[SUBMISSION].revision == 2
    assert reloaded["MALAVI-SUB-2026-000002"].revision == 1
    assert not reloaded["MALAVI-SUB-2026-000002"].corrections[0].applied
