"""The submission ledger: one record per submission, and the rules that move it.

Every intake track — a template through the public Form, a paper a curator found, a
curator's own template, something that arrived by email — writes into this one ledger. It
is what the public queue, the curator view and release staging are all views of, and it is
the only place a submission's state is decided.

**What this module does and does not do.** It holds the state machine, the verdict rules
and the clocks. It does not send mail, does not talk to Google, does not open issues and
does not write to the record store. Everything here is pure enough to test, which matters
more than usual: these rules encode governance decisions that were argued about, and an
argument settled in a docstring but broken in code is not settled.

**The rules that are load-bearing**, each of which exists because the alternative failed a
specific test:

* *A hold blocks.* One approval plus a 24-hour wait is enough to release — but only if a
  second curator can actually stop it. Otherwise the wait is decorative.
* *An approval belongs to a revision; an objection belongs to the submission.* This
  asymmetry is the single most important thing in the file and it is implemented as an
  asymmetry, in :func:`approvals` and :func:`blocking_holds`, rather than emerging from a
  shared helper. A curator who approved revision 2 did not approve revision 3, however small
  the difference looks. But an unanswered objection is *not* answered by editing the file,
  so a hold carries forward across revisions until the curator who raised it speaks again,
  retracts it, or a lead clears it.
* *Whoever authored a revision cannot approve it.* A curator may resubmit on a submitter's
  behalf after consulting them. If that same curator then approves their own typing, one
  person is corrector and approver and the review gate has become a single point of
  judgment. They may still hold — objecting to your own work is not a conflict.
* *Automation never resolves a disagreement.* :func:`due_actions` reports what is ripe; it
  returns proposals, and a caller applies them. Every blocking rule is then re-checked
  inside :func:`transition`, at the moment of the write, including on the transition into
  ``released`` — which is the one that actually ships data and cannot be undone.
* *Nothing is deleted.* Retracted holds, superseded verdicts and cleared objections are all
  retained. Collapsing them into a single current status would erase the fact that somebody
  objected, which is precisely the fact a disputed record needs years later.

**Concurrency.** Two jobs write this file — the verdict fetcher and the promoter — so the
read-modify-write cycle is not safe on its own: the later writer would silently delete
whatever the earlier one recorded, which breaks "nothing is deleted" in the most damaging
way available. :func:`open_ledger` is the supported entry point: it takes an exclusive lock
and refuses to write a copy that went stale while it was held. The repository lives on NFS,
where advisory locking is real but attribute caching is not instant, so the lock is backed
up by a compare-and-swap on the file's size and mtime rather than trusted alone.

**Where it lives, and why not in git.** The ledger sits in the gitignored intake tree beside
the submissions, because verdict reason text quotes what was wrong with somebody's
unpublished data. What *is* committed is the decision record (:func:`decision_record`): a
handful of fields with no science in them, so that a withdrawn submission can be erased
while "what did we decide about that, and when?" stays answerable. One record cannot be both
a working interface and a permanent audit trail, so there are two. Everything that reaches
the committed record is a controlled value — an identifier, a date, a curator id, or a
reason drawn from a fixed vocabulary — because a field that accepts free prose will
eventually receive some.

The ledger holds only opaque submission identifiers. The mapping from those to a person
lives in ``submission_ids.json`` and is not duplicated here.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .config import load_config
from .curators import resolve

_LEDGER_NAME = "review_ledger.json"
_LOCK_NAME = ".review_ledger.lock"
_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------------------
# `held` is a branch rather than a stage: a submission comes back from it to `in_review`.
#
# Two states exist only because their absence caused a specific failure. `withdrawn`,
# because submitters do withdraw and there was nowhere to put them. `screening_failed`,
# because a submission whose automated checks could not run must not sit in the queue
# looking like one that passed them.
STATES = (
    "received",            # fetched, nothing done yet
    "screening_failed",    # the checks could not complete — NOT the same as failing them
    "ready_for_review",    # screened, report written, waiting for a curator
    "in_review",           # at least one curator has it
    "held",                # an unresolved objection blocks it
    "awaiting_submitter",  # we asked a question and cannot proceed until it is answered
    "approved",            # approved; the publish hold is running
    "declined",            # will not be included
    "released",            # published in a release
    "withdrawn",           # the submitter took it back
    "dormant",             # timed out waiting on the submitter; names released
)

# Which states a submission may move to from each state. Encoded rather than described,
# because a transition table in prose is one nobody can test.
ALLOWED_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "received": ("screening_failed", "ready_for_review", "withdrawn"),
    "screening_failed": ("ready_for_review", "withdrawn"),
    "ready_for_review": ("in_review", "awaiting_submitter", "withdrawn"),
    "in_review": ("held", "awaiting_submitter", "approved", "declined", "withdrawn"),
    "held": ("in_review", "awaiting_submitter", "declined", "withdrawn"),
    "awaiting_submitter": ("in_review", "ready_for_review", "dormant", "withdrawn",
                           "declined"),
    "approved": ("held", "released", "in_review", "withdrawn"),
    "declined": ("in_review",),          # reopening requires a deliberate act, and is logged
    "released": (),                      # terminal: a published release is not un-published here
    "withdrawn": (),                     # terminal
    # A late reply revives it; `declined` is how a name is finally given back, now that
    # going dormant no longer does that on its own.
    "dormant": ("in_review", "ready_for_review", "withdrawn", "declined"),
}

# States a submission can be in while still plausibly heading for a release.
LIVE_STATES = ("received", "screening_failed", "ready_for_review", "in_review", "held",
               "awaiting_submitter", "approved")

# States from which a return to the live workflow is a *reopening*: the submission was
# finished, and coming back has to undo the bookkeeping that finishing it did.
CLOSED_STATES = ("declined", "dormant")

# The lifecycle of a reserved lineage name, tracked alongside the submission because the
# two can diverge: a declined submission's names go back, an approved one's are confirmed
# only when a release actually ships, and a dormant one goes on holding its claim
# indefinitely.
NAME_STATES = ("claimed", "held", "confirmed", "released")

VERDICTS = ("approve", "hold", "decline")

# Verdicts that block. Both must carry written reasoning: the submitter has to be able to
# answer the objection and the lead has to be able to weigh it.
BLOCKING_VERDICTS = ("hold", "decline")

# What licensed a change the submitter did not type. Consulting another curator authorizes
# a judgment fix (a host synonym, a country spelling); only the author can authorize a data
# fix (which host, which locality, what the prevalence was). Two curators agreeing about
# somebody else's field data is still a guess.
AUTHORITIES = ("submitter", "author", "curator")

# Reasons a submission may be finished, as a closed vocabulary.
#
# This is the one field of the working ledger that reaches the *committed* decision record,
# whose entire premise is that it contains no unpublished science. A free-text parameter
# there would eventually receive a sentence describing somebody's data, in the one file that
# is meant to survive the erasure of that data. So it is a fixed list, and an unlisted value
# is refused rather than quietly written.
DISPOSITION_REASON_CODES = (
    "",                          # unspecified
    "duplicate",                 # already in MalAvi
    "out_of_scope",              # not avian haemosporidian data
    "unresolved_objection",      # declined because a hold was never answered
    "withdrawn_by_submitter",
    "submitter_unresponsive",    # the 60-day timeout expired
    "data_not_verifiable",       # the records could not be checked against the source
    "superseded",                # replaced by another submission
    "released_in_build",         # included in a constructed release
    "reopened",                  # cleared on revival; kept so history reads sensibly
    "objection_resolved",        # approved once the hold blocking it was cleared or
                                 # retracted, on an approval that already stood
)


# Why a submission may be DECLINED, as opposed to finished some other way. A subset of the
# codes above: the ones left out belong to other paths and would be false on a decline.
# `submitter_unresponsive` is the timeout's, written by promote.py; `released_in_build` is a
# release's; `reopened` is set on revival; `withdrawn_by_submitter` describes a different act
# by a different person.
#
# Here rather than in a caller because two interfaces now reach it -- a lead answering the
# verdict form, and the maintainer running close_submission.py -- and a vocabulary that
# lives in one of them is a vocabulary the other can disagree with.
DECLINE_REASON_CODES = (
    "duplicate",                 # already in MalAvi
    "out_of_scope",              # not avian haemosporidian data
    "unresolved_objection",      # a flag was never answered
    "data_not_verifiable",       # the records could not be checked against the source
    "superseded",                # replaced by another submission
)


class LedgerError(ValueError):
    """A rule in this module was violated. Never raised for anything a curator typed."""


# --------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------

@dataclass
class Verdict:
    """One curator's attributed judgment of one revision of one submission."""

    id: str                            # "V1", "V2", … unique within the submission
    curator: str                       # registry id, resolved from a verified address
    verdict: str                       # approve | hold | decline
    revision: int                      # which revision was judged
    at: str                            # ISO 8601 UTC
    reason_code: str = ""
    reason_text: str = ""
    # A hold the curator withdrew themselves. Kept, not deleted: the record that somebody
    # objected and later changed their mind is worth more than a tidy list.
    retracted_at: Optional[str] = None
    retracted_by: Optional[str] = None
    # A hold cleared by a lead over the objector's head. Distinct from a retraction,
    # because who resolved it is exactly what a later reader needs to know.
    overridden_by: Optional[str] = None

    @property
    def resolved(self) -> bool:
        """Whether this verdict has stopped counting — as an objection or as an approval."""
        return self.retracted_at is not None or self.overridden_by is not None

    @property
    def blocks(self) -> bool:
        """Whether this verdict currently stands in the way of a release."""
        return self.verdict in BLOCKING_VERDICTS and not self.resolved


