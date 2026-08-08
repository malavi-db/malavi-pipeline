"""Enroll fetched submissions into the review ledger.

**The join that was missing.** ``fetch_submissions.py`` lays each submission out on disk
and ``check_template.py`` screens it, and neither of them ever told the review ledger that
the submission existed. So the ledger was empty, and every rule that acts on an entry —
every verdict, every clock, every reserved name — had nothing to act on. A curator's
approval would have been filed as "unknown submission", which reads like a mistyped
identifier rather than like a system with a missing step.

**Enrolling is not deciding.** All this does is create the entry and put it in front of a
curator. It records what the submission claims and what the screen found; it forms no
opinion about whether any of it is right. That is the curator's job and the ledger refuses
to let this module do it.

**It is idempotent, and it is conservative about what it will overwrite.** The daily job
re-runs over the same submissions forever. Creating a second entry would split one
submission's verdicts across two records, so :func:`ledger.ensure_entry` is what creates
them. Re-running must also never undo curation: a submission a curator has already picked
up, held or approved keeps its state, and its reserved names are left alone once they have
moved past ``claimed`` — by then they are what a curator agreed to, and a re-read of the
screen would silently replace an agreed name with a freshly suggested one.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import ledger

# Screen results are read through the functions below rather than by re-reading a
# submitter's workbook, so that the names the ledger reserves are exactly the names the
# reservation feed advertises and exactly the names the curator was shown. Three readers of
# one workbook would be three chances to disagree.


def claimed_names(sub_dir: Path) -> List[str]:
    """The new lineage names one submission proposes, from its screening report.

    A submission that has not been screened yet claims nothing — it is in the queue, and
    its names appear on the next run after screening.
    """
    screen_path = Path(sub_dir) / "screen.json"
    if not screen_path.is_file():
        return []
    try:
        reports = json.loads(screen_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    if isinstance(reports, dict):
        reports = [reports]
    names: List[str] = []
    for report in reports:
        names.extend((report.get("lineages") or {}).keys())
    return names


def name_suggestions(sub_dir: Path) -> Dict[str, str]:
    """Free names the screen offered in place of names MalAvi already owns.

    Keyed by the taken name, upper-cased, because that is how the reservation feed keys
    them and a mismatch in case would mean the correction silently never applied.
    """
    path = Path(sub_dir) / "screen.json"
    if not path.is_file():
        return {}
    try:
        reports = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if isinstance(reports, dict):
        reports = [reports]
    suggestions: Dict[str, str] = {}
    for report in reports:
        for taken, free in (report.get("name_suggestions") or {}).items():
            suggestions[str(taken).strip().upper()] = str(free)
    return suggestions


def screened(sub_dir: Path) -> Optional[bool]:
    """Whether the screen produced a usable result.

    ``True`` it ran and read at least one workbook, ``False`` it ran and could read none,
    ``None`` it has not run. The three are genuinely different: a submission whose checks
    could not complete must not sit in the queue looking like one that passed them, and a
    submission nobody has screened yet must not look like one that failed.
    """
    path = Path(sub_dir) / "screen.json"
    if not path.is_file():
        return None
    try:
        reports = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    if isinstance(reports, dict):
        reports = [reports]
    return any(report.get("workbook") for report in reports)


def received_at(sub_dir: Path) -> str:
    """When the submission arrived, at full precision, as ISO 8601 UTC.

    **Not truncated to a date.** The public reservation feed shows a date and only a date,
    deliberately; the ledger must not, because name-reservation priority is decided by the
    earliest Form timestamp and two submissions on one day would otherwise tie. A tie
    resolved by whichever directory sorted first is a priority claim decided by filename.

    Falls back to the directory name, which is itself built from the same timestamp, and
    finally to the directory's own mtime — never to "now", which would reset the clock of
    every submission on every run.
    """
    sub_dir = Path(sub_dir)
    meta_path = sub_dir / "metadata.json"
    raw = ""
    if meta_path.is_file():
        try:
            raw = str((json.loads(meta_path.read_text(encoding="utf-8"))
                       or {}).get("Timestamp") or "").strip()
        except (ValueError, UnicodeDecodeError):
            raw = ""

    # The sheet writes US-style dates, but a re-export or a locale change can hand back
    # ISO instead. Both are accepted; the sheet's timezone is UTC by configuration.
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return (datetime.strptime(raw, fmt)
                    .replace(tzinfo=timezone.utc).isoformat())
        except ValueError:
            continue
    try:
        moment = datetime.fromisoformat(raw)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass

    # Directory names are <YYYYMMDD>T<HHMMSS>_<slug>, which carries the full time.
    stem = sub_dir.name.split("_")[0]
    try:
        return (datetime.strptime(stem, "%Y%m%dT%H%M%S")
                .replace(tzinfo=timezone.utc).isoformat())
    except ValueError:
        pass

    return (datetime.fromtimestamp(sub_dir.stat().st_mtime, tz=timezone.utc)
            .replace(microsecond=0).isoformat())


def enroll_one(entries: Dict[str, ledger.Entry], submission_id: str, sub_dir: Path,
               track: str = "A") -> Dict[str, Any]:
    """Ensure one submission has a ledger entry in the right state.

    Returns a small record of what happened, for the caller to print. Never raises on a
    submission it cannot advance: the entry existing at all is the valuable part, and a
    state that would not move is a thing to report.
    """
    sub_dir = Path(sub_dir)
    arrived = received_at(sub_dir)

    existed = submission_id in entries
    entry = ledger.ensure_entry(entries, submission_id, track, arrived)

    result: Dict[str, Any] = {"submission_id": submission_id, "directory": sub_dir.name,
                              "created": not existed, "state": entry.state, "note": ""}

    # Reserved names, but only while they are still merely claimed. Once a curator has
    # approved the submission the names are held or confirmed and represent an agreement;
    # re-reading the screen then could replace an agreed name with a newly suggested one
    # and nobody would see it happen.
    if entry.name_state == "claimed":
        entry.reserved_names = sorted(set(claimed_names(sub_dir)))
        entry.name_corrections = name_suggestions(sub_dir)

    # A submission a curator has already touched keeps whatever state they put it in.
    # Enrollment is allowed to move it off the intake states and nowhere else.
    if entry.state not in ("received", "screening_failed"):
        return result

    outcome = screened(sub_dir)
    if outcome is None:
        result["note"] = "not screened yet; stays in the queue"
        return result

    target = "ready_for_review" if outcome else "screening_failed"
    if entry.state != target:
        try:
            ledger.transition(entry, target, actor="intake", at=arrived)
        except ledger.LedgerError as exc:
            result["note"] = f"could not move to {target}: {exc}"
            return result
    result["state"] = entry.state
    result["note"] = ("screened; waiting for a curator" if outcome
                      else "the screen could not read a workbook")
    return result
