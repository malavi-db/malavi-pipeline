"""Stable, opaque identifiers for submissions.

A submission arrives in a directory named after the person who sent it —
``20260727T233146_Vincenzo_Ellis``. That name is fine where the submission itself lives,
which is a gitignored tree only curators can read. It is not fine anywhere else, and
"anywhere else" turns out to be a long list: a GitHub issue's metadata, an artifact
filename, a workflow log, the decision record, and the notification email that GitHub sends
to every watcher and that nobody can ever unsend.

So the directory name stays private and everything public-facing uses a minted identifier:

    MALAVI-SUB-2026-000123

It carries a year, so a curator reading a list can tell recent from old, and a sequence
number, which says nothing about anybody. The mapping between the two lives in one file
beside the submissions, and is the only place the two forms appear together.

**The identifier is assigned once and never changes.** A curator's decision, a name
reservation and a published record may all point at it years later, so re-minting one would
orphan every reference. That is why the ledger is append-only and why a lookup for a
directory that already has an id returns it rather than issuing a new one.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# MALAVI-SUB-<year>-<six digits>. Six digits is far more than MalAvi will ever need, and
# fixed width keeps identifiers sortable as plain text.
ID_PATTERN = re.compile(r"^MALAVI-SUB-(\d{4})-(\d{6})$")

_LEDGER_NAME = "submission_ids.json"


def is_opaque(value: str) -> bool:
    """Does this look like a minted identifier rather than an intake directory name?

    Used by the tests that guard against a directory name leaking into a public surface.
    """
    return bool(ID_PATTERN.match(value or ""))


def _ledger_path(inbox: Path) -> Path:
    return Path(inbox) / _LEDGER_NAME


def load_ledger(inbox: Path) -> Dict[str, Any]:
    """Read the id ledger, or an empty one if it does not exist yet."""
    path = _ledger_path(inbox)
    if not path.is_file():
        return {"version": 1, "next": 1, "ids": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Refuse rather than start a fresh ledger: overwriting it would re-mint
        # identifiers that other records already point at.
        raise ValueError(
            f"{path} is unreadable ({exc}). Fix or restore it before minting more "
            f"identifiers — starting a new ledger would re-issue ids that decisions, "
            f"reservations and issues already reference.") from exc
    data.setdefault("version", 1)
    data.setdefault("ids", {})
    data.setdefault("next", 1)
    return data


def _save_ledger(inbox: Path, ledger: Dict[str, Any]) -> None:
    """Write the ledger atomically, so an interrupted run cannot truncate it."""
    path = _ledger_path(inbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def submission_id_for(inbox: Path, directory: str,
                      year: Optional[int] = None) -> str:
    """The identifier for one submission directory, minting one if it has none.

    Idempotent by design: calling it twice for the same directory returns the same
    identifier. The daily job runs every day over the same submissions, so anything else
    would issue a new id per run and detach every existing reference.
    """
    inbox = Path(inbox)
    ledger = load_ledger(inbox)

    existing = ledger["ids"].get(directory)
    if existing:
        return existing["id"]

    year = year or datetime.now(timezone.utc).year
    number = int(ledger["next"])
    minted = f"MALAVI-SUB-{year}-{number:06d}"

    ledger["ids"][directory] = {
        "id": minted,
        "minted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ledger["next"] = number + 1
    _save_ledger(inbox, ledger)
    return minted


def directory_for(inbox: Path, submission_id: str) -> Optional[str]:
    """The private directory an identifier refers to, or None.

    The reverse lookup exists for a curator working from an issue back to the files. It is
    deliberately the only way back: nothing else should be able to derive a submitter's
    name from a public identifier.
    """
    ledger = load_ledger(Path(inbox))
    for directory, entry in ledger["ids"].items():
        if entry.get("id") == submission_id:
            return directory
    return None


def issue_link(inbox: Path, submission_id: str) -> Optional[int]:
    """The GitHub issue number already opened for this submission, if any."""
    ledger = load_ledger(Path(inbox))
    for entry in ledger["ids"].values():
        if entry.get("id") == submission_id:
            return entry.get("issue")
    return None


def record_issue(inbox: Path, submission_id: str, issue_number: int) -> None:
    """Remember which issue belongs to this submission.

    This is what makes the daily job idempotent. Without it the job would open a fresh
    issue for the same submission every morning, and every curator would be notified
    again each time.
    """
    inbox = Path(inbox)
    ledger = load_ledger(inbox)
    for entry in ledger["ids"].values():
        if entry.get("id") == submission_id:
            entry["issue"] = int(issue_number)
            _save_ledger(inbox, ledger)
            return
    raise KeyError(f"{submission_id} is not in the ledger; mint it first")