@dataclass
class Override:
    """A lead clearing another curator's unresolved hold.

    The consultation fields are required and are not decoration. A bare "I discussed this"
    checkbox is an attestation with nothing behind it; naming who was consulted and when
    turns it into something a later reader can check. They are exported to the committed
    decision record for the same reason: an override recorded only in the erasable copy is
    an accountability trail that disappears exactly when it is needed.
    """

    verdict_id: str          # the hold being cleared
    by: str                  # lead curator id
    at: str
    consulted: List[str]     # who was spoken to — curator ids and/or free text
    consulted_on: str        # the date of that conversation
    consulted_how: str       # email, call, meeting …
    note: str = ""


@dataclass
class Correction:
    """A change a curator wants made to a submission, and who has agreed to it.

    Recorded against a standing flag and applied by nobody until a lead approves it. That
    order is the point: a correction changes what MalAvi will say about somebody else's
    study, so it goes through the same person who is already the only one able to clear
    another curator's objection. Without it, one curator could describe a change and have a
    maintainer apply it with no second view at all.
    """

    id: str                       # "C1", "C2", … unique within the submission
    by: str                       # curator id who proposed it
    at: str
    authority: str                # "author" | "curator" — who licensed the change
    consulted: List[str] = field(default_factory=list)
    consulted_on: str = ""
    change: str = ""              # what should change, in the curator's words
    approved_by: str = ""         # lead curator id; empty until approved
    approved_at: str = ""
    applied_at: str = ""          # set when it becomes a revision

    @property
    def approved(self) -> bool:
        return bool(self.approved_by)

    @property
    def applied(self) -> bool:
        return bool(self.applied_at)


@dataclass
class Revision:
    """One version of the submitted content, and who is responsible for it.

    ``revised_by`` is empty when the submitter produced it themselves and carries a curator
    id when a curator typed it on the submitter's behalf. The distinction has to survive in
    the record: a curator-typed revision is no longer something the submitter attested to,
    and the provenance is what replaces the attestation.
    """

    number: int
    at: str
    revised_by: str = ""          # "" = the submitter
    reason: str = ""
    authority: str = "submitter"  # one of AUTHORITIES
    consulted: List[str] = field(default_factory=list)


