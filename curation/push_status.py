#!/usr/bin/env python3
# @title Push the ledger's view of every submission to the curators' status sheet
# @purpose Read the review ledger and the curator registry, build one row per submission
#   (state, what it is waiting on, hold end, report link, submitter contact) and send them
#   to the Apps Script endpoint's `status` action, which writes the private status sheet.
# @why The status sheet is where assignment, reminders and the inbox triage live (Google
#   side), but only the ledger knows a submission's state and clocks. Without this the
#   sheet's state columns would be guesses; with it they are the ledger's, stamped with
#   when they were synced (results/PLAN_SUBMISSION_SYSTEM_RETHINK_2026-10-14.md, section 4).
# @input curation/intake/submissions/review_ledger.json (via malavi_curation.ledger)
# @input curation/intake/submissions/*/submission.json (submitter name and address)
# @input config/curators.yml (the registry, copied to the sheet's Curators tab)
# @input config/project.yml (google.report_endpoint, review.publish_hold_hours, submissions.exclude)
# @output the Submissions and Curators tabs of the status sheet (remote; nothing local)
# @program python3
# @critical-var STATUS_ROW_KEYS
# @critical-flag push_status.py "" --dry-run
"""Push the ledger's view to the status sheet.

    .venv/bin/python curation/push_status.py --dry-run     # print the rows; send nothing
    .venv/bin/python curation/push_status.py               # send them

Run by ``intake_cycle.py`` as its last step. Exit 0 when the sheet was written or when
there is nothing to write to (no endpoint configured, or the Apps Script project has no
STATUS_SHEET_ID yet): the sheet is optional and must never stop the cycle. Exit 1 only
when the endpoint refused for another reason (bad signature, network).

**What is sent, and what is not.** One row per submission in the ledger that is not in
``submissions.exclude``: id, arrival, submitter name and address, revision, state, the
"waiting on" code the board uses (``status_board._waiting_on``), when that wait began,
when the publish hold ends, embargo, how many names are reserved, the report link and
date. Never a verdict's reason text, never a correction's wording, never a lineage name:
``report_delivery.build_status_payload`` refuses any other key. The Curators tab gets the
registry (name, address, role, active) so the sheet can show names instead of addresses.

Finished submissions are sent too, with their terminal state, so the sheet shows them as
closed rather than leaving yesterday's "in_review" standing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation import curators, ledger, report_delivery, status_board  # noqa: E402
from malavi_curation.config import load_config, repo_root                    # noqa: E402
from malavi_curation.report_delivery import DeliveryError                      # noqa: E402
from malavi_curation.submission_id import directory_for                       # noqa: E402


def submissions_inbox(configuration: Dict[str, Any]) -> Path:
    return repo_root() / (configuration.get("submissions") or {}).get(
        "inbox_dir", "curation/intake/submissions")


def submitter_contact(inbox: Path, submission_id: str) -> Dict[str, str]:
    """Name and address from the screen's submission.json; blanks when unknown."""
    directory = directory_for(inbox, submission_id)
    if not directory:
        return {}
    path = inbox / directory / "submission.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    submitter = data.get("submitter") or {}
    out: Dict[str, str] = {}
    name = str(submitter.get("name") or "").strip()
    email = str(submitter.get("email") or "").strip()
    if name:
        out["submitter_name"] = name
    if "@" in email:
        out["submitter_email"] = email
    return out


