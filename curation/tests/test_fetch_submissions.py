"""What the fetch lays down for a response, files or not.

A form response with no attachments used to be dropped before its directory and
``metadata.json`` were written -- one line of stdout, then nothing: no minted id, no queue
entry, no report, no email. The same shortcut meant an edited response never had its new
answers stored, because the answers were only written beside a fresh download. These
tests pin the directory-first behavior, and check that the screen copes with a directory
that holds answers and no workbook by saying so, not by crashing.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

from malavi_curation.config import repo_root

SHEET = {"submissions": {"responses_sheet": "sheet-id", "inbox_dir": "inbox"}}


def a_response(**answers):
    """One responses-sheet row, in the form's own column names, with no uploads."""
    row = {"Timestamp": "08/01/2026 12:00:00",
           "What is your first and last name?": "A Person",
           "Email Address": "a.person@example.edu",
           "Please provide any relevant notes or communication here (if applicable).": ""}
    row.update(answers)
    return row


@pytest.fixture(scope="module")
def cli():
    path = repo_root() / "curation" / "fetch_submissions.py"
    spec = importlib.util.spec_from_file_location("_fetch_submissions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sheet(cli, tmp_path, monkeypatch):
    """The fetch wired to a fake sheet and a tmp inbox. Set ``rows[:]`` to change it."""
    rows = []
    monkeypatch.setattr(cli, "load_config", lambda: SHEET)
    monkeypatch.setattr(cli, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli.google_auth, "describe", lambda: "identity: test")
    monkeypatch.setattr(cli.google_auth, "access_token", lambda: "token")
    monkeypatch.setattr(cli, "fetch_responses", lambda sheet_id, token: list(rows))

    def fake_download(file_id, dest_dir, token=None):
        path = dest_dir / f"{file_id}.xlsx"
        path.write_bytes(b"not really a workbook")
        return path
    monkeypatch.setattr(cli, "download_drive_file", fake_download)
    return {"rows": rows, "inbox": tmp_path / "inbox"}


def only_directory(inbox):
    directories = [p for p in inbox.iterdir() if p.is_dir()]
    assert len(directories) == 1, directories
    return directories[0]


def test_a_response_with_no_attachments_still_enters_the_pipeline(cli, sheet, capsys):
    sheet["rows"][:] = [a_response()]
    assert cli.main([]) == 0
    out = capsys.readouterr().out

    directory = only_directory(sheet["inbox"])
    stored = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert stored["What is your first and last name?"] == "A Person"
    assert stored["_fetched_at"], "the received date the report shows"
    assert [p.name for p in directory.iterdir()] == ["metadata.json"]
    assert "no uploaded files" in out and "nothing to check" in out


def test_an_edited_response_updates_the_stored_answers_and_keeps_the_received_date(
        cli, sheet, capsys):
    notes = "Please provide any relevant notes or communication here (if applicable)."
    sheet["rows"][:] = [a_response()]
    cli.main([])
    directory = only_directory(sheet["inbox"])
    first = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    capsys.readouterr()

    sheet["rows"][:] = [a_response(**{notes: "The paper is now out."})]
    assert cli.main([]) == 0
    out = capsys.readouterr().out

    second = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert second[notes] == "The paper is now out."
    assert second["_fetched_at"] == first["_fetched_at"], "an edit is not a new arrival"
    assert "answers updated" in out


def test_a_submitter_who_edits_their_name_keeps_their_directory(cli, sheet, capsys):
    """The directory name carries a slug of the submitter's name, which they can edit.
    Until 2026-09-02 an edited name made a second directory, and so a second submission."""
    name = "What is your first and last name?"
    sheet["rows"][:] = [a_response()]
    cli.main([])
    directory = only_directory(sheet["inbox"])
    capsys.readouterr()

    sheet["rows"][:] = [a_response(**{name: "A. Person-Renamed"})]
    assert cli.main([]) == 0
    assert only_directory(sheet["inbox"]) == directory
    stored = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert stored[name] == "A. Person-Renamed"


def test_unchanged_answers_leave_the_file_alone(cli, sheet, capsys):
    sheet["rows"][:] = [a_response()]
    cli.main([])
    directory = only_directory(sheet["inbox"])
    before = (directory / "metadata.json").read_bytes()
    capsys.readouterr()

    assert cli.main([]) == 0
    assert (directory / "metadata.json").read_bytes() == before
    out = capsys.readouterr().out
    assert "answers updated" not in out and "answers created" not in out


def test_a_response_with_a_file_gets_the_answers_and_the_file(cli, sheet):
    sheet["rows"][:] = [a_response(**{
        "Upload your filled template file":
            "https://drive.google.com/open?id=1UuTIxYku5cn-4Orsv1AOnmQxCs2tHx_8"})]
    assert cli.main([]) == 0
    directory = only_directory(sheet["inbox"])
    assert (directory / "metadata.json").is_file()
    assert (directory / "1UuTIxYku5cn-4Orsv1AOnmQxCs2tHx_8.xlsx").is_file()
    fetched = json.loads((sheet["inbox"] / "fetched.json").read_text(encoding="utf-8"))
    assert fetched["1UuTIxYku5cn-4Orsv1AOnmQxCs2tHx_8"]["submission"] == directory.name


def test_a_dry_run_writes_no_directory_for_anybody(cli, sheet, capsys):
    sheet["rows"][:] = [a_response()]
    assert cli.main(["--dry-run"]) == 0
    assert not sheet["inbox"].exists() or not any(
        p.is_dir() for p in sheet["inbox"].iterdir())
    assert "would record the answers" in capsys.readouterr().out


# ------------------------------------------------------------- what the screen makes of it

def test_the_screen_reports_a_directory_with_answers_and_no_workbook(tmp_path,
                                                                     monkeypatch, capsys):
    """Downstream of the fetch: a directory holding metadata.json and nothing else.

    The screen has to say loudly that nothing was checked -- exit 3 and a report a curator
    can read -- rather than crash or, worse, exit clean. It writes the paper-only report,
    which is the right shape; its wording assumes a paper was attached, and that is a
    report_html matter, not a fetch one.
    """
    path = repo_root() / "curation" / "check_template.py"
    spec = importlib.util.spec_from_file_location("_check_template", path)
    check_template = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_template)

    from malavi_curation.config import load_config
    alignment = check_template.default_alignment_path(
        repo_root(), load_config()["malaviR"]["release"])
    if alignment is None or not alignment.is_file():
        pytest.skip("no reference alignment in this checkout")

    # The report writer refuses to write outside <root>/curation/intake, so the fake
    # submission lives in a tmp tree of that shape and the screen is pointed at it.
    directory = tmp_path / "curation" / "intake" / "submissions" / "20260801T120000_A_Person"
    directory.mkdir(parents=True)
    (directory / "metadata.json").write_text(json.dumps(
        dict(a_response(), _fetched_at="2026-08-01T12:00:00Z")), encoding="utf-8")
    monkeypatch.setattr(check_template, "repo_root", lambda: tmp_path)

    code = check_template.main(["--no-r", "--alignment", str(alignment), str(directory)])
    out = capsys.readouterr().out

    assert code == 3, "not clean, not a failed check: a curator must act"
    assert "No data template" in out
    assert (directory / "report.html").is_file()
    assert "Nothing here has been checked" in (directory / "report.html").read_text(
        encoding="utf-8")
