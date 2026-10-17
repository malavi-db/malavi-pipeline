#!/usr/bin/env python3
# @title File submitters' updates as new revisions of their original submissions
# @purpose Read the "update a submission" form's responses, put each corrected workbook
#   into the ORIGINAL submission's directory (the old one set aside), and record a new
#   revision in the review ledger, so the submission keeps its id, its date and its names.
# @why A submitter asked to fix something answered with a one-line email or an attached
#   spreadsheet and did not want to go through the whole form again; the only routes
#   were a full resubmission (a new id, a new date, the names lost) or the maintainer
#   placing the file by hand (to-do J, 2026-10-02).
# @input config/project.yml (submissions.update_responses_sheet, inbox_dir)
# @input the update form's responses sheet, read through the Drive service account
# @output curation/intake/submissions/<dir>/ (the new workbook; the old one under _superseded/)
# @output curation/intake/submissions/review_ledger.json (a new revision)
# @output curation/intake/submissions/fetched_updates.json (which responses were filed)
# @program python3
# @critical-var FILE_COLUMN_HINTS
# @critical-var SUPERSEDED_DIR
# @critical-flag fetch_updates.py "" --dry-run
"""File submitters' updates as new revisions.

    .venv/bin/python curation/fetch_updates.py --dry-run
    .venv/bin/python curation/fetch_updates.py

What an update is
-----------------
A response on the "update a submission" form (``curation/apps_script/create_update_form.gs``):
the submission's id, what changed in the submitter's words, and optionally the corrected
template. It is the answer to a flag, sent without re-filling the submission form.

What this does with it
----------------------
For each response not filed yet:

1. finds the submission by its id in the review ledger and its directory through the id
   map. An id that is not in the ledger, or a submission that is released or withdrawn, is
   reported and left alone: there is nothing to revise;
2. **with a file:** moves the submission's current workbook(s) and its report, screen and
   checks into ``_superseded/<timestamp>/`` inside the directory (kept, never deleted),
   downloads the new file(s) into the directory, writes the submitter's note beside them as
   ``update_<timestamp>.json``, and records a new revision in the ledger
   (:func:`ledger.bump_revision`, authority ``submitter``). The directory now has no
   ``report.html``, so the intake cycle screens it again; the new revision makes its report
   pending again (``publish_report.py --all-pending``); and the approvals of the earlier
   revision are cleared, as a revision always clears them;
3. **without a file:** writes the note as ``update_<timestamp>.json`` and reports it as
   needing a person. Words are not a workbook: a curator reads them and files what they
   say as a correction on the verdict form, or asks for the file.

Every filed response is recorded in ``fetched_updates.json`` so it is never filed twice.
A dormant or declined submission gets its revision like any other and is reported as
needing a lead's reopening (``close_submission.py --reopen``): the files and the ledger are
ready, and the state change is a lead's decision.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation import google_auth, ledger as ledger_mod           # noqa: E402
from malavi_curation.config import load_config, repo_root               # noqa: E402
from malavi_curation.submission_id import directory_for, is_opaque      # noqa: E402

# The sheet reader and the Drive download live in fetch_submissions.py, a script; loaded
# from there rather than copied, so there is one Drive download in the project.
_FETCH = importlib.util.spec_from_file_location(
    "_fetch_submissions", Path(__file__).resolve().parent / "fetch_submissions.py")
fetch_submissions = importlib.util.module_from_spec(_FETCH)
sys.modules["_fetch_submissions"] = fetch_submissions
_FETCH.loader.exec_module(fetch_submissions)

# Columns matched on a lowercase substring, as the submission fetch does.
ID_COLUMN_HINT = "submission id"
NOTE_COLUMN_HINT = "what changed"
FILE_COLUMN_HINTS = fetch_submissions.FILE_COLUMN_HINTS

# Where a replaced workbook and its report go, inside the submission's directory.
SUPERSEDED_DIR = "_superseded"

# The files a new workbook supersedes: the screen's outputs, which describe the old one.
SCREEN_OUTPUTS = ("report.html", "report.pdf", "screen.json", "checks.json")

RECORD_NAME = "fetched_updates.json"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _column(row: Dict[str, str], hint: str) -> str:
    for question, value in row.items():
        if hint in (question or "").lower():
            return str(value or "").strip()
    return ""


def response_key(row: Dict[str, str]) -> str:
    """One response, once: the form's timestamp and the id it names."""
    return f"{row.get('Timestamp', '')}|{_column(row, ID_COLUMN_HINT)}"


def parse_response(row: Dict[str, str]) -> Dict[str, Any]:
    """The parts of one response this program acts on."""
    submission_id = _column(row, ID_COLUMN_HINT).upper()
    files: List[str] = []
    for question, cell in row.items():
        if any(hint in (question or "").lower() for hint in FILE_COLUMN_HINTS):
            files.extend(fetch_submissions.drive_ids(cell))
    return {"submission_id": submission_id, "note": _column(row, NOTE_COLUMN_HINT),
            "email": str(row.get("Email Address") or row.get("Email address") or "").strip(),
            "files": files, "timestamp": str(row.get("Timestamp") or "")}


def set_aside(directory: Path, stamp: str) -> List[str]:
    """Move the current workbook(s) and the screen's outputs under _superseded/<stamp>/."""
    moved: List[str] = []
    target = directory / SUPERSEDED_DIR / stamp
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        is_workbook = path.suffix.lower() == ".xlsx" and not path.name.startswith("~$")
        if is_workbook or path.name in SCREEN_OUTPUTS:
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target / path.name))
            moved.append(path.name)
    return moved


