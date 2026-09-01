"""Tests for the no-op-suppressing feed writer.

Every site feed carries a ``generated`` timestamp, so a plain write produces a different
file on every run even when no data moved. The daily workflow commits whatever changed,
which was enough to put a commit in the repository every day of the year — three files,
three insertions, three deletions, all of them the timestamp line.

``write_feed`` makes the timestamp mean what a reader assumes it means: when the contents
last *changed*. The tests below fix that meaning, and fix the one thing it must never do,
which is decline to write when the data really did move.
"""
from __future__ import annotations

import json

import pytest

from malavi_curation.feeds import write_feed


@pytest.fixture
def feed(tmp_path):
    return tmp_path / "queue.json"


def _payload(generated="2026-08-06T00:00:00Z", **rest):
    payload = {"release": "2026-03-23", "generated": generated, "n": 1,
               "items": [{"name": "NECMON01", "claimed": "2026-07-27"}]}
    payload.update(rest)
    return payload


class TestWriteFeed:
    def test_writes_when_the_file_does_not_exist(self, feed):
        assert write_feed(feed, _payload()) is True
        assert json.loads(feed.read_text())["n"] == 1

    def test_does_not_rewrite_when_only_the_timestamp_moved(self, feed):
        write_feed(feed, _payload(generated="2026-08-06T00:00:00Z"))
        before = feed.read_text()

        assert write_feed(feed, _payload(generated="2026-08-07T09:30:00Z")) is False
        # Byte-identical: the old timestamp is kept, because the contents are what
        # the timestamp is dating and they did not change.
        assert feed.read_text() == before

    def test_writes_when_the_data_moved(self, feed):
        write_feed(feed, _payload())
        assert write_feed(feed, _payload(n=2)) is True
        assert json.loads(feed.read_text())["n"] == 2

    def test_writes_when_a_nested_value_moved(self, feed):
        # The comparison has to be deep. A claim date changing inside a list is
        # exactly the kind of change that must reach the site.
        write_feed(feed, _payload())
        changed = _payload()
        changed["items"][0]["claimed"] = "2026-08-01"
        assert write_feed(feed, changed) is True

    def test_writes_when_a_key_is_removed(self, feed):
        write_feed(feed, _payload(extra="gone soon"))
        assert write_feed(feed, _payload()) is True

    def test_an_unreadable_file_is_overwritten(self, feed):
        # Corrupt output is not evidence that the current data matches it.
        feed.write_text("{not json at all")
        assert write_feed(feed, _payload()) is True
        assert json.loads(feed.read_text())["n"] == 1

    def test_a_non_dict_file_is_overwritten(self, feed):
        feed.write_text("[1, 2, 3]")
        assert write_feed(feed, _payload()) is True

    def test_creates_missing_parent_directories(self, tmp_path):
        nested = tmp_path / "assets" / "data" / "queue.json"
        assert write_feed(nested, _payload()) is True
        assert nested.is_file()

    def test_honors_the_newline_and_ascii_options(self, tmp_path):
        # build_name_reservations writes with a trailing newline and ASCII escaping;
        # build_site_feeds does not. Both call sites must get what they asked for.
        ascii_feed = tmp_path / "a.json"
        write_feed(ascii_feed, _payload(note="Skåne"), ensure_ascii=True, newline="\n")
        raw = ascii_feed.read_text()
        assert raw.endswith("\n")
        assert "Sk\\u00e5ne" in raw

        utf8_feed = tmp_path / "b.json"
        write_feed(utf8_feed, _payload(note="Skåne"))
        assert "Skåne" in utf8_feed.read_text()

    def test_a_custom_timestamp_key_is_respected(self, feed):
        write_feed(feed, {"built": "one", "n": 1}, timestamp_key="built")
        assert write_feed(feed, {"built": "two", "n": 1}, timestamp_key="built") is False
        assert write_feed(feed, {"built": "two", "n": 2}, timestamp_key="built") is True

    def test_comparison_ignores_only_the_timestamp(self, feed):
        """A run that changed nothing but claims to have changed must still be quiet,
        and a run that changed something must never be silenced by this."""
        write_feed(feed, _payload())
        # Same data, different timestamp, reordered keys: still nothing to write.
        reordered = {"items": _payload()["items"], "n": 1, "release": "2026-03-23",
                     "generated": "2026-12-31T23:59:59Z"}
        assert write_feed(feed, reordered) is False


