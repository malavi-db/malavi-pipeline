"""The automatic feed publish: what it must never do.

Every test here is about a failure mode rather than a feature. The function's job is
small -- run two generators and one publisher -- and the whole reason it exists as a
module instead of three lines in each caller is that it has to fail in a particular way:
quietly, and without taking down the program that already wrote to the ledger.
"""
from __future__ import annotations

import subprocess

import pytest

from malavi_curation import public_feeds


class _Recorder:
    """Stands in for subprocess.run, remembering what was asked and answering to script."""

    def __init__(self, *, fail_on=(), needs_person_on=(), stdout=""):
        self.fail_on = tuple(fail_on)
        # Builders that write their feed and then exit 2: "done, but a person must act".
        self.needs_person_on = tuple(needs_person_on)
        self.stdout = stdout
        self.calls = []

    def __call__(self, command, *args, **kwargs):
        self.calls.append(command)
        joined = " ".join(command)
        failed = any(marker in joined for marker in self.fail_on)
        needs_person = any(marker in joined for marker in self.needs_person_on)
        if needs_person:
            return subprocess.CompletedProcess(
                args=command, returncode=public_feeds.NEEDS_A_PERSON,
                stdout="wrote reserved_names.json",
                stderr="1 name(s) are claimed by more than one submission. "
                       "The earliest claim is published; the other submitter(s) must be "
                       "offered another name before their submission is approved.")
        return subprocess.CompletedProcess(
            args=command, returncode=1 if failed else 0,
            stdout="" if failed else self.stdout,
            stderr="something went wrong" if failed else "")

    @property
    def ran(self):
        return " | ".join(" ".join(c) for c in self.calls)


@pytest.fixture
def recorder(monkeypatch):
    def install(**kwargs):
        rec = _Recorder(**kwargs)
        monkeypatch.setattr(public_feeds.subprocess, "run", rec)
        return rec
    return install


def test_a_dry_run_touches_nothing(recorder, capsys):
    """--dry-run means --dry-run. A caller previewing a change publishes nothing."""
    rec = recorder()
    assert public_feeds.refresh(dry_run=True) is True
    assert rec.calls == []
    assert "dry-run" in capsys.readouterr().out


def test_the_happy_path_builds_both_feeds_then_publishes(recorder):
    rec = recorder()
    assert public_feeds.refresh() is True
    assert "build_site_feeds.py" in rec.ran
    assert "build_name_reservations.py" in rec.ran
    assert "push_feeds.sh" in rec.ran


def test_publish_false_rebuilds_but_does_not_publish(recorder):
    """For a caller that wants the files current without an outward-facing action."""
    rec = recorder()
    assert public_feeds.refresh(publish=False) is True
    assert "build_site_feeds.py" in rec.ran
    assert "push_feeds.sh" not in rec.ran


def test_a_failed_builder_stops_the_publish(recorder, capsys):
    """Publishing feeds a generator just failed to write would push a half-updated set."""
    rec = recorder(fail_on=("build_name_reservations.py",))
    assert public_feeds.refresh() is False
    assert "push_feeds.sh" not in rec.ran
    assert "not publishing" in capsys.readouterr().out


def test_a_name_collision_is_reported_loudly_and_still_published(recorder, capsys):
    """REGRESSION: build_name_reservations.py writes its feed and exits 2 on a collision,
    by design -- the earliest claim is the right feed and the other submitter needs a
    person. refresh() read every non-zero exit as a failed rebuild and withheld all three
    feeds, queue.json included, so one stale collision silently froze the public queue."""
    rec = recorder(needs_person_on=("build_name_reservations.py",))
    assert public_feeds.refresh() is True
    assert "push_feeds.sh" in rec.ran, "the feeds were written; they must be published"
    out = capsys.readouterr().out
    assert "needs a person" in out
    assert "claimed by more than one submission" in out, (
        "the builder's own message is the one a curator has to read")
    assert "not publishing" not in out


def test_a_builder_that_could_not_write_still_stops_the_publish(recorder):
    """Exit 2 is the only exit that publishes. Any other non-zero exit still means the
    feed on disk may be stale, and a half-updated set must not go out."""
    rec = recorder(fail_on=("build_site_feeds.py",),
                   needs_person_on=("build_name_reservations.py",))
    assert public_feeds.refresh() is False
    assert "push_feeds.sh" not in rec.ran


def test_a_failed_publish_is_reported_and_swallowed(recorder, capsys):
    """The caller has already written to the ledger and cannot undo it.

    A dead network must leave the verdict recorded and print a note, never raise.
    """
    recorder(fail_on=("push_feeds.sh",))
    assert public_feeds.refresh() is False          # reported...
    out = capsys.readouterr().out
    assert "publishing the feeds failed" in out     # ...and explained...
    assert "push_feeds.sh" in out                   # ...with the way to finish it by hand


def test_it_never_raises_even_when_everything_fails(recorder):
    """The contract the four callers depend on."""
    recorder(fail_on=("build_site_feeds.py", "build_name_reservations.py",
                      "push_feeds.sh"))
    assert public_feeds.refresh() is False


def test_nothing_to_push_does_not_read_as_a_failure(recorder, capsys):
    """The normal result right after an approval, because of the publish hold."""
    recorder(stdout="the published feeds already match docs/; nothing to push.")
    assert public_feeds.refresh() is True
    assert "already current" in capsys.readouterr().out


def test_it_publishes_feeds_and_never_the_whole_site(recorder):
    """The rule that makes an automatic publish safe to leave switched on.

    push_site.sh syncs all of docs/. If this ever called it, a verdict recorded while a
    page was half-edited would put that page on the public web.
    """
    rec = recorder()
    public_feeds.refresh()
    assert "push_site.sh" not in rec.ran
