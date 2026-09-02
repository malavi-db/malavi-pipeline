"""The gate that stops a release carrying records nobody approved.

This is the invariant the review apparatus exists to serve, so these tests are written
against the failure rather than the success: what a release must REFUSE to do.
"""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from malavi_curation import ledger, release_gate
from malavi_curation.release_gate import NO_SOURCE


# --------------------------------------------------------------------------- fixtures

def store_with(*sources):
    """A minimal store whose host_records carry the given ``_source`` values."""
    return {"host_records": [{"LINEAGE_NAME": f"TUMIG{i:02d}", "_source": source}
                             for i, source in enumerate(sources, start=1)],
            "lineages": []}


REGISTRY = [
    {"id": "lead", "name": "Lead Curator", "email": "lead@example.edu",
     "role": "lead", "active": True},
    {"id": "alice", "name": "Alice", "email": "alice@example.edu", "role": "curator"},
]

CLOCKS = {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60}


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "curators.yml"
    path.write_text(yaml.safe_dump({"curators": REGISTRY}), encoding="utf-8")
    return path


def approved_entry(registry, submission_id="MALAVI-SUB-2026-000123", *, hours_ago=48):
    """An entry a curator approved, walked through the states the machine requires.

    ``hours_ago`` sets how long ago the approval happened, which is what decides whether
    the publish hold has elapsed. It is measured from now rather than fixed, because the
    hold is measured against the wall clock at the moment of the release build.
    """
    approved_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
                   ).replace(microsecond=0).isoformat()
    entry = ledger.ensure_entry({}, submission_id, "A", "2026-08-01T00:00:00+00:00")
    ledger.transition(entry, "ready_for_review", "intake", at="2026-08-01T12:00:00+00:00")
    ledger.transition(entry, "in_review", "alice", at="2026-08-01T12:00:00+00:00")
    ledger.record_verdict(entry, "alice@example.edu", "approve",
                          at="2026-08-01T13:00:00+00:00", registry_path=registry)
    ledger.transition(entry, "approved", "alice", at=approved_at, config=CLOCKS)
    return entry


# ------------------------------------------------------------------------- seed rows

def test_a_seed_only_store_needs_no_ledger():
    """The rows Staffan released predate this project's review; there is nothing to find.

    Requiring a curator decision for them would either block every release or invite a
    fiction in the ledger, and a fiction in the ledger is worse than an honest exemption.
    """
    result = release_gate.check(store_with("seed", "seed"), entries=None)
    assert result.ok
    assert result.exempt_rows == 2
    assert result.publishing == []


# --------------------------------------------------------- rows nobody can account for

def test_a_row_with_no_source_is_refused():
    result = release_gate.check(store_with("seed", ""), entries={})
    assert not result.ok
    assert result.violations[0].source == NO_SOURCE
    assert "no _source" in result.violations[0].reason


def test_a_row_naming_an_unknown_submission_is_refused():
    result = release_gate.check(store_with("MALAVI-SUB-2026-000999"), entries={})
    assert not result.ok
    assert "no such submission" in result.violations[0].reason


def test_a_submission_row_with_no_ledger_at_all_is_refused():
    """A missing ledger excuses seed rows; it cannot excuse a row claiming a review."""
    result = release_gate.check(store_with("MALAVI-SUB-2026-000123"), entries=None)
    assert not result.ok
    assert "no review ledger" in result.violations[0].reason


# ------------------------------------------------------------------ unapproved states

def test_a_submission_still_in_review_is_refused(registry):
    entry = ledger.Entry(submission_id="MALAVI-SUB-2026-000123", track="A",
                         received_at="2026-08-01T00:00:00+00:00")
    entry.state = "in_review"
    result = release_gate.check(store_with(entry.submission_id),
                                {entry.submission_id: entry})
    assert not result.ok
    assert "not 'approved'" in result.violations[0].reason


def test_an_embargoed_submission_is_refused(registry):
    """Approved is not the same as publishable. Embargo is the submitter's own claim."""
    entry = approved_entry(registry)
    entry.embargoed = True
    result = release_gate.check(store_with(entry.submission_id),
                                {entry.submission_id: entry})
    assert not result.ok
    assert "embargoed" in result.violations[0].reason


def test_a_standing_hold_beats_an_approval(registry):
    """Dissent outranks approval, and the release build is where that has to bite."""
    entry = approved_entry(registry)
    ledger.record_verdict(entry, "alice@example.edu", "hold",
                          reason_code="host_needs_review",
                          reason_text="Locality looks wrong.", registry_path=registry)
    result = release_gate.check(store_with(entry.submission_id),
                                {entry.submission_id: entry})
    assert not result.ok
    assert "objection" in result.violations[0].reason


