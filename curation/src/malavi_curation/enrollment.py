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
from typing import Any, Dict, Iterable, List, Optional

from . import form_metadata, ledger

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


def standing_claims(inbox: Path, *, exclude: Optional[Iterable[str]] = None,
                    known: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Every lineage name a *pending* submission has already claimed → whose it is.

    Keys are upper-cased names; values are the directory that claimed each one. Names the
    release already owns are left out: those are not reservations, and the caller already
    knows them from the snapshot.

    **Why this exists.** The screen's only notion of "taken" was the released names in
    ``db_snapshot.json``, so a name claimed by a submission that arrived a week earlier
    passed the screen without comment — and the free-name search could offer submission B
    the very name submission A had been offered. Both submitters were then emailed "your
    names are confirmed", and the clash surfaced at ingest, after somebody may already
    have deposited the name in GenBank. The website's own checker consulted these claims;
    the curator pipeline did not, which made the public tool stricter than the review.

    The same reading as the reservation feed, deliberately: :func:`claimed_names` with
    :func:`name_suggestions` applied over it, so a name the screen replaced with a free
    alternative reserves the alternative rather than the name that was never going to be
    granted. Two readers of one screen would be two chances to disagree.

    ``exclude`` is directory names to ignore — pass the submission being screened, or it
    collides with itself, plus anything in ``submissions.exclude`` from the config.

    This does not read the ledger, so a name whose submission was declined stays listed
    until the directory is excluded. That is the conservative direction (it never hands a
    name to a second person while the first still believes it is theirs) but it is not
    free: see the note in ``build_name_reservations.py``.
    """
    inbox = Path(inbox)
    if not inbox.is_dir():
        return {}
    skip = {str(name) for name in (exclude or ())}
    owned = {str(name).strip().upper() for name in (known or ())}
    claims: Dict[str, str] = {}
    for sub_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
        if sub_dir.name in skip:
            continue
        corrections = name_suggestions(sub_dir)
        for name in claimed_names(sub_dir):
            name = (name or "").strip().upper()
            if not name:
                continue
            replacement = (corrections.get(name) or "").strip().upper()
            if replacement and replacement not in owned:
                name = replacement
            if name in owned or name in claims:
                # First directory in sorted order keeps it. Priority between two claimants
                # is settled by arrival date in build_name_reservations.py, which is the
                # program that publishes it; here the only question is whether the name is
                # spoken for at all.
                continue
            claims[name] = sub_dir.name
    return claims


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


def submitter_metadata(sub_dir: Path) -> Dict[str, Any]:
    """The form answers beside a submission, or an empty mapping.

    Unreadable is treated as absent rather than raised, for the same reason the rest of
    this module does: a submission whose metadata will not parse is still a submission a
    curator must see, and the empty mapping is read conservatively downstream.
    """
    path = Path(sub_dir) / "metadata.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (ValueError, UnicodeDecodeError, OSError):
        return {}


def apply_embargo(entry: ledger.Entry, sub_dir: Path) -> Optional[bool]:
    """Set the entry's embargo from what the submitter asked for. Returns it if it moved.

    **Where the value comes from.** The form asks "If your data are unpublished, may we
    add the records to MalAvi now?", and :func:`form_metadata.records_are_held` reads it:
    held unless the data are published, or the submitter explicitly said "add them now".
    An unanswered question means held, which is what every submission fetched before
    2026-08-09 is, since it predates the question.

    **Why this is re-read on every run, but only until somebody decides.** The submitter
    can change their mind -- "the paper is out, go ahead" -- and that arrives by email, to
    be applied with :func:`ledger.set_embargo`. Once anybody has done that, this stops
    reading the form: re-imposing the original answer over a decision made since would
    keep withholding records whose author has already released them, silently, on a
    schedule. Until then the form answer stays authoritative, so a corrected metadata.json
    takes effect rather than being frozen at first sight.
    """
    if ledger.embargo_decided(entry):
        return None

    metadata = submitter_metadata(sub_dir)
    if not metadata:
        # No answers at all. Saying nothing is not the same as saying "publish", but this
        # is also not the place to invent a decision: the entry keeps its default, which
        # is False, and the submission is visible to a curator either way.
        return None

    wanted = form_metadata.records_are_held(metadata)
    if wanted == entry.embargoed:
        return None

    # Set directly rather than through ledger.set_embargo, deliberately: that function
    # records an EMBARGO_EVENTS entry, which is what `embargo_decided` reads to stop this
    # re-reading the form, and reading the form is not somebody deciding.
    #
    # But it still gets a history line. Until 2026-08-10 this was the one path that could
    # un-embargo a submission leaving no trace at all, so "why are these records public?"
    # had no answer in the record. The event name is deliberately NOT one of
    # EMBARGO_EVENTS, so it is auditable without being mistaken for a decision.
    entry.embargoed = wanted
    entry.history.append({
        "at": ledger.now_utc(), "event": "embargo_from_form", "actor": "intake",
        "to": wanted,
        "note": "read from the submitter's form answer; not a decision by a curator"})
    return wanted


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
                              "created": not existed, "state": entry.state, "note": "",
                              "embargoed": entry.embargoed, "embargo_note": ""}

    # Before the state machine, because an embargo is about publication rather than about
    # review: it applies to a submission in any state, including one a curator has already
    # approved, and the release gate is what reads it.
    #
    # Reported in its own field rather than in `note`, which the state logic below
    # overwrites on every path that does not return early -- an embargo applied to a
    # submission still waiting to be screened would otherwise be set silently.
    moved = apply_embargo(entry, sub_dir)
    if moved is not None:
        result["embargoed"] = moved
        result["embargo_note"] = ("records held until the submitter says the study is out"
                                  if moved else
                                  "submitter agreed the records may go public")

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
            # Stamped NOW, not at `arrived`. The submission arrived whenever it arrived,
            # but this transition is happening today, and stale_live() reads last activity
            # as max(history["at"]) -- so back-dating it meant a submission enrolled weeks
            # after it landed was reported stale on the day it entered the queue, and the
            # staleness report is how a forgotten submission gets noticed at all.
            # `received_at` still carries the true arrival, which is what name-reservation
            # priority is decided by.
            ledger.transition(entry, target, actor="intake")
        except ledger.LedgerError as exc:
            result["note"] = f"could not move to {target}: {exc}"
            return result
    result["state"] = entry.state
    result["note"] = ("screened; waiting for a curator" if outcome
                      else "the screen could not read a workbook")
    return result
