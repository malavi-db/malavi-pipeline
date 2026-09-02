"""The public reservation feed and the review ledger.

The feed is generated from the intake directories, and a directory outlives every decision
made about the submission in it. These tests pin what the ledger is allowed to change
about the feed: a declined submission's names come off it; a dormant one's stay on.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

from malavi_curation import ledger, submission_id
from malavi_curation.config import repo_root


@pytest.fixture(scope="module")
def builder():
    path = repo_root() / "curation" / "build_name_reservations.py"
    spec = importlib.util.spec_from_file_location("_build_name_reservations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _submission(inbox, name, *lineages):
    sub_dir = inbox / name
    sub_dir.mkdir(parents=True)
    (sub_dir / "metadata.json").write_text(json.dumps({"Timestamp": "01/01/2026 00:00:00"}))
    (sub_dir / "screen.json").write_text(json.dumps([{
        "workbook": "ImportMalavi_Test.xlsx",
        "lineages": {n: {} for n in lineages},
        "name_suggestions": {}}]))
    return sub_dir


def _decide(inbox, directory, state, public_id, *, name_state=None):
    ids = submission_id.load_ledger(inbox)
    ids["ids"][directory] = {"id": public_id, "minted_at": "2026-01-01T00:00:00+00:00"}
    submission_id._save_ledger(inbox, ids)
    entries = ledger.load(inbox)
    entry = ledger.ensure_entry(entries, public_id, "A", "2026-01-01T00:00:00+00:00")
    entry.state = state
    if name_state is not None:
        entry.name_state = name_state
    ledger.save(inbox, entries)


@pytest.fixture
def repo(tmp_path, monkeypatch, builder):
    """A throwaway repository root with an inbox and a tiny lineage index."""
    data = tmp_path / "docs" / "assets" / "data"
    data.mkdir(parents=True)
    (data / "lineage_sequences.json").write_text(json.dumps(
        {"release": "test", "entries": [{"names": ["TUMIG01"]}]}))
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(builder, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(builder, "load_config",
                        lambda: {"submissions": {"inbox_dir": "inbox"}})
    return inbox


def _feed_names(tmp_path):
    feed = tmp_path / "docs" / "assets" / "data" / "reserved_names.json"
    return {r["name"] for r in json.loads(feed.read_text())["names"]}


def test_a_declined_submission_leaves_the_public_feed(builder, repo, tmp_path, capsys):
    """REGRESSION (A13): the feed never read the ledger, so a declined submission held
    its names publicly forever, and close_submission's "reserved names released" was not
    true anywhere the names were read."""
    _submission(repo, "20260101T000000_Declined", "TUMIG31")
    _submission(repo, "20260202T000000_Live", "TUMIG32")
    _decide(repo, "20260101T000000_Declined", "declined", "MALAVI-SUB-2026-000001",
            name_state="released")
    assert builder.main([]) == 0
    assert _feed_names(tmp_path) == {"TUMIG32"}
    assert "names returned" in capsys.readouterr().out


def test_a_dormant_submission_stays_on_the_public_feed(builder, repo, tmp_path):
    """A dormant submission keeps its claim indefinitely, and stays visible."""
    _submission(repo, "20260101T000000_Dormant", "TUMIG31")
    _decide(repo, "20260101T000000_Dormant", "dormant", "MALAVI-SUB-2026-000001")
    assert builder.main([]) == 0
    assert _feed_names(tmp_path) == {"TUMIG31"}


def test_a_declined_claim_no_longer_counts_as_a_collision(builder, repo, tmp_path):
    """The newcomer asking for a name a declined submission once wanted gets it, and the
    run reports success rather than a collision that needs a person."""
    _submission(repo, "20260101T000000_Declined", "TUMIG31")
    _submission(repo, "20260202T000000_Newcomer", "TUMIG31")
    _decide(repo, "20260101T000000_Declined", "declined", "MALAVI-SUB-2026-000001",
            name_state="released")
    assert builder.main([]) == 0
    assert _feed_names(tmp_path) == {"TUMIG31"}


def test_an_unreadable_ledger_is_reported_and_releases_nothing(builder, repo, tmp_path,
                                                                capsys):
    _submission(repo, "20260101T000000_Declined", "TUMIG31")
    _decide(repo, "20260101T000000_Declined", "declined", "MALAVI-SUB-2026-000001",
            name_state="released")
    ledger.ledger_path(repo).write_text("{not json")
    assert builder.main([]) == 0
    assert _feed_names(tmp_path) == {"TUMIG31"}
    assert "could not read the review ledger" in capsys.readouterr().err


def test_a_collision_still_writes_the_feed_and_exits_two(builder, repo, tmp_path):
    """The contract public_feeds.refresh() relies on: exit 2 means written, needs a
    person. Two live submissions claiming one name is exactly that."""
    _submission(repo, "20260101T000000_First", "TUMIG31")
    _submission(repo, "20260202T000000_Second", "TUMIG31")
    assert builder.main([]) == 2
    assert _feed_names(tmp_path) == {"TUMIG31"}