# ------------------------------------------------------------------------ the pass case

def test_an_approved_submission_is_published(registry):
    entry = approved_entry(registry)
    result = release_gate.check(store_with("seed", entry.submission_id),
                                {entry.submission_id: entry})
    assert result.ok
    assert result.publishing == [entry.submission_id]
    assert result.exempt_rows == 1


def test_an_already_released_submission_is_not_published_twice(registry):
    """Its rows stay in the store and in every later release; there is nothing to mark."""
    entry = approved_entry(registry)
    ledger.transition(entry, "released", "maintainer", config=CLOCKS)
    result = release_gate.check(store_with(entry.submission_id),
                                {entry.submission_id: entry})
    assert result.ok
    assert result.publishing == []


# ------------------------------------------------------------------ the rehearsal step

def test_the_publish_hold_is_caught_before_anything_is_built(registry):
    """A release the ledger would refuse to record must never reach the disk.

    check() deliberately does not know about the 24-hour hold -- that rule belongs to
    ledger.transition. The rehearsal is what makes the two agree without copying the rule.
    """
    entry = approved_entry(registry, hours_ago=1)
    entries = {entry.submission_id: entry}
    assert release_gate.check(store_with(entry.submission_id), entries).ok

    refusals = release_gate.plan_release_transitions(entries, [entry.submission_id],
                                                     actor="maintainer", config=CLOCKS)
    assert refusals
    assert "publish hold has not elapsed" in refusals[0]


def test_the_rehearsal_changes_nothing(registry):
    """It runs on copies. A rehearsal with side effects would release on a dry run."""
    entry = approved_entry(registry)
    entries = {entry.submission_id: entry}
    before = copy.deepcopy(entries[entry.submission_id])

    assert release_gate.plan_release_transitions(entries, [entry.submission_id],
                                                 actor="maintainer", config=CLOCKS) == []
    after = entries[entry.submission_id]
    assert after.state == before.state == "approved"
    assert after.name_state == before.name_state
    assert len(after.history) == len(before.history)


# ------------------------------------------------------------------- the CLI refusal
#
# The module above is only half the fix. The gap this work closed was that build_release
# never CALLED any of it, so these test the wiring: that a store with unapprovable rows
# makes the program exit non-zero and write nothing at all.

@pytest.fixture(scope="module")
def build_release_cli():
    import importlib.util
    from malavi_curation.config import repo_root
    path = repo_root() / "curation" / "build_release.py"
    spec = importlib.util.spec_from_file_location("_build_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_build_refuses_and_writes_nothing(build_release_cli, tmp_path,
                                              monkeypatch, capsys):
    """The whole point: an unapproved row stops the release, and leaves no ZIP behind."""
    monkeypatch.setattr(build_release_cli, "read_store",
                        lambda _dir: store_with("MALAVI-SUB-2026-000999"))
    monkeypatch.setattr(build_release_cli, "store_dir", lambda _root: tmp_path / "store")

    destination = tmp_path / "releases"
    code = build_release_cli.main(["--release", "2026-08-10",
                                   "--destination", str(destination)])

    assert code == 2
    assert "may not be built" in capsys.readouterr().err
    assert not destination.exists()


def test_the_override_builds_but_marks_nothing_released(build_release_cli, tmp_path,
                                                        monkeypatch, capsys):
    """The escape hatch ships the data and says so; it never fakes a curator decision."""
    monkeypatch.setattr(build_release_cli, "read_store",
                        lambda _dir: store_with("MALAVI-SUB-2026-000999"))
    monkeypatch.setattr(build_release_cli, "store_dir", lambda _root: tmp_path / "store")
    monkeypatch.setattr(build_release_cli, "build_release",
                        lambda store, release, destination: {
                            "release": release, "rows": {"host_records": 1},
                            "archive": str(destination / "x.zip"),
                            "alignment_records": 0})

    destination = tmp_path / "releases"
    destination.mkdir()
    code = build_release_cli.main(["--release", "2026-08-10",
                                   "--destination", str(destination),
                                   "--i-am-overriding-the-approval-gate"])

    assert code == 0
    report = json.loads((destination / "release_report_2026-08-10.json").read_text())
    assert report["approval"]["gate_overridden"] is True
    assert "marked_released" not in report["approval"]
    assert "OVERRIDDEN" in capsys.readouterr().err