def file_update(inbox: Path, entries: Dict[str, Any], update: Dict[str, Any], *,
                token: Optional[str], dry_run: bool, stamp: str) -> Dict[str, Any]:
    """File one update. Returns a dict with ``status`` and a ``note`` for the console.

    ``status`` is one of ``filed`` (a new revision), ``noted`` (words only; a person
    files them), ``skipped`` (nothing to revise) or ``failed`` (a download failed).
    """
    submission_id = update["submission_id"]
    if not submission_id or not is_opaque(submission_id):
        return {"status": "skipped", "note": f"'{submission_id}' is not a submission id"}
    entry = entries.get(submission_id)
    if entry is None:
        return {"status": "skipped", "note": "not in the review ledger"}
    if entry.state in ("released", "withdrawn"):
        return {"status": "skipped", "note": f"its state is {entry.state}; nothing to revise"}
    directory_name = directory_for(inbox, submission_id)
    if not directory_name or not (inbox / directory_name).is_dir():
        return {"status": "skipped", "note": "no directory for this id"}
    directory = inbox / directory_name

    if not update["files"]:
        if not dry_run:
            (directory / f"update_{stamp}.json").write_text(
                json.dumps(update, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"status": "noted",
                "note": "words only, no file: a curator reads the note and files it as a "
                        "correction on the verdict form, or asks for the workbook"}

    if dry_run:
        return {"status": "filed", "note": f"would file {len(update['files'])} file(s) as "
                                           f"revision {entry.revision + 1}"}

    moved = set_aside(directory, stamp)
    downloaded: List[str] = []
    for file_id in update["files"]:
        try:
            path = fetch_submissions.download_drive_file(file_id, directory, token)
        except Exception as exc:                                     # noqa: BLE001
            path = None
            print(f"      FAILED {file_id}: {exc}")
        if path is None:
            # Put things back: a directory with its workbook gone and nothing new in
            # it would be screened as a paper without a template.
            for name in moved:
                shutil.move(str(directory / SUPERSEDED_DIR / stamp / name), str(directory / name))
            return {"status": "failed", "note": f"download of {file_id} failed; nothing changed"}
        downloaded.append(path.name)

    (directory / f"update_{stamp}.json").write_text(
        json.dumps({**update, "downloaded": downloaded, "superseded": moved},
                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    reason = (f"update from the submitter on {update['timestamp'] or stamp}"
              + (f": {update['note'][:300]}" if update["note"] else ""))
    revision = ledger_mod.bump_revision(entry, reason, authority="submitter")
    note = (f"filed {', '.join(downloaded)} as revision {revision.number}; "
            f"{len(moved)} file(s) set aside under {SUPERSEDED_DIR}/{stamp}")
    outcome = {"status": "filed", "note": note}
    if entry.state in ledger_mod.CLOSED_STATES:
        outcome["note"] += (f"; the submission is {entry.state}: a lead reopens it "
                            f"(close_submission.py --reopen)")
        outcome["attention"] = True
    return outcome


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be filed; download and write nothing")
    arguments = parser.parse_args(argv)

    configuration = load_config()
    submissions = configuration.get("submissions") or {}
    sheet_id = submissions.get("update_responses_sheet")
    print("== malavi_rebuild :: fetch_updates ==")
    if not sheet_id:
        # The form does not exist until it is created in the browser (RUNBOOK). Not an
        # error: this runs inside the intake cycle, and an optional step that is not set
        # up must not turn every cycle red.
        print("config/project.yml has no submissions.update_responses_sheet: the update "
              "form is not set up, so there is nothing to fetch.")
        return 0
    inbox = repo_root() / submissions.get("inbox_dir", "curation/intake/submissions")

    print(google_auth.describe())
    try:
        token = google_auth.access_token()
    except google_auth.CredentialError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    if token is None:
        print("\nNo Google credential configured; see curation/GOOGLE_ACCESS.md.",
              file=sys.stderr)
        return 1

    try:
        rows = fetch_submissions.fetch_responses(sheet_id, token)
    except Exception as exc:                                         # noqa: BLE001
        print(f"\ncould not read the update form's responses: {exc}", file=sys.stderr)
        return 1
    print(f"update form: {len(rows)} response(s)\n")

    record_path = inbox / RECORD_NAME
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    counts: Dict[str, int] = {}
    attention = 0
    stamp = _stamp()
    with ledger_mod.open_ledger(inbox, write=not arguments.dry_run) as entries:
        for row in rows:
            key = response_key(row)
            if key in record:
                continue
            update = parse_response(row)
            outcome = file_update(inbox, entries, update, token=token,
                                  dry_run=arguments.dry_run, stamp=stamp)
            counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
            if outcome["status"] in ("noted", "skipped") or outcome.get("attention"):
                attention += 1
            print(f"  {update['submission_id'] or '(no id)'}: [{outcome['status']}] {outcome['note']}")
            if not arguments.dry_run and outcome["status"] in ("filed", "noted", "skipped"):
                record[key] = {"at": stamp, "status": outcome["status"],
                               "submission": update["submission_id"]}
    if not arguments.dry_run:
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")

    print("\n" + (", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
                  or "nothing new"))
    if arguments.dry_run:
        print("[dry-run] nothing was written.")
        return 0
    if counts.get("failed"):
        return 1
    # Exit 2, "a person is needed": a note without a file, an update to a closed
    # submission (a lead reopens it), or an id this program could not place.
    return 2 if attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