@dataclass
class Entry:
    """Everything the project knows about the progress of one submission."""

    submission_id: str
    track: str                    # A | B | C | D — see the submission-loop design
    received_at: str              # the ORIGINAL arrival; never moves on resubmission
    state: str = "received"
    revision: int = 1
    revisions: List[Revision] = field(default_factory=list)
    verdicts: List[Verdict] = field(default_factory=list)
    overrides: List[Override] = field(default_factory=list)
    corrections: List[Correction] = field(default_factory=list)
    reserved_names: List[str] = field(default_factory=list)
    # Proposed names MalAvi already owns, mapped to the free name offered instead.
    # Recorded by the screen and shown in the curator's report, so an approval is an
    # approval *including* these corrections -- "approved" on its own could not say which
    # name was agreed, and the name is the whole point of the submission.
    name_corrections: Dict[str, str] = field(default_factory=dict)
    name_state: str = "claimed"
    # When the submission entered awaiting_submitter — the start of the 60-day clock.
    awaiting_since: Optional[str] = None
    # When it was approved — the start of the publish hold.
    approved_at: Optional[str] = None
    release_target: str = ""
    # Unpublished work holds its name indefinitely. A submitter waiting on a journal is not
    # a submitter who has gone quiet, and the 60-day clock is for the second case: applying
    # it to the first would hand somebody's reserved name away while their paper is in
    # review. The submission can be approved -- the records are checked and agreed -- and
    # still be held out of every release until the work is public.
    embargoed: bool = False
    final_disposition: Optional[Dict[str, Any]] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    # Responses that arrived from an address the registry does not know, or that could not
    # be interpreted. Recorded rather than dropped, because a curator whose new address was
    # never added will otherwise believe they voted and wonder why nothing happened.
    unrecognized: List[Dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------

def now_utc() -> str:
    """The current time as the ledger writes it: ISO 8601, UTC, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse(stamp: str) -> datetime:
    """Read a ledger timestamp back. Accepts the trailing 'Z' form as well as '+00:00'."""
    text = str(stamp).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    # A naive timestamp is treated as UTC rather than as local time: local time would make
    # the 24-hour hold depend on which machine ran the promoter.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _validate_stamp(stamp: str, label: str) -> str:
    """Normalize a timestamp at the point of writing, refusing anything unreadable.

    Validating on the way in rather than on the way out is what stops one malformed value —
    a hand edit, a Google Sheet's ``MM/DD/YYYY`` rendering — from aborting a promoter scan
    across every submission in the ledger long after whoever wrote it has forgotten.
    """
    try:
        parsed = _parse(stamp)
    except (ValueError, TypeError) as exc:
        raise LedgerError(
            f"{label} is not a readable ISO 8601 timestamp: {stamp!r} ({exc}).") from exc
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _review_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The review clocks, from ``config/project.yml`` unless supplied directly.

    Validated on both paths. A caller-supplied dict gets exactly the same scrutiny as the
    file, because the value that would do the damage — a zero publish hold, silently
    disabling the wait a second curator relies on — is no less dangerous for having come
    from a caller than from a config file.

    **Either shape is accepted: the whole project config, or its ``review`` section.** Until
    2026-08-13 only the section was, and every caller in the repository passed the whole
    config — ``promote.py``, ``fetch_verdicts.py``, ``notify_submitters.py``,
    ``release_gate.py``. The lookups therefore missed and every one of them silently got the
    defaults back. It was invisible because the defaults happen to equal what
    ``config/project.yml`` says; the moment anyone lengthened the publish hold, the release
    gate and the submitter notices would have gone on using 24 hours with nothing to say so.
    Accepting both shapes here fixes every caller at once, and the two cannot be confused: a
    ``review`` section never contains a key called ``review``.
    """
    source = "config/project.yml review" if config is None else "review config"
    configured = config if config is not None else (load_config().get("review") or {})
    if "review" in configured:
        configured = configured.get("review") or {}

    settings = {
        "publish_hold_hours": configured.get("publish_hold_hours", 24),
        "awaiting_submitter_timeout_days":
            configured.get("awaiting_submitter_timeout_days", 60),
    }
    for key, value in settings.items():
        try:
            settings[key] = int(value)
        except (TypeError, ValueError) as exc:
            raise LedgerError(
                f"{source}.{key} must be a whole number, got {value!r}.") from exc
        if settings[key] <= 0:
            # Zero would silently disable the protection the value exists to provide.
            raise LedgerError(f"{source}.{key} must be positive, got {settings[key]}.")
    return settings


# --------------------------------------------------------------------------------------
# Load, save, lock
# --------------------------------------------------------------------------------------

def ledger_path(inbox: Path) -> Path:
    return Path(inbox) / _LEDGER_NAME


def _stamp_of(path: Path) -> Optional[Tuple[int, int]]:
    """A cheap fingerprint of the file on disk: (size, mtime_ns), or None if absent."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_size, info.st_mtime_ns)


def load(inbox: Path) -> Dict[str, Entry]:
    """Read the ledger, or an empty one if this is the first run.

    An unreadable or structurally wrong ledger raises rather than starting fresh. Starting
    fresh would silently discard every recorded verdict, and the loss would not be noticed
    until somebody asked who approved a record that is already published.
    """
    path = ledger_path(inbox)
    if not path.is_file():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LedgerError(
            f"{path} is unreadable ({exc}). Fix or restore it before recording anything "
            f"further — starting a new ledger would discard every verdict in it.") from exc

    if not isinstance(raw, dict) or "entries" not in raw:
        # A file that parses as JSON but has no `entries` key is a botched repair, not an
        # empty ledger. Loading it as empty is the exact outcome this function exists to
        # prevent.
        raise LedgerError(
            f"{path} has no 'entries' key. It parses as JSON but is not a ledger; restore "
            f"it rather than letting an empty ledger discard every verdict in it.")

    version = raw.get("version")
    if version != _SCHEMA_VERSION:
        raise LedgerError(
            f"{path} is schema version {version!r}, this code writes {_SCHEMA_VERSION}. "
            f"Reading it anyway would silently drop or misread fields.")

    entries: Dict[str, Entry] = {}
    for submission_id, payload in (raw.get("entries") or {}).items():
        try:
            entry = Entry(
                submission_id=submission_id,
                track=payload["track"],
                received_at=payload["received_at"],
                state=payload["state"],
                revision=int(payload["revision"]),
                revisions=[Revision(**r) for r in payload.get("revisions", [])],
                verdicts=[Verdict(**v) for v in payload.get("verdicts", [])],
                overrides=[Override(**o) for o in payload.get("overrides", [])],
                corrections=[Correction(**c) for c in payload.get("corrections", [])],
                reserved_names=list(payload.get("reserved_names", [])),
                name_corrections=dict(payload.get("name_corrections", {})),
                name_state=payload.get("name_state", "claimed"),
                awaiting_since=payload.get("awaiting_since"),
                approved_at=payload.get("approved_at"),
                release_target=payload.get("release_target", ""),
                embargoed=bool(payload.get("embargoed", False)),
                final_disposition=payload.get("final_disposition"),
                history=list(payload.get("history", [])),
                unrecognized=list(payload.get("unrecognized", [])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # Without this the dataclass constructors raise a bare TypeError naming a
            # keyword argument, which tells an operator nothing about what to do.
            raise LedgerError(
                f"{path}: entry {submission_id} is malformed ({exc}). Fix or restore the "
                f"ledger rather than recording anything further against it.") from exc

        # `received_at` carries the name-reservation claim and `state` drives every
        # transition; a missing or invalid one must stop the run, not default quietly.
        if entry.state not in STATES:
            raise LedgerError(f"{path}: entry {submission_id} has unknown state "
                              f"{entry.state!r}.")
        if entry.name_state not in NAME_STATES:
            raise LedgerError(f"{path}: entry {submission_id} has unknown name_state "
                              f"{entry.name_state!r}.")
        _validate_stamp(entry.received_at, f"{submission_id} received_at")

        seen_ids = [v.id for v in entry.verdicts]
        if len(seen_ids) != len(set(seen_ids)):
            # Duplicate ids make _find_verdict act on whichever came first, so a retraction
            # could clear a different curator's objection than the one it names.
            raise LedgerError(
                f"{path}: entry {submission_id} has duplicate verdict ids "
                f"({', '.join(sorted(seen_ids))}).")

        entries[submission_id] = entry
    return entries


def save(inbox: Path, entries: Dict[str, Entry],
         expect: Optional[Tuple[int, int]] = None) -> None:
    """Write the ledger atomically and durably.

    ``expect`` is the fingerprint :func:`load` saw, and passing it turns the write into a
    compare-and-swap: if another process wrote the file in between, this refuses rather
    than overwriting their work. That check is what makes the read-modify-write cycle safe
    for two jobs, and it is deliberately independent of the lock — the repository lives on
    NFS, where advisory locking works but is not something to trust alone.
    """
    path = ledger_path(inbox)
    path.parent.mkdir(parents=True, exist_ok=True)

    if expect is not None and _stamp_of(path) != expect:
        raise LedgerError(
            f"{path} changed on disk since it was read. Another job wrote to it; re-read "
            f"and re-apply rather than overwriting, or its verdicts are lost.")

    payload = {
        "version": _SCHEMA_VERSION,
        "written": now_utc(),
        "entries": {sid: asdict(entry) for sid, entry in sorted(entries.items())},
    }
    # The submission id is the dict key; carrying it inside the value too would let the two
    # disagree after a hand edit.
    for value in payload["entries"].values():
        value.pop("submission_id", None)

    # Preserve the existing permissions. mkstemp creates 0600 owned by whoever is running,
    # so without this every write would silently narrow the file and lock out a second
    # maintainer or a cron account on the shared filesystem.
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o640

    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            # os.replace is atomic against interruption, but without the flush+fsync the
            # renamed inode can still contain zero bytes after a power loss.
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

    # Persist the rename itself, not just the data.
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    except OSError:
        pass          # some network filesystems refuse to fsync a directory; the data is safe
    finally:
        os.close(directory)


@contextmanager
def open_ledger(inbox: Path, write: bool = True) -> Iterator[Dict[str, Entry]]:
    """The supported way to change the ledger: lock, read, modify, write, unlock.

    Two jobs write this file. Without the lock, the promoter reading at 06:00:00 and saving
    at 06:00:20 would erase a hold the fetcher recorded at 06:00:15 — not superseded, not
    retracted, *deleted*, which is the one thing this module promises never to happen.

    Use it as::

        with open_ledger(inbox) as entries:
            entry = ensure_entry(entries, sid, "A", received)
            record_verdict(entry, address, "approve")

    Nothing is written if the body raises, so a rule violation part-way through leaves the
    ledger exactly as it was.
    """
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    lock_file = inbox / _LOCK_NAME

    handle = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o640)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        stamp = _stamp_of(ledger_path(inbox))
        entries = load(inbox)
        yield entries
        if write:
            save(inbox, entries, expect=stamp)
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def ensure_entry(entries: Dict[str, Entry], submission_id: str, track: str,
                 received_at: str) -> Entry:
    """Fetch the entry for a submission, creating it on first sight.

    Idempotent: the daily job runs over the same submissions repeatedly, and a second entry
    for one submission would split its verdicts across two records.

    ``received_at`` is only applied at creation. A resubmission is revision *n+1* of an
    existing submission, not a new one, and if its later arrival time overwrote the
    original the submitter would lose their name-reservation priority to somebody who filed
    after them — a harm caused entirely by our filing.
    """
    existing = entries.get(submission_id)
    if existing is not None:
        return existing

    stamp = _validate_stamp(received_at, "received_at")
    entry = Entry(submission_id=submission_id, track=track, received_at=stamp)
    entry.revisions.append(Revision(number=1, at=stamp, authority="submitter"))
    entry.history.append({"at": stamp, "event": "created", "state": "received",
                          "actor": "intake", "track": track})
    entries[submission_id] = entry
    return entry


# The history events that mean somebody decided about the embargo on purpose, rather than
# it being read off the submitter's original form answer. Enrollment consults this before
# re-reading metadata.json: without it, a maintainer lifting an embargo because the
# submitter emailed "the paper is out" would have it silently re-imposed on the next
# intake run, and the release would go on withholding records the author had released.
EMBARGO_EVENTS = ("embargo_set", "embargo_lifted")


def embargo_decided(entry: Entry) -> bool:
    """Has anybody explicitly set or lifted this submission's embargo?"""
    return any(event.get("event") in EMBARGO_EVENTS for event in entry.history)


def set_embargo(entry: Entry, embargoed: bool, actor: str, at: Optional[str] = None,
                note: str = "") -> None:
    """Hold this submission's records out of releases, or stop holding them.

    **What an embargo is and is not.** It withholds *publication* of the records. It does
    not withhold review: the submission can be screened, approved, and have its lineage
    names confirmed and reserved while embargoed, which is the point — a submitter waiting
    on a journal gets their name secured without their unpublished data being released
    from under them.

    ``released`` is refused because it is terminal and already published. Embargoing after
    the fact would put a claim in the ledger that the released ZIP contradicts, and the
    ledger cannot un-publish something people have downloaded.

    ``note`` is free text and is kept here only; unlike a disposition reason it never
    reaches the committed decision record, so it is not a closed vocabulary.
    """
    if entry.state == "released":
        raise LedgerError(
            f"{entry.submission_id} is released; its records are already published and an "
            f"embargo recorded now would contradict the release.")

    stamp = _validate_stamp(at or now_utc(), "embargo timestamp")
    was = entry.embargoed
    entry.embargoed = bool(embargoed)
    # Recorded even when the value did not change. "Somebody looked at this and confirmed
    # it should stay held" is a different fact from "nobody has considered it since
    # intake", and the second is the one that lets enrollment keep re-reading the form.
    entry.history.append({
        "at": stamp,
        "event": "embargo_set" if entry.embargoed else "embargo_lifted",
        "actor": actor, "from": was, "to": entry.embargoed, "note": note})


# --------------------------------------------------------------------------------------
# Reading an entry's review status
# --------------------------------------------------------------------------------------

def _sort_key(entry: Entry, verdict: Verdict) -> Tuple[datetime, int]:
    """Order verdicts by recorded time, using arrival order only to break exact ties.

    List order alone is not enough: callers stamp verdicts with an explicit time, and a
    re-fetch of an edited sheet, a backfill, or a merge of the email track can append an
    older verdict after a newer one. If that happened, a curator's superseded approval
    would silently become their standing position and their later hold would vanish from
    the gate while still sitting in the list looking recorded.
    """
    return (_parse(verdict.at), entry.verdicts.index(verdict))


def verdicts_for_revision(entry: Entry, revision: Optional[int] = None) -> List[Verdict]:
    """Every verdict recorded against one revision, oldest first."""
    target = entry.revision if revision is None else revision
    matching = [v for v in entry.verdicts if v.revision == target]
    return sorted(matching, key=lambda v: _sort_key(entry, v))


def current_verdicts(entry: Entry) -> Dict[str, Verdict]:
    """Each curator's standing position **on the current revision**, keyed by curator id.

    A curator may record more than once — the form cannot stop them, and they may genuinely
    change their mind after reading a colleague's objection. The most recent entry is their
    position; the earlier ones stay in ``entry.verdicts`` as history. Deduplicating by
    deleting would destroy the record of a mind being changed, which is the interesting
    part.
    """
    standing: Dict[str, Verdict] = {}
    for verdict in verdicts_for_revision(entry):
        standing[verdict.curator] = verdict     # later entries overwrite earlier ones
    return standing


def standing_positions(entry: Entry) -> Dict[str, Verdict]:
    """Where every curator stands right now, carrying unanswered objections forward.

    This is the asymmetry the design rests on, in one function. A curator who has spoken
    about the *current* revision is represented by what they said about it. A curator who
    has not, but who left an unresolved objection against an earlier revision, keeps that
    objection: editing the file is not an answer to it, and the alternative — which is what
    a purely revision-scoped lookup does — means any correction whatsoever silently clears
    every outstanding hold and nobody is told.
    """
    carried: Dict[str, Verdict] = {}
    for verdict in sorted(entry.verdicts, key=lambda v: _sort_key(entry, v)):
        if verdict.revision < entry.revision and verdict.blocks:
            carried[verdict.curator] = verdict

    standing = dict(carried)
    standing.update(current_verdicts(entry))    # speaking about the new revision supersedes
    return standing


def blocking_holds(entry: Entry) -> List[Verdict]:
    """Unresolved objections that stand in the way — from any revision."""
    return [v for v in standing_positions(entry).values() if v.blocks]


def hold_elapsed(stamp: str, config: Optional[Dict[str, Any]] = None,
                 now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Has the publish hold run out since ``stamp``? With the reason if not.

    Only the arithmetic, deliberately: *which* timestamp a caller measures from, and what
    else must be true besides the clock, differ by caller and stay with the caller.
    ``notify_submitters`` measures from the approval or from the decline; the public queue
    measures from the approval only.

    Two behaviors are worth keeping in one place rather than in each of them:

    * the hours are read through :func:`_review_config`, which coerces to int and refuses
      zero — a zero hold silently disables the wait a second curator relies on, and a
      quoted ``"24"`` in YAML used to raise ``TypeError`` and kill the whole run;
    * an unreadable timestamp fails **this one submission**, not the scan. The same bug
      was fixed in :func:`due_actions` for the same reason: one bad value used to stop
      every clock for every submission in the ledger until somebody noticed.
    """
    if not stamp:
        return False, "carries no timestamp for when that happened"

    hours = _review_config(config)["publish_hold_hours"]
    try:
        moment = now or datetime.now(timezone.utc)
        waited = moment - _parse(stamp)
    except (ValueError, TypeError) as exc:
        return False, f"unreadable timestamp {stamp!r} ({exc})"

    if waited < timedelta(hours=hours):
        return False, f"hold has {timedelta(hours=hours) - waited} left to run"
    return True, ""


def approvals(entry: Entry) -> List[Verdict]:
    """Standing approvals **of the current revision**, excluding withdrawn ones."""
    return [v for v in current_verdicts(entry).values()
            if v.verdict == "approve" and not v.resolved]


def is_approvable(entry: Entry) -> Tuple[bool, str]:
    """Whether the current revision may advance, and why not if it may not.

    One approval is enough. No number of approvals is enough while an objection stands:
    dissent outranks approval, and that ordering is the whole reason the publish hold is
    worth having.
    """
    holds = blocking_holds(entry)
    if holds:
        holders = ", ".join(sorted({v.curator for v in holds}))
        return False, f"unresolved objection from {holders}"
    if not approvals(entry):
        return False, "no standing approval on the current revision"
    return True, ""


def releasable(entries: Dict[str, Entry]) -> List[str]:
    """Approved submissions a release may actually take.

    Embargoed ones are excluded however long they have been approved. Their names stay
    reserved and their records stay agreed; what is withheld is publication, because the
    submitter's own work is not public yet and a release would scoop them with their own
    data.
    """
    return sorted(sid for sid, e in entries.items()
                  if e.state == "approved" and not e.embargoed)


def agreed_names(entry: Entry) -> List[str]:
    """The names this submission actually claims, corrections applied.

    A submitter proposing TUMIG06 when MalAvi already has one is offered TUMIG25; if the
    submission is approved, TUMIG25 is what gets reserved and what gets released. Reading
    it through this function rather than off ``reserved_names`` directly is what stops the
    public queue advertising a name that was never going to be granted.
    """
    return sorted({entry.name_corrections.get(name, name) for name in entry.reserved_names})


def author_of_revision(entry: Entry, revision: Optional[int] = None) -> str:
    """The curator who typed a revision, or "" if the submitter produced it."""
    target = entry.revision if revision is None else revision
    for record in reversed(entry.revisions):
        if record.number == target:
            return record.revised_by
    return ""


# --------------------------------------------------------------------------------------
# Recording things
# --------------------------------------------------------------------------------------

def _next_id(existing: Iterable[Any], prefix: str) -> str:
    """The next free id of a series, derived from the highest in use rather than the count.

    The ledger is a hand-editable file. Deriving from the count means deleting one row makes
    the next write re-issue an id that another record already has, after which a retraction
    can clear a different curator's objection than the one it names -- or a lead can approve
    a correction they never read.

    Shared by verdicts and corrections. Corrections minted ids by counting until
    2026-08-10, 200 lines below this docstring explaining why counting is wrong, so an
    entry holding C1 and C2 with C1 hand-deleted minted C2 a second time; and
    :func:`approve_correction` returns the first match.
    """
    highest = 0
    for item in existing:
        identifier = getattr(item, "id", "") or ""
        if identifier.startswith(prefix) and identifier[len(prefix):].isdigit():
            highest = max(highest, int(identifier[len(prefix):]))
    return f"{prefix}{highest + 1}"


def _next_verdict_id(entry: Entry) -> str:
    """The next free verdict id."""
    return _next_id(entry.verdicts, "V")


def record_verdict(entry: Entry, address: str, verdict: str, reason_code: str = "",
                   reason_text: str = "", at: Optional[str] = None,
                   revision: Optional[int] = None,
                   registry_path: Optional[Path] = None) -> Optional[Verdict]:
    """Record one curator's verdict, resolving them from their verified email address.

    Returns the stored :class:`Verdict`, or ``None`` when the response cannot be attributed
    to an active curator — in which case it is filed under ``entry.unrecognized`` for a
    maintainer to look at. It is neither honored nor discarded: honoring it would let anyone
    with the form link decide MalAvi's contents, and discarding it would silently lose the
    verdict of a curator whose new address was never added to the registry.

    A verdict names the revision it judges. The default is the current one. An explicit
    revision must already exist — a Google Form prefill is editable by the responder, and
    without this check an approval could be recorded against a revision that has not been
    written yet, sit inert, and then become the standing approval the moment somebody makes
    a correction. That is precisely "an approval carried across a revision bump".
    """
    if verdict not in VERDICTS:
        raise LedgerError(f"Unknown verdict {verdict!r}; expected one of {VERDICTS}.")

    stamp = _validate_stamp(at or now_utc(), "verdict timestamp")
    curator = resolve(address, registry_path)

    if curator is None or not curator.active:
        entry.unrecognized.append({
            "at": stamp,
            "address": address,
            "curator": curator.id if curator else "",
            "verdict": verdict,
            "note": ("curator is retired; not acted on" if curator
                     else "address not in the curator registry; not acted on"),
        })
        return None

    # Written reasoning is required for anything that blocks. The submitter has to be able
    # to answer the objection and the lead has to be able to weigh it.
    if verdict in BLOCKING_VERDICTS and not reason_text.strip():
        raise LedgerError(
            f"A {verdict!r} must carry written reasoning: it blocks the submission, and an "
            f"objection nobody can read cannot be answered or weighed.")

    if revision is None:
        target_revision = entry.revision
    else:
        try:
            target_revision = int(revision)
        except (TypeError, ValueError):
            # A non-numeric value came off a form field. File it rather than crashing the
            # fetch job for every other submission in the run.
            entry.unrecognized.append({
                "at": stamp, "address": address, "curator": curator.id,
                "verdict": verdict, "revision": str(revision),
                "note": "revision is not a number; not acted on"})
            return None
        if not 1 <= target_revision <= entry.revision:
            raise LedgerError(
                f"{entry.submission_id} has no revision {target_revision} (current is "
                f"{entry.revision}). A verdict cannot be recorded against a revision that "
                f"does not exist — it would become standing the moment one did.")

    # The self-approval rule, asked of the revision actually being judged. A curator who
    # typed that revision on the submitter's behalf may still object to it; what they may
    # not do is be both corrector and approver.
    if verdict == "approve":
        author = author_of_revision(entry, target_revision)
        if author and author == curator.id:
            raise LedgerError(
                f"{curator.id} authored revision {target_revision} of "
                f"{entry.submission_id} and cannot also approve it. Another curator must "
                f"approve; {curator.id} may still record a hold.")

    stored = Verdict(
        id=_next_verdict_id(entry),
        curator=curator.id,
        verdict=verdict,
        revision=target_revision,
        at=stamp,
        reason_code=reason_code,
        reason_text=reason_text,
    )
    entry.verdicts.append(stored)
    entry.history.append({"at": stamp, "event": "verdict", "actor": curator.id,
                          "verdict": verdict, "revision": target_revision,
                          "verdict_id": stored.id})
    return stored


def retract_verdict(entry: Entry, verdict_id: str, address: str,
                    at: Optional[str] = None,
                    registry_path: Optional[Path] = None) -> Verdict:
    """Withdraw an objection you placed yourself.

    Deliberately available to every curator, and deliberately restricted to your own
    verdict. Retracting your own objection is not an override — nobody's judgment is being
    set aside but your own — so it needs no lead and no consultation record.

    Only an objection can be retracted. Withdrawing an *approval* would need to mean
    "un-approve", and an approval is withdrawn by recording a different verdict, which keeps
    the change of mind visible as its own attributed act.
    """
    stamp = _validate_stamp(at or now_utc(), "retraction timestamp")
    curator = resolve(address, registry_path)
    if curator is None or not curator.active:
        raise LedgerError(f"{address} is not an active curator; cannot retract a verdict.")

    target = _find_verdict(entry, verdict_id)
    if target.verdict not in BLOCKING_VERDICTS:
        raise LedgerError(
            f"{verdict_id} is an {target.verdict!r}, not an objection. To withdraw an "
            f"approval, record a different verdict — that keeps the change of mind visible "
            f"as its own act rather than quietly deleting a position.")
    if target.curator != curator.id:
        raise LedgerError(
            f"{verdict_id} was recorded by {target.curator}, not {curator.id}. Clearing "
            f"another curator's hold is an override and requires a lead.")
    if target.resolved:
        raise LedgerError(f"{verdict_id} has already been resolved.")

    target.retracted_at = stamp
    target.retracted_by = curator.id
    entry.history.append({"at": stamp, "event": "retracted", "actor": curator.id,
                          "verdict_id": verdict_id})
    return target


def override_hold(entry: Entry, verdict_id: str, address: str, consulted: Iterable[str],
                  consulted_on: str, consulted_how: str, note: str = "",
                  at: Optional[str] = None,
                  registry_path: Optional[Path] = None) -> Override:
    """A lead clears an unresolved hold placed by another curator.

    The narrowest extra power a lead has, and the only one. It requires a record of who was
    consulted: an override nobody can see is indistinguishable from there having been no
    hold at all, and the people entitled to see it are the ones who were told about the
    objection in the first place.
    """
    stamp = _validate_stamp(at or now_utc(), "override timestamp")
    curator = resolve(address, registry_path)
    if curator is None or not curator.is_lead:
        raise LedgerError(
            f"{address} is not an active lead curator. Clearing another curator's hold is "
            f"a lead action; without that restriction dissent stops outranking approval.")

    target = _find_verdict(entry, verdict_id)
    if target.verdict not in BLOCKING_VERDICTS:
        raise LedgerError(f"{verdict_id} is an {target.verdict!r}; only an objection can "
                          f"be overridden.")
    if target.resolved:
        raise LedgerError(f"{verdict_id} has already been resolved.")
    if target.curator == curator.id:
        raise LedgerError(
            f"{verdict_id} is {curator.id}'s own verdict — retract it rather than "
            f"overriding it, so the record shows a mind changed and not a hold cleared.")

    consulted_list = [str(c) for c in consulted if str(c).strip()]
    if not consulted_list or not consulted_on.strip() or not consulted_how.strip():
        raise LedgerError(
            "An override must record who was consulted, when, and how. A bare "
            "acknowledgment that a discussion happened is an attestation with nothing "
            "behind it.")

    record = Override(verdict_id=verdict_id, by=curator.id, at=stamp,
                      consulted=consulted_list, consulted_on=consulted_on,
                      consulted_how=consulted_how, note=note)
    entry.overrides.append(record)
    target.overridden_by = curator.id
    entry.history.append({"at": stamp, "event": "override", "actor": curator.id,
                          "verdict_id": verdict_id, "consulted": consulted_list})
    return record


def decline(entry: Entry, address: str, reason: str, note: str = "",
            at: Optional[str] = None,
            config: Optional[Dict[str, Any]] = None,
            registry_path: Optional[Path] = None) -> None:
    """A lead closes a submission MalAvi will not include.

    **Lead-only, and checked here.** ``transition()`` takes ``actor`` as free text and does
    not resolve it, which is right for the promoter and the intake but wrong for this: a
    decline is the most consequential thing anybody can do to a submission, and it is the
    one state a curator can reach from the verdict form where nothing else would ask who
    they are. The two other lead powers -- clearing another curator's hold, approving a
    correction -- resolve the address the same way, for the same reason.

    **Why "Reject" on the form does not come here.** It lands on ``held``, deliberately, so
    that a rejection gets a second look rather than being terminal on one person's say-so.
    This is the separate, later act: a lead confirming that the objection was never answered
    and the submission is finished. Somebody has to have flagged it first, which the state
    machine enforces -- ``in_review``, ``held`` and ``awaiting_submitter`` can decline, and
    ``approved`` cannot.

    ``reason`` is a closed vocabulary because it reaches ``data/decisions.json``, the one
    committed file whose premise is that it holds no unpublished science.

    ``note`` is free text, kept in the gitignored ledger only, and is where the lead says
    what a reason code cannot.
    """
    curator = resolve(address, registry_path)
    if curator is None or not curator.is_lead:
        raise LedgerError(
            f"{address} is not an active lead curator. Closing a submission is a lead "
            f"action: a rejection already gets a second look by landing on 'held', and "
            f"ending it there should not rest on the same single judgment.")

    if reason not in DECLINE_REASON_CODES:
        raise LedgerError(
            f"reason must be one of {DECLINE_REASON_CODES}, got {reason!r}. The codes "
            f"left out of that list describe other ways a submission ends and would be "
            f"false here.")

    stamp = _validate_stamp(at or now_utc(), "decline timestamp")
    # transition() re-checks the state machine at the write and releases the reserved
    # names. Attributed to the curator id, not the address, like every other actor here.
    transition(entry, "declined", actor=curator.id, at=stamp, reason=reason, config=config)
    if note.strip():
        entry.history.append({"at": stamp, "event": "decline_note",
                              "actor": curator.id, "note": note.strip()})


def record_correction(entry: Entry, address: str, change: str, authority: str,
                     consulted: Iterable[str], consulted_on: str = "",
                     at: Optional[str] = None,
                     registry_path: Optional[Path] = None) -> Correction:
    """Propose a change to a submission. Any curator may; nobody applies it yet.

    Requires a standing objection, because a correction and an acceptance cannot be the
    same act: without a flag, the curator would be approving a version that does not exist
    and the maintainer would be applying a change nobody had reviewed in final form.
    """
    stamp = _validate_stamp(at or now_utc(), "correction timestamp")
    curator = resolve(address, registry_path)
    if curator is None or not curator.active:
        raise LedgerError(f"{address} is not an active curator; cannot record a correction.")
    if authority not in ("author", "curator"):
        raise LedgerError(f"authority must be 'author' or 'curator', got {authority!r}.")
    if not str(change).strip():
        raise LedgerError("a correction must say what should change.")
    if not blocking_holds(entry):
        raise LedgerError(
            "a correction requires a standing flag on the submission. Flag it, describe "
            "the correction, and accept once the corrected report exists — otherwise the "
            "approval would name a version that does not exist yet.")

    correction = Correction(
        id=_next_id(entry.corrections, "C"), by=curator.id, at=stamp,
        authority=authority, consulted=[str(c) for c in consulted],
        consulted_on=consulted_on, change=str(change).strip())
    entry.corrections.append(correction)
    entry.history.append({"at": stamp, "event": "correction_proposed",
                          "actor": curator.id, "correction_id": correction.id})
    return correction


def approve_correction(entry: Entry, correction_id: str, address: str,
                       at: Optional[str] = None,
                       registry_path: Optional[Path] = None) -> Correction:
    """A lead agrees a proposed correction may be applied.

    Lead-only for the same reason clearing another curator's objection is: a correction
    changes what MalAvi will publish about somebody else's study, and one person should not
    be able to describe a change and have it applied without a second view. The lead may not
    approve their own — that would be exactly the single point of judgment the rule exists
    to prevent.
    """
    stamp = _validate_stamp(at or now_utc(), "correction approval timestamp")
    curator = resolve(address, registry_path)
    if curator is None or not curator.is_lead:
        raise LedgerError(
            f"{address} is not an active lead curator. A correction is applied on a lead's "
            f"approval, after discussion with the curator who raised it and, where the data "
            f"itself is changing, with the authors.")

    for correction in entry.corrections:
        if correction.id == correction_id:
            if correction.approved:
                raise LedgerError(f"{correction_id} is already approved by "
                                  f"{correction.approved_by}.")
            if correction.by == curator.id:
                raise LedgerError(
                    f"{correction_id} was proposed by {curator.id}, who cannot also "
                    f"approve it. Another lead must, or another curator must propose it.")
            correction.approved_by = curator.id
            correction.approved_at = stamp
            entry.history.append({"at": stamp, "event": "correction_approved",
                                  "actor": curator.id, "correction_id": correction_id})
            return correction
    raise LedgerError(f"{entry.submission_id} has no correction {correction_id!r}.")


def pending_corrections(entry: Entry) -> List[Correction]:
    """Corrections approved by a lead and not yet turned into a revision."""
    return [c for c in entry.corrections if c.approved and not c.applied]


def bump_revision(entry: Entry, reason: str, at: Optional[str] = None,
                  revised_by: str = "", authority: str = "submitter",
                  consulted: Optional[Iterable[str]] = None,
                  registry_path: Optional[Path] = None) -> Revision:
    """Register a new revision of the submitted content, clearing standing approvals.

    Called whenever normalized scientific content changes: a sequence, a host, a locality,
    an accession, a lineage name, a prevalence figure, the reference, which records are
    present, or a blocking finding. **Not** called when a regeneration changes only
    presentation or a timestamp.

    Approvals do not survive, and that is the point: a curator who approved revision 2 did
    not approve revision 3, however small the difference looks from here. Objections do
    survive, carried by :func:`standing_positions`, until the curator who raised one speaks
    about the new revision, retracts it, or a lead clears it.

    ``revised_by`` is an email address, resolved through the registry exactly as every other
    actor in this module is. It used to be a raw curator id, which meant a caller passing an
    address — the natural mistake, given every neighboring function takes one — made the
    self-approval comparison silently never match, disabling the rule with no error
    anywhere.
    """
    stamp = _validate_stamp(at or now_utc(), "revision timestamp")

    if authority not in AUTHORITIES:
        raise LedgerError(f"Unknown authority {authority!r}; expected one of {AUTHORITIES}.")

    author_id = ""
    if revised_by:
        curator = resolve(revised_by, registry_path)
        if curator is None or not curator.active:
            raise LedgerError(
                f"{revised_by!r} does not resolve to an active curator, so the revision "
                f"would have nobody accountable for it and the self-approval rule would "
                f"not apply to it.")
        author_id = curator.id

    if author_id and authority == "submitter":
        raise LedgerError(
            "A revision typed by a curator cannot claim the submitter's authority; record "
            "whether the authors or another curator licensed the change.")
    if authority in ("author", "curator") and not author_id:
        raise LedgerError(
            f"authority={authority!r} says a curator typed this revision after consulting "
            f"someone, so revised_by must name that curator.")

    entry.revision += 1
    revision = Revision(number=entry.revision, at=stamp, revised_by=author_id,
                        reason=reason, authority=authority,
                        consulted=[str(c) for c in (consulted or [])])
    entry.revisions.append(revision)

    # An approval that was standing is now an approval of something that no longer exists.
    # New content is also the answer an awaiting_submitter was waiting for, so the 60-day
    # clock stops. Since 2026-08-20 the timeout no longer takes the names back, so this is
    # no longer what stands between a day-59 reply and a lost reservation; it still matters,
    # because a submission that has answered should not be reported as still waiting.
    #
    # Both moves are set directly rather than through transition(), because transition
    # would clear approved_at and rerun rules that have nothing to say about a resubmission.
    # They still get a `state` history event: anything reconstructing the timeline from
    # history -- an audit, a later report -- otherwise sees a submission jump from approved
    # to held with no recorded step between, which reads as a lost record rather than a
    # revision.
    previous_state = entry.state
    entry.approved_at = None
    if entry.state in ("approved", "awaiting_submitter"):
        entry.state = "in_review"
    entry.awaiting_since = None

    entry.history.append({"at": stamp, "event": "revision", "revision": entry.revision,
                          "actor": author_id or "submitter", "authority": authority,
                          "reason": reason})
    if entry.state != previous_state:
        entry.history.append({"at": stamp, "event": "state", "from": previous_state,
                              "to": entry.state, "actor": author_id or "submitter",
                              "reason": "reopened"})
    return revision


def _find_verdict(entry: Entry, verdict_id: str) -> Verdict:
    for verdict in entry.verdicts:
        if verdict.id == verdict_id:
            return verdict
    raise LedgerError(f"{entry.submission_id} has no verdict {verdict_id!r}.")


# --------------------------------------------------------------------------------------
# State transitions
# --------------------------------------------------------------------------------------

def transition(entry: Entry, to_state: str, actor: str, at: Optional[str] = None,
               reason: str = "", config: Optional[Dict[str, Any]] = None) -> None:
    """Move a submission to a new state, refusing moves the rules do not allow.

    **Every blocking rule is re-checked here**, at the moment of the write, and that
    includes the move into ``released``. An earlier version checked only ``approved``, which
    left the transition that actually ships data — and which is terminal — with no check at
    all: a hold recorded thirty seconds after the promoter's scan lost to a decision made
    before it. Re-checking at the write is what makes "a hold recorded at any point inside
    the window stops the release" true rather than aspirational.

    Side effects are confined to the clocks and the name lifecycle, which have to move with
    the state or they drift out of agreement with it.
    """
    if to_state not in STATES:
        raise LedgerError(f"Unknown state {to_state!r}.")
    if reason not in DISPOSITION_REASON_CODES:
        raise LedgerError(
            f"reason must be one of {DISPOSITION_REASON_CODES}, got {reason!r}. It is "
            f"exported to the committed decision record, which must contain no unpublished "
            f"science, so it is a closed vocabulary rather than free text.")

    allowed = ALLOWED_TRANSITIONS.get(entry.state, ())
    if to_state not in allowed:
        raise LedgerError(
            f"{entry.submission_id}: {entry.state} -> {to_state} is not an allowed "
            f"transition (allowed: {', '.join(allowed) or 'none — terminal state'}).")

    stamp = _validate_stamp(at or now_utc(), "transition timestamp")

    if to_state in ("approved", "released"):
        approvable, why_not = is_approvable(entry)
        if not approvable:
            raise LedgerError(f"{entry.submission_id} cannot be {to_state}: {why_not}.")

    if to_state == "released":
        # The publish hold is a real precondition of releasing, not a suggestion the
        # promoter is trusted to have honored.
        settings = _review_config(config)
        if not entry.approved_at:
            raise LedgerError(
                f"{entry.submission_id} has no approval timestamp, so the publish hold "
                f"cannot be shown to have elapsed.")
        waited = _parse(stamp) - _parse(entry.approved_at)
        required = timedelta(hours=settings["publish_hold_hours"])
        if waited < required:
            raise LedgerError(
                f"{entry.submission_id} was approved at {entry.approved_at}; the "
                f"{settings['publish_hold_hours']}h publish hold has not elapsed "
                f"({waited} so far). The wait exists so a second curator can still object.")

        # The embargo, re-checked here like every other blocking rule. It was missing
        # until 2026-08-10, which made this function's own promise -- "every blocking rule
        # is re-checked here" -- untrue of the one rule that protects a submitter's
        # unpublished data. release_gate and releasable() both filter embargoed entries,
        # but build_release reads the ledger OUTSIDE the lock and transitions inside a
        # later one, so an embargo set in between was invisible to both. The defense the
        # stale read relies on is exactly this re-check.
        if entry.embargoed:
            raise LedgerError(
                f"{entry.submission_id} is embargoed: the submitter asked that their "
                f"records be held until their study is out. Releasing it would publish "
                f"unpublished data. Lift the embargo first -- publish_reference does it "
                f"when the study appears.")

    previous = entry.state
    entry.state = to_state

    # The 60-day clock starts when we begin waiting, and stops the moment we stop.
    entry.awaiting_since = stamp if to_state == "awaiting_submitter" else None
    # The publish hold starts at approval. Releasing keeps it, because the decision record
    # needs to show what the release was based on.
    if to_state == "approved":
        entry.approved_at = stamp
    elif to_state != "released":
        entry.approved_at = None

    # The name reservation follows the submission. Approving it also adopts whatever
    # corrections were offered: from here the corrected name is the one being held, so the
    # public queue, the release and any later collision check all see the same name.
    if to_state == "approved":
        if entry.name_corrections:
            entry.reserved_names = agreed_names(entry)
        entry.name_state = "held"
    elif to_state == "released":
        entry.name_state = "confirmed"
    elif to_state in ("declined", "withdrawn"):
        entry.name_state = "released"
    # `dormant` is deliberately NOT in that list. A submission goes dormant because its
    # submitter has not answered yet -- very often because a curator asked them to
    # resequence, which is weeks of bench work, not an abandonment. Taking the name back at
    # 60 days would hand NECMON01 to somebody else while the original submitter is doing
    # exactly what we asked; if they had already put that name in a manuscript or a GenBank
    # record, MalAvi would then hold two different sequences under one name, which is the
    # single failure the reservation system exists to prevent. So a dormant submission keeps
    # its claim indefinitely, and a name comes back only when somebody actively declines the
    # submission. Decided with Staffan Bensch, 2026-08-20.

    # Reopening a finished submission has to undo the bookkeeping that finishing it did,
    # or the record contradicts itself: a live submission advertising reserved names while
    # its name_state says they were given back, and a decision record still reporting it as
    # declined months after it returned to review.
    if previous in CLOSED_STATES and to_state in LIVE_STATES:
        entry.name_state = "claimed"
        entry.final_disposition = None
        entry.history.append({
            "at": stamp, "event": "reopened", "actor": actor, "from": previous,
            "note": ("reserved names re-claimed; verify against the reservation store "
                     "before relying on them")})

    if to_state in ("declined", "released", "withdrawn", "dormant"):
        entry.final_disposition = {
            "disposition": to_state,
            "by": actor,
            "at": stamp,
            "revision": entry.revision,
            "reason_code": reason,
        }

    entry.history.append({"at": stamp, "event": "state", "from": previous, "to": to_state,
                          "actor": actor, "reason": reason})


# --------------------------------------------------------------------------------------
# The promoter: what is ripe, never what is decided
# --------------------------------------------------------------------------------------

@dataclass
class DueAction:
    """Something a clock says is ripe. A proposal for a caller to apply, not a decision."""

    submission_id: str
    action: str          # "release_eligible" | "timeout_dormant" | "malformed"
    because: str


def due_actions(entries: Dict[str, Entry], now: Optional[str] = None,
                config: Optional[Dict[str, Any]] = None) -> List[DueAction]:
    """Everything whose clock has run out, as proposals.

    Two clocks, and neither of them decides anything on its own:

    * the **publish hold** — an approved submission becomes release-eligible once it has
      waited, and stops being eligible the moment an objection is recorded, however late in
      the window. That is what makes the wait real rather than ceremonial;
    * the **awaiting-submitter timeout** — 60 days, after which the submission goes dormant.
      Its reserved names are **kept**: dormancy means the submitter has not answered yet,
      not that they have given up, and a name taken back while they are resequencing at our
      request is a name that can end up on two different sequences. Declining is what
      returns a name.

    Returning proposals rather than applying them is the rule that automation never
    resolves a disagreement. A caller applies these through :func:`transition`, which
    re-checks every blocking rule at the moment of application, so an objection recorded
    between the scan and the write still wins.

    A submission with an unreadable timestamp yields a ``malformed`` proposal rather than
    aborting the scan. One bad value used to stop every clock for every submission in the
    ledger until somebody noticed, which is a silent failure of exactly the kind the rest of
    this module is written to avoid.
    """
    settings = _review_config(config)
    moment = _parse(now) if now else datetime.now(timezone.utc)
    hold = timedelta(hours=settings["publish_hold_hours"])
    timeout = timedelta(days=settings["awaiting_submitter_timeout_days"])

    due: List[DueAction] = []
    for submission_id, entry in sorted(entries.items()):
        try:
            if entry.state == "approved" and entry.approved_at:
                waited = moment - _parse(entry.approved_at)
                approvable, why_not = is_approvable(entry)
                # `not entry.embargoed` matches :func:`releasable`, which has always
                # excluded them. Without it the two disagreed, and the operator running
                # promote.py was told an embargoed submission was ready to ship while the
                # release build correctly refused to ship it -- the same rule in two
                # places with a clause missing from one.
                if waited >= hold and approvable and not entry.embargoed:
                    due.append(DueAction(
                        submission_id=submission_id,
                        action="release_eligible",
                        because=(f"approved {entry.approved_at}, publish hold of "
                                 f"{settings['publish_hold_hours']}h elapsed, no standing "
                                 f"objection")))

            # An approval that outlived the objection against it. Clearing the last hold
            # returns a submission to `in_review`; it does not look at what was already
            # approved. So a submission whose only objection a lead has formally cleared,
            # and which still carries a standing approval, sat in `in_review` with no
            # clock on it at all -- the publish hold below only ever starts from
            # `approved`. Nothing would have moved it again, and `stale_live` would have
            # noticed days later, if anyone ran it.
            #
            # Reached the same way by a curator retracting their own hold.
            #
            # Proposed, not applied: `transition` re-checks every blocking rule at the
            # moment of the write, so an objection recorded between this scan and that
            # write still wins. What is not in question here is a disagreement -- the
            # objection has been resolved on the record, by someone entitled to resolve
            # it, and the approval was never withdrawn.
            if entry.state == "in_review" and not entry.embargoed:
                approvable, _why_not = is_approvable(entry)
                standing = approvals(entry)
                if approvable and standing and not blocking_holds(entry):
                    who = ", ".join(sorted({v.curator for v in standing}))
                    due.append(DueAction(
                        submission_id=submission_id,
                        action="approve",
                        because=(f"approved by {who} and no objection stands; the hold "
                                 f"that was blocking it has been resolved")))

            if (entry.state == "awaiting_submitter" and entry.awaiting_since
                    and not entry.embargoed):
                waited = moment - _parse(entry.awaiting_since)
                if waited >= timeout:
                    due.append(DueAction(
                        submission_id=submission_id,
                        action="timeout_dormant",
                        because=(f"waiting on the submitter since {entry.awaiting_since}, "
                                 f"past {settings['awaiting_submitter_timeout_days']} "
                                 f"days; its reserved names are KEPT")))
        except (ValueError, TypeError) as exc:
            due.append(DueAction(submission_id=submission_id, action="malformed",
                                 because=f"unreadable timestamp ({exc}); needs a maintainer"))

    return due


def stale_live(entries: Dict[str, Entry], days: int,
               now: Optional[str] = None) -> List[DueAction]:
    """Live submissions nothing has happened to in ``days`` — for a maintainer to look at.

    The 60-day timeout covers only ``awaiting_submitter``. A submission that is ``held``
    after an objection nobody followed up, or ``screening_failed`` and never revisited, sits
    holding its reserved lineage names with no clock on it at all — which the timeout's own
    rationale says is worse than having no reservation system.

    There is no default threshold on purpose: how long a held submission may sit before
    somebody is nudged is a policy decision, not a fallback for this function to invent, and
    ``days`` is therefore required.
    """
    moment = _parse(now) if now else datetime.now(timezone.utc)
    cutoff = timedelta(days=days)

    stale: List[DueAction] = []
    for submission_id, entry in sorted(entries.items()):
        if entry.state not in LIVE_STATES:
            continue
        stamps = [h.get("at") for h in entry.history if h.get("at")]
        try:
            last = max(_parse(s) for s in stamps) if stamps else _parse(entry.received_at)
        except (ValueError, TypeError):
            continue          # due_actions already reports this one as malformed
        if moment - last >= cutoff:
            stale.append(DueAction(
                submission_id=submission_id, action="stale",
                because=(f"in state {entry.state} with no activity since "
                         f"{last.isoformat()}; reserved names still {entry.name_state}")))
    return stale


# --------------------------------------------------------------------------------------
# The retained record
# --------------------------------------------------------------------------------------

def decision_record(entries: Dict[str, Entry]) -> List[Dict[str, Any]]:
    """The minimal, committable record of what was decided.

    Deliberately not the ledger. The ledger is a working interface: it holds verdict reason
    text, which quotes what was wrong with somebody's unpublished data, and it must be
    erasable when a submitter withdraws. This is the part that survives — identifiers,
    dates, curator ids and closed-vocabulary codes, so that years later "what did we decide
    about that, and when?" is still answerable about a submission whose every other trace
    has been deleted.

    Individual verdicts are kept alongside the final disposition rather than collapsed into
    it. A submission that was approved over an objection and one that was approved
    unanimously are different events, and a single status field cannot tell them apart.

    The override's consultation fields are exported for the same reason. They are the
    evidence that clearing somebody's objection involved talking to them — and if they lived
    only in the erasable copy, the accountability trail would vanish exactly when it was
    needed. They contain curator ids, a date and a medium; the free-text ``note`` stays
    behind.
    """
    records: List[Dict[str, Any]] = []
    for submission_id, entry in sorted(entries.items()):
        if entry.final_disposition is None:
            continue
        records.append({
            "submission_id": submission_id,
            "track": entry.track,
            "received_at": entry.received_at,
            "revision": entry.revision,
            "disposition": entry.final_disposition.get("disposition", ""),
            "decided_by": entry.final_disposition.get("by", ""),
            "decided_at": entry.final_disposition.get("at", ""),
            "reason_code": entry.final_disposition.get("reason_code", ""),
            "release_target": entry.release_target,
            "name_state": entry.name_state,
            # Which names this submission was holding, so "what was released, and when"
            # survives the erasure of the submission itself.
            "reserved_names": sorted(entry.reserved_names),
            # Curator ids and verdict types only. No reason text: it is free prose about
            # somebody's data and belongs with the submission, which is erasable.
            "verdicts": [
                {"curator": v.curator, "verdict": v.verdict, "revision": v.revision,
                 "at": v.at, "resolved": v.resolved}
                for v in entry.verdicts
            ],
            "overrides": [
                {"by": o.by, "at": o.at, "verdict_id": o.verdict_id,
                 "consulted": o.consulted, "consulted_on": o.consulted_on,
                 "consulted_how": o.consulted_how}
                for o in entry.overrides
            ],
        })
    return records


def public_queue(entries: Dict[str, Entry]) -> List[Dict[str, Any]]:
    """What the public queue may show: an opaque id, a date, and how far along it is.

    Which is the settled half of an open question. Showing a name and institution beside
    "in review" is motivating, and it is also a public statement that a named person holds
    unpublished data on a particular parasite. Until that is decided, this shows neither.

    The detailed state is deliberately **coarsened** before it goes out. A reserved lineage
    name encodes the host taxon of unpublished work, so publishing ``held`` beside one would
    announce that curators objected to a specific person's specific claim — considerably
    more than the recorded "name and date only" decision covers. ``open`` and
    ``in_progress`` say enough for a submitter to see their work moving.
    """
    coarse = {
        "received": "open",
        "screening_failed": "open",
        "ready_for_review": "open",
        "in_review": "in_progress",
        "held": "in_progress",
        "awaiting_submitter": "in_progress",
        "approved": "in_progress",
    }
    return [
        {"submission_id": entry.submission_id,
         "status": coarse.get(entry.state, "open"),
         "received_at": entry.received_at,
         "reserved_names": sorted(entry.reserved_names),
         "name_state": entry.name_state}
        for _, entry in sorted(entries.items())
        if entry.state in LIVE_STATES
    ]
