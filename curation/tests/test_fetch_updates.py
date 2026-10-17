"""Submitters' updates filed as new revisions (curation/fetch_updates.py).

The form's responses and the Drive download are stand-ins; the review ledger and the
submission directory are real, under tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from malavi_curation import ledger
from malavi_curation.config import repo_root

ID = "MALAVI-SUB-0000-000001"
DIRECTORY = "20260101T000000_Jane_Smith"
CONFIG = {"submissions": {"update_responses_sheet": "SHEET", "inbox_dir": "inbox"}}


@pytest.fixture(scope="module")
def cli():
    path = repo_root() / "curation" / "fetch_updates.py"
    spec = importlib.util.spec_from_file_location("_fetch_updates", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_fetch_updates"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def world(cli, tmp_path, monkeypatch):
    """One held submission with a screened workbook, and an empty update form."""
    inbox = tmp_path / "inbox"
    directory = inbox / DIRECTORY
    directory.mkdir(parents=True)
    (directory / "ImportMalavi_old.xlsx").write_bytes(b"old workbook")
    (directory / "metadata.json").write_text("{}")
    for name in ("report.html", "report.pdf", "screen.json", "checks.json"):
        (directory / name).write_text("old " + name)
    (inbox / "submission_ids.json").write_text(json.dumps(
        {"version": 1, "next": 2, "ids": {DIRECTORY: {"id": ID}}}), encoding="utf-8")
    with ledger.open_ledger(inbox, write=True) as entries:
        entry = ledger.ensure_entry(entries, ID, "A", "2026-01-01T00:00:00+00:00")
        entry.state = "held"

    rows = []
    monkeypatch.setattr(cli, "load_config", lambda: CONFIG)
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli.google_auth, "describe", lambda: "identity: test")
    monkeypatch.setattr(cli.google_auth, "access_token", lambda: "token")
    monkeypatch.setattr(cli.fetch_submissions, "fetch_responses", lambda sheet, token: list(rows))

    def fake_download(file_id, dest_dir, token=None):
        path = dest_dir / f"{file_id}.xlsx"
        path.write_bytes(b"new workbook " + file_id.encode())
        return path
    monkeypatch.setattr(cli.fetch_submissions, "download_drive_file", fake_download)
    return {"inbox": inbox, "directory": directory, "rows": rows}


def response(submission_id=ID, note="I re-read the trace at 240; it is a T.",
             file_id="1ExampleFileId00000000000000000z", timestamp="10/2/2026 15:04:00"):
    return {"Timestamp": timestamp, "Email Address": "jane@example.org",
            "Submission id": submission_id, "What changed": note,
            "Upload the corrected template file":
                f"https://drive.google.com/open?id={file_id}" if file_id else ""}


def test_an_update_with_a_file_becomes_a_new_revision(cli, world, capsys):
    world["rows"][:] = [response()]
    assert cli.main([]) == 0
    directory = world["directory"]
    # The new workbook is in the directory; the old one and the screen's outputs are set
    # aside, kept, and the directory no longer looks screened.
    assert (directory / "1ExampleFileId00000000000000000z.xlsx").read_bytes().startswith(b"new")
    superseded = list((directory / "_superseded").iterdir())
    assert len(superseded) == 1
    assert sorted(p.name for p in superseded[0].iterdir()) == [
        "ImportMalavi_old.xlsx", "checks.json", "report.html", "report.pdf", "screen.json"]
    assert not (directory / "report.html").exists()
    # The submitter's words are beside the file.
    notes = list(directory.glob("update_*.json"))
    assert len(notes) == 1 and "240" in notes[0].read_text(encoding="utf-8")
    # The ledger has revision 2, by the submitter.
    with ledger.open_ledger(world["inbox"], write=False) as entries:
        entry = entries[ID]
    assert entry.revision == 2
    assert entry.revisions[-1].authority == "submitter" and "240" in entry.revisions[-1].reason
    assert "revision 2" in capsys.readouterr().out


def test_a_filed_response_is_not_filed_again(cli, world):
    world["rows"][:] = [response()]
    assert cli.main([]) == 0
    assert cli.main([]) == 0
    with ledger.open_ledger(world["inbox"], write=False) as entries:
        assert entries[ID].revision == 2
    assert len(list((world["directory"] / "_superseded").iterdir())) == 1


def test_words_without_a_file_are_kept_and_handed_to_a_person(cli, world, capsys):
    world["rows"][:] = [response(file_id="")]
    assert cli.main([]) == 2
    directory = world["directory"]
    assert (directory / "report.html").exists()                  # nothing set aside
    assert list(directory.glob("update_*.json"))
    with ledger.open_ledger(world["inbox"], write=False) as entries:
        assert entries[ID].revision == 1                          # no revision from words
    assert "words only" in capsys.readouterr().out


def test_a_dry_run_changes_nothing(cli, world):
    world["rows"][:] = [response()]
    assert cli.main(["--dry-run"]) == 0
    assert (world["directory"] / "ImportMalavi_old.xlsx").exists()
    assert not (world["directory"] / "_superseded").exists()
    assert not (world["inbox"] / "fetched_updates.json").exists()
    with ledger.open_ledger(world["inbox"], write=False) as entries:
        assert entries[ID].revision == 1


@pytest.mark.parametrize("bad_id", ["MALAVI-SUB-0000-000099", "not an id", ""])
def test_an_id_that_cannot_be_placed_is_reported_not_guessed(cli, world, bad_id, capsys):
    world["rows"][:] = [response(submission_id=bad_id)]
    assert cli.main([]) == 2
    assert "[skipped]" in capsys.readouterr().out
    assert not (world["directory"] / "_superseded").exists()


def test_a_released_submission_is_not_revised(cli, world, capsys):
    with ledger.open_ledger(world["inbox"], write=True) as entries:
        entries[ID].state = "released"
    world["rows"][:] = [response()]
    assert cli.main([]) == 2
    assert "its state is released" in capsys.readouterr().out
    assert (world["directory"] / "ImportMalavi_old.xlsx").exists()


def test_an_update_to_a_dormant_submission_is_filed_and_a_lead_is_asked_to_reopen(cli, world,
                                                                                  capsys):
    with ledger.open_ledger(world["inbox"], write=True) as entries:
        entries[ID].state = "dormant"
    world["rows"][:] = [response()]
    assert cli.main([]) == 2
    out = capsys.readouterr().out
    assert "revision 2" in out and "--reopen" in out


def test_a_failed_download_puts_everything_back(cli, world, monkeypatch, capsys):
    def refuse(file_id, dest_dir, token=None):
        raise OSError("HTTP 503")
    monkeypatch.setattr(cli.fetch_submissions, "download_drive_file", refuse)
    world["rows"][:] = [response()]
    assert cli.main([]) == 1
    directory = world["directory"]
    assert (directory / "ImportMalavi_old.xlsx").exists() and (directory / "report.html").exists()
    with ledger.open_ledger(world["inbox"], write=False) as entries:
        assert entries[ID].revision == 1
    # ...and it is tried again next time, since it was not recorded as filed.
    assert ID not in json.dumps(json.loads((world["inbox"] / "fetched_updates.json").read_text()))


def test_no_update_form_configured_is_quiet_and_clean(cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: {"submissions": {}})
    assert cli.main([]) == 0
    assert "not set up" in capsys.readouterr().out