def test_the_success_path_marks_the_submission_released(build_release_cli, tmp_path,
                                                        registry, monkeypatch, capsys):
    """The path that was never tested, and where the reason-code bug lived.

    Both CLI tests exercised refusal and override. Nothing exercised a build that
    actually publishes a submission -- the only path that reaches the ledger write --
    so a reason outside DISPOSITION_REASON_CODES sat there passing rehearsal and
    failing the real write, after the ZIP was on disk.
    """
    entry = approved_entry(registry)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger.save(inbox, {entry.submission_id: entry})

    monkeypatch.setattr(build_release_cli, "read_store",
                        lambda _dir: store_with("seed", entry.submission_id))
    monkeypatch.setattr(build_release_cli, "store_dir", lambda _root: tmp_path / "store")
    monkeypatch.setattr(build_release_cli, "submissions_inbox", lambda _root: inbox)
    monkeypatch.setattr(build_release_cli, "load_config", lambda: {"review": CLOCKS})
    monkeypatch.setattr(build_release_cli, "build_release",
                        lambda store, release, destination: {
                            "release": release, "rows": {"host_records": 2},
                            "archive": str(destination / "x.zip"),
                            "alignment_records": 0})

    destination = tmp_path / "releases"
    destination.mkdir()
    code = build_release_cli.main(["--release", "2026-08-10",
                                   "--destination", str(destination)])

    assert code == 0, capsys.readouterr().err
    report = json.loads((destination / "release_report_2026-08-10.json").read_text())
    assert report["approval"]["marked_released"] == [entry.submission_id]

    after = ledger.load(inbox)[entry.submission_id]
    assert after.state == "released"
    assert after.name_state == "confirmed"


def test_the_rehearsal_uses_the_reason_the_real_write_will_pass(registry):
    """A rehearsal that does not use the real arguments is not a rehearsal.

    transition() validates `reason` against a closed vocabulary, so rehearsing with the
    default "" passed while the real call, passing prose, refused.
    """
    entry = approved_entry(registry)
    entries = {entry.submission_id: entry}
    assert release_gate.plan_release_transitions(
        entries, [entry.submission_id], actor="maintainer",
        reason="published in release 2026-08-10", config=CLOCKS)
    assert release_gate.plan_release_transitions(
        entries, [entry.submission_id], actor="maintainer",
        reason=build_release_reason(), config=CLOCKS) == []


def build_release_reason():
    """The constant the CLI actually uses, read from it rather than restated here."""
    import importlib.util
    from malavi_curation.config import repo_root
    path = repo_root() / "curation" / "build_release.py"
    spec = importlib.util.spec_from_file_location("_build_release_const", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RELEASED_REASON


# ------------------------------------------------------- saying what to do about it

def withdrawn_entry(registry):
    """An approved, ingested submission the submitter then took back."""
    entry = approved_entry(registry)
    ledger.transition(entry, "withdrawn", "vaellis@udel.edu",
                      at="2026-08-15T00:00:00+00:00", reason="withdrawn_by_submitter")
    return entry


def test_a_refusal_for_a_withdrawn_submission_names_the_retract_command(registry):
    """The refusal said what was wrong; only RUNBOOK row 12b said what to do."""
    entry = withdrawn_entry(registry)
    entries = {entry.submission_id: entry}
    gate = release_gate.check(store_with(entry.submission_id), entries)
    assert not gate.ok

    lines = release_gate.describe(gate, entries=entries, release="2026-09-02")
    command = [line for line in lines if "--retract" in line]
    assert len(command) == 1
    assert command[0].strip() == (
        ".venv/bin/python curation/ingest_submissions.py --release 2026-09-02 "
        f"--retract {entry.submission_id} --apply")


def test_an_embargo_refusal_offers_no_retraction(registry):
    """An embargoed submission's rows are waiting for a paper, not for deletion."""
    entry = approved_entry(registry)
    entry.embargoed = True
    entries = {entry.submission_id: entry}
    gate = release_gate.check(store_with(entry.submission_id), entries)
    assert not gate.ok
    assert not any("--retract" in line
                   for line in release_gate.describe(gate, entries=entries))


def test_the_build_prints_the_retract_command_when_it_refuses(build_release_cli, tmp_path,
                                                              registry, monkeypatch,
                                                              capsys):
    entry = withdrawn_entry(registry)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    ledger.save(inbox, {entry.submission_id: entry})

    monkeypatch.setattr(build_release_cli, "read_store",
                        lambda _dir: store_with("seed", entry.submission_id))
    monkeypatch.setattr(build_release_cli, "store_dir", lambda _root: tmp_path / "store")
    monkeypatch.setattr(build_release_cli, "submissions_inbox", lambda _root: inbox)

    destination = tmp_path / "releases"
    code = build_release_cli.main(["--release", "2026-09-02",
                                   "--destination", str(destination)])
    err = capsys.readouterr().err

    assert code == 2
    assert f"--release 2026-09-02 --retract {entry.submission_id} --apply" in err
    assert not destination.exists()