class TestPublicFeedSafety:
    """The reservation feed's contract, which write_feed must not be able to widen."""

    def test_the_reserved_names_feed_carries_a_name_and_a_date_only(self):
        # PUBLIC_FIELDS is the whole contract, and records are BUILT from it rather
        # than filtered down, so a later edit cannot leak a field by forgetting to
        # strip it. This test states the contract so a change to it is deliberate.
        import importlib.util
        from pathlib import Path

        from malavi_curation.config import repo_root

        path = repo_root() / "curation" / "build_name_reservations.py"
        spec = importlib.util.spec_from_file_location("_brn", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.PUBLIC_FIELDS == ("name", "claimed"), (
            "the public reservation feed's fields changed — this is a privacy "
            "decision, not a refactor: a name and a date cannot be scooped, a "
            "sequence or a host can")


# --------------------------------------------------------------------------------------
# What the PUBLIC queue is allowed to say about a curator's decision
# --------------------------------------------------------------------------------------

import importlib.util as _ilu                                            # noqa: E402

from malavi_curation.config import repo_root as _repo_root               # noqa: E402


@pytest.fixture(scope="module")
def feeds_cli():
    path = _repo_root() / "curation" / "build_site_feeds.py"
    spec = _ilu.spec_from_file_location("_build_site_feeds", path)
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("state", ["declined", "dormant", "withdrawn"])
def test_a_closed_submission_leaves_the_queue_entirely(feeds_cli, state):
    """It drops off; it is never labeled.

    A public page that said "declined" would be a permanent record of whose work was
    turned down. The entries carry an opaque id precisely because we already decided that
    who submitted what is nobody else's business.
    """
    assert feeds_cli.public_status(state, screened=True, has_errors=False) is None


def test_a_released_submission_also_leaves(feeds_cli):
    """It is in the database now, and that is where it should be read."""
    assert feeds_cli.public_status("released", screened=True, has_errors=False) is None


@pytest.mark.parametrize("state", ["held", "awaiting_submitter", "in_review",
                                   "ready_for_review", "screening_failed"])
def test_a_questioned_submission_is_indistinguishable_from_a_healthy_one(feeds_cli, state):
    """This is the privacy property, not a cosmetic choice.

    Held, flagged, waiting on the submitter and screened-with-problems must all read the
    same as a submission that arrived this morning. Anything else lets a reader work out
    that a particular submission is in trouble.
    """
    label, _pill = feeds_cli.public_status(state, screened=True, has_errors=True)
    assert label == "Under review"


def test_an_approval_is_shown(feeds_cli):
    """Good news is allowed: it shows the queue moving rather than merely growing."""
    label, _pill = feeds_cli.public_status("approved", screened=True, has_errors=False)
    assert label == "Accepted"


def test_blocking_screen_errors_never_reach_the_public_page(feeds_cli):
    """The old feed published "Needs attention" for a submission with blocking errors.

    That is between the submitter and the curators. With no ledger entry yet, a submission
    with errors must look like any other screened one.
    """
    label, _pill = feeds_cli.public_status(None, screened=True, has_errors=True)
    assert label == "Under review"
    assert "attention" not in label.lower()


def test_an_unscreened_submission_with_no_ledger_entry_is_simply_queued(feeds_cli):
    label, _pill = feeds_cli.public_status(None, screened=False, has_errors=False)
    assert label == "In the queue"


# --------------------------------------------------------------------------------------
# An approval is not public until the publish hold has run out
# --------------------------------------------------------------------------------------
#
# The queue must not say "Accepted" inside the hold window. The hold exists so a second
# curator can still object; a queue that published the approval immediately would have to
# walk it back to "Under review" in front of the submitter when a late hold landed. That
# retraction would be legible on a public page even though the hold itself is deliberately
# not -- which defeats the whole lossy mapping above.

from datetime import datetime, timedelta, timezone                       # noqa: E402

from malavi_curation import ledger as _ledger                            # noqa: E402


def _entry(state):
    """A minimal ledger entry in the given state."""
    return _ledger.Entry(submission_id="MALAVI-SUB-2026-000001", track="A",
                         received_at="2026-01-01T00:00:00+00:00", state=state)


def _approved_entry(approved_at):
    """A minimal approved ledger entry, approved at the given moment."""
    entry = _entry("approved")
    entry.approved_at = approved_at
    return entry


def _stamp(hours_ago):
    moment = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return moment.replace(microsecond=0).isoformat()


def test_an_approval_inside_the_hold_still_reads_as_under_review(feeds_cli):
    entry = _approved_entry(_stamp(1))
    assert feeds_cli.public_review_state(entry) == "in_review"
    label, _pill = feeds_cli.public_status(
        feeds_cli.public_review_state(entry), screened=True, has_errors=False)
    assert label == "Under review"


def test_an_approval_becomes_public_once_the_hold_has_elapsed(feeds_cli):
    entry = _approved_entry(_stamp(25))
    assert feeds_cli.public_review_state(entry) == "approved"
    label, _pill = feeds_cli.public_status(
        feeds_cli.public_review_state(entry), screened=True, has_errors=False)
    assert label == "Accepted"


def test_a_late_hold_keeps_an_elapsed_approval_off_the_public_page(feeds_cli):
    """A hold recorded late in the window still wins, as it does everywhere else."""
    entry = _approved_entry(_stamp(25))
    entry.verdicts.append(_ledger.Verdict(id="V1", curator="sari", verdict="hold",
                                          revision=1, at=_stamp(2)))
    assert _ledger.blocking_holds(entry)
    assert feeds_cli.public_review_state(entry) == "in_review"


def test_an_approval_with_no_timestamp_is_not_published(feeds_cli):
    """Missing data must fail closed. Treating it as elapsed would publish immediately."""
    assert feeds_cli.public_review_state(_approved_entry("")) == "in_review"


def test_an_unreadable_approval_timestamp_is_not_published(feeds_cli):
    assert feeds_cli.public_review_state(_approved_entry("8/20/2026")) == "in_review"


@pytest.mark.parametrize("state", ["received", "in_review", "held", "declined",
                                   "withdrawn", "released"])
def test_every_other_state_passes_through_untouched(feeds_cli, state):
    """The hold rule applies to approvals only; it must not reinterpret anything else."""
    assert feeds_cli.public_review_state(_entry(state)) == state
