"""push_status.py: the ledger's view, as rows for the curators' status sheet.

What matters: every live and finished submission gets a row with the ledger's state and
the board's "waiting on" code; the submitter's contact comes from submission.json when the
id map knows the directory; and nothing the sheet may not hold (reason text, lineage
names) ever reaches a row -- ``build_status_payload`` refuses it, and the rows this program
builds must pass that gate.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

from malavi_curation import report_delivery
from malavi_curation.config import repo_root

from test_status_board import CONFIG, REGISTRY, SECRET_REASON, _entries

spec = importlib.util.spec_from_file_location(
    "push_status", repo_root() / "curation" / "push_status.py")
push_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push_status)


@pytest.fixture
def inbox(tmp_path):
    """An inbox whose id map knows submission 1's directory, with a submission.json."""
    inbox = tmp_path / "submissions"
    directory = inbox / "20260929T165923_Jane_Smith"
    directory.mkdir(parents=True)
    (directory / "submission.json").write_text(json.dumps(
        {"submitter": {"name": "Jane Smith", "email": "jane@example.edu"}}), encoding="utf-8")
    (inbox / "submission_ids.json").write_text(json.dumps(
        {"version": 1, "next": 2,
         "ids": {"20260929T165923_Jane_Smith": {"id": "MALAVI-SUB-0000-000001"}}}),
        encoding="utf-8")
    return inbox


def test_every_submission_gets_a_row_with_the_ledger_s_state_and_wait(inbox):
    rows = push_status.status_rows(_entries(), REGISTRY, inbox, CONFIG)
    by_id = {row["id"][-1]: row for row in rows}
    assert set(by_id) == {"1", "2", "3", "4"}
    assert by_id["1"]["state"] == "ready_for_review"
    assert by_id["1"]["waiting_on"] == "verdict"
    assert by_id["1"]["waiting_since"] == "2026-09-30T22:14:11+00:00"
    assert by_id["1"]["report_url"] == "https://drive.google.com/file/d/REPORT1/view"
    assert by_id["1"]["names"] == 2 and by_id["1"]["embargoed"] is True
    assert by_id["2"]["waiting_on"] == "flag"
    assert by_id["3"]["waiting_on"] == "hold"
    assert by_id["3"]["hold_ends"] == "2026-10-04T21:50:00Z"
    # A finished submission is sent with its terminal state and no wait, so the sheet
    # stops showing yesterday's "in_review".
    assert by_id["4"]["state"] == "released" and by_id["4"]["waiting_on"] == ""


def test_the_submitter_contact_comes_from_submission_json_when_the_id_is_mapped(inbox):
    rows = push_status.status_rows(_entries(), REGISTRY, inbox, CONFIG)
    by_id = {row["id"][-1]: row for row in rows}
    assert by_id["1"]["submitter_name"] == "Jane Smith"
    assert by_id["1"]["submitter_email"] == "jane@example.edu"
    # Unmapped ids simply carry no contact; the row still goes.
    assert "submitter_email" not in by_id["2"]


def test_no_reason_text_or_lineage_name_reaches_a_row(inbox):
    rows = push_status.status_rows(_entries(), REGISTRY, inbox, CONFIG)
    flat = json.dumps(rows)
    assert SECRET_REASON not in flat
    assert "Dr Private" not in flat
    assert "TESTUS01" not in flat
    # And the rows pass the payload gate, which is what the sheet actually receives.
    payload = report_delivery.build_status_payload(rows, push_status.curator_rows(REGISTRY))
    assert len(payload["rows"]) == 4
    assert payload["curators"] == [
        {"name": "Ada Lead", "email": "lead@example.org", "role": "lead", "active": True},
        {"name": "Ben Second", "email": "second@example.org", "role": "curator", "active": True}]


def test_excluded_submissions_are_left_off(inbox):
    rows = push_status.status_rows(_entries(), REGISTRY, inbox, CONFIG,
                                   excluded_ids=["MALAVI-SUB-0000-000004"])
    assert [row["id"][-1] for row in rows] == ["1", "2", "3"]