def status_rows(entries: Dict[str, Any], registry: Dict[str, Any], inbox: Path,
                configuration: Dict[str, Any], excluded_ids=()) -> List[Dict[str, Any]]:
    """One sheet row per submission, from the ledger alone (plus the contact details)."""
    hold_hours = int(ledger._review_config(configuration)["publish_hold_hours"])
    skip = set(excluded_ids)
    rows: List[Dict[str, Any]] = []
    for submission_id, entry in sorted(entries.items()):
        if submission_id in skip:
            continue
        report = status_board._last_event(entry, "report_published")
        report_at = str(report.get("at")) if report else ""
        row: Dict[str, Any] = {
            "id": submission_id,
            "received": entry.received_at,
            "revision": int(entry.revision or 1),
            "state": entry.state,
            "embargoed": bool(entry.embargoed),
            "names": len(entry.reserved_names),
            "report_url": str(report.get("url") or "") if report else "",
            "report_at": report_at,
        }
        if entry.state in ledger.LIVE_STATES:
            waiting = status_board._waiting_on(entry, registry, hold_hours, report_at)
            row["waiting_on"] = str(waiting.get("code") or "")
            row["waiting_since"] = str(waiting.get("since") or "")
            row["hold_ends"] = str(waiting.get("ends") or "")
        else:
            row["waiting_on"] = ""
            row["waiting_since"] = ""
            row["hold_ends"] = ""
        row.update(submitter_contact(inbox, submission_id))
        rows.append(row)
    return rows


def curator_rows(registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per ADDRESS, aliases included, all with the person's name.

    The sheet looks names up by the address Google saw (the CURATORS property, a form
    response, an assignment), and for one curator that is an alias, not the registry's
    primary address (her institution runs no Google account). A row per alias makes every
    lookup land; the registry stays the record of who the person is.
    """
    rows: List[Dict[str, Any]] = []
    for _id, person in sorted(registry.items()):
        for address in [person.email, *getattr(person, "aliases", ())]:
            if not address:
                continue
            rows.append({"name": person.name, "email": address, "role": person.role,
                         "active": bool(person.active)})
    return rows


def _describe(rows: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for row in rows:
        lines.append(f"  {row['id']}  {row['state']:<18} waiting on: {row.get('waiting_on') or '-':<20}"
                     f" since {str(row.get('waiting_since') or '')[:10]:<10}"
                     f" submitter: {row.get('submitter_email') or '(no address)'}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print the rows that would be sent; send nothing")
    arguments = parser.parse_args(argv)

    print("== malavi_rebuild :: push_status ==")
    configuration = load_config()
    inbox = submissions_inbox(configuration)
    entries = ledger.load(inbox)
    registry = curators.load_registry()
    hidden = status_board.excluded_submission_ids(inbox, list(entries), configuration)
    rows = status_rows(entries, registry, inbox, configuration, excluded_ids=hidden)
    people = curator_rows(registry)
    print(f"{len(rows)} submission row(s), {len(people)} curator(s)"
          + (f", {len(hidden)} hidden by submissions.exclude" if hidden else ""))
    for line in _describe(rows):
        print(line)

    # Refuse early if a row carries something the sheet may not hold.
    report_delivery.build_status_payload(rows, people)

    if arguments.dry_run:
        print("\n[dry-run] nothing sent.")
        return 0
    if not report_delivery.endpoint_url():
        print("\nNo report endpoint configured (google.report_endpoint); the status sheet "
              "cannot be reached from here. Not an error: the sheet is optional.")
        return 0
    try:
        reply = report_delivery.deliver_status(rows, people)
    except DeliveryError as exc:
        if "STATUS_SHEET_ID" in str(exc):
            print(f"\nThe Apps Script project has no status sheet yet: {exc}\nNot an error: "
                  f"run createStatusSheet in the project (RUNBOOK section 3g).")
            return 0
        if "unusable filename" in str(exc):
            # An endpoint older than the `status` action routes it to the PDF branch, which
            # refuses it this way (report_delivery.describe_live documents the same tell).
            # Not an error here: the sheet is optional, and publish_report.py --check --live
            # is the program that says a redeploy is owed.
            print("\nThe deployed Apps Script predates the status action (RUNBOOK section 3g, "
                  "step 10 of the browser checklist). Nothing synced; not an error.")
            return 0
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    print(f"\nsynced {reply.get('rows', 0)} row(s) ({reply.get('created', 0)} new), "
          f"{reply.get('curators', 0)} curator(s) at {reply.get('synced_at', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
