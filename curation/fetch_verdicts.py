#!/usr/bin/env python3
# @title Fetch curator verdicts from the Google Form and apply them to the review ledger
# @purpose Read the verdict responses sheet, turn each response into a ledger action, and
#   record it — so that a curator clicking a link in their email actually moves the
#   submission they were asked about.
# @why Every rule about verdicts, holds, overrides and corrections was written and tested
#   in malavi_curation.ledger, and none of it ran: the responses sat in a sheet nothing
#   read. This is the join between the two.
# @input config/project.yml (review.verdict_sheet, review.verdict_sheet_timezone)
# @input config/curators.yml (who may record a verdict)
# @output curation/intake/submissions/review_ledger.json (via malavi_curation.ledger)
# @output curation/intake/submissions/verdicts_applied.json (which responses are done)
# @program python3
# @critical-var VERDICT_SHEET
# @critical-var APPLIED_LEDGER_NAME
# @critical-flag fetch_verdicts.py "" --dry-run
"""Pull curator verdicts out of the Google Form and record them in the review ledger.

What this program is
--------------------
The return half of the submission loop. ``fetch_submissions.py`` brings work *in*;
this brings the decisions about that work *back*. A curator reads a report, clicks the
prefilled link in it, answers a form, and their answer lands in a spreadsheet. Until this
program runs, that is all that has happened — the spreadsheet is not the ledger, and
nothing in MalAvi has changed.

Three properties it has to have, each for a specific reason
-----------------------------------------------------------
**It never decides anything.** It parses a response into a *request* and hands that request
to :mod:`malavi_curation.ledger`, which re-checks every rule at the moment of the write: is
this address an active curator, does a standing objection block this approval, did this
person author the revision they are trying to approve. If the rules refuse, the refusal is
recorded and the run continues. Moving a rule check into this file would mean the rule is
enforced in whichever caller remembered it.

**It is idempotent.** The sheet is append-only from Google's side and this program may run
on a schedule, by hand, and again after a failure. Recording a verdict twice would not
merely be untidy: two approvals from one curator look like agreement between two people.
Every response is fingerprinted by its content and applied at most once — see
:func:`fingerprint`.

**It never throws a response away.** A response that cannot be parsed, that names an
unknown submission, that comes from an address not in the registry, or that the ledger
refuses — all of them are *filed*, with the reason, in ``verdicts_applied.json``. By the
time anyone reads a log the curator has submitted their decision and gone; a decision that
vanishes because a field was mistyped is worse than one that is visibly stuck.

What a verdict does to a submission's state
-------------------------------------------
Recording the verdict and moving the state are two separate steps here, in that order, and
the first does not depend on the second succeeding. The verdict is the durable thing: it is
the curator's act, and it belongs in the record whether or not the submission happened to
be in a state that could move.

The moves themselves follow ``ops/curator-instructions.src.html``, which is what curators
were actually promised:

* **Accept** — the submission is picked up (``ready_for_review`` → ``in_review`` if it had
  not been already) and then approved, which starts the 24-hour publish hold. The ledger
  refuses the approval outright if an objection stands, which is the rule that makes the
  hold worth having.
* **Flag for further review** — moves to ``held``. Allowed from ``approved`` as well as
  from ``in_review``, and that is the whole point of the publish window: an objection
  recorded at hour 23 of 24 still stops the release.
* **Reject** — *also* moves to ``held``, deliberately, and **not** to the terminal
  ``declined`` state. The curator instructions promise that "the lead curator can still
  overrule the decision after discussion with the curators", and an override acts on a
  standing objection. Ending the submission outright on one curator's say-so would make
  that promise false and would have automation resolving a disagreement, which is the one
  thing this system is built not to do. Declining for good is a deliberate act by a lead,
  not a consequence of a form submission.

Reading the sheet
-----------------
Access is authenticated: the verdict sheet must never be link-shared, because verdict
reason text quotes what was wrong with somebody's unpublished data and curator identities
sit beside it. The service account needs Viewer on it; see ``curation/GOOGLE_ACCESS.md``.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
import urllib.parse
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import (  # noqa: E402
    curators, google_auth, ledger, public_feeds, report_delivery, verdicts,
)
from malavi_curation.config import load_config, repo_root  # noqa: E402

# Which responses have already been acted on. Lives in the gitignored intake tree beside
# the review ledger, for the same reason the ledger does: the rows it fingerprints quote
# unpublished data.
APPLIED_LEDGER_NAME = "verdicts_applied.json"

TIMEOUT = 60


# ======================================================================================
# Reading the responses sheet
# ======================================================================================

def fetch_rows(sheet_id: str, token: str) -> List[Dict[str, str]]:
    """Return the verdict responses sheet as a list of {question: answer} dicts.

    Exported as CSV through the Drive API, which is the documented way to read a private
    Sheet. There is deliberately no unauthenticated fallback here, unlike in
    ``fetch_submissions.py``: that one keeps a link-shared path only so an older sheet can
    still be read, and this sheet must never be link-shared at all.
    """
    url = (f"https://www.googleapis.com/drive/v3/files/{sheet_id}/export"
           f"?mimeType=text%2Fcsv")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "malavi_rebuild/fetch_verdicts",
                 "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        raw = response.read()
    # utf-8-sig: Google prefixes its CSV export with a byte-order mark, which would
    # otherwise become part of the first column's name and stop every lookup on it.
    return read_rows(raw.decode("utf-8-sig"))


def duplicate_columns(header: Sequence[str]) -> Dict[str, int]:
    """Column names that appear more than once, and how often. Empty when all are unique.

    Only names the parser actually reads are reported. The responses sheet accumulates
    orphan columns from every hand edit to the form -- a question that was deleted, or
    replaced rather than renamed, leaves its column behind forever -- and most of them are
    harmless clutter this program never looks at.

    A repeated name is not clutter. ``csv.DictReader`` keeps the LAST column of a repeated
    name, so a stale empty column sitting to the right of a live one silently answers in
    its place, and every read of that field comes back "". Two ways this has already
    happened here: two questions given the same title (see verdicts.COL_CONCLUDED), and a
    question re-created during a hand edit so that its old column survived beside the new
    one. Neither is visible in the form; both are visible here.
    """
    from collections import Counter
    counts = Counter(name for name in header if name)
    reads = verdicts.COLUMNS_READ
    return {name: count for name, count in counts.items() if count > 1 and name in reads}


def read_rows(text: str) -> List[Dict[str, str]]:
    """Parse the exported CSV, refusing a header that would silently misread a column."""
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    repeated = duplicate_columns(header)
    if repeated:
        listing = "\n".join(
            f"    {name!r} appears {count} times" for name, count in sorted(repeated.items()))
        raise DuplicateColumns(
            f"the responses sheet has repeated column headers that this program reads:\n"
            f"{listing}\n"
            f"Only the RIGHTMOST of each is read, so if it is an orphan left behind by a "
            f"hand edit, every answer to that question arrives empty and nothing else "
            f"complains. The sheet is the place to fix it: open it, check which column the "
            f"live form actually fills, and delete the others. Verify afterwards by "
            f"submitting one response and re-reading the header.")
    return list(reader)


class DuplicateColumns(RuntimeError):
    """Raised when the responses sheet would misread a column the parser depends on."""


def fingerprint(row: Dict[str, Any]) -> str:
    """A stable identity for one response, derived from its content.

    Row *position* is not usable as an identity: Google's export order is not guaranteed to
    be stable across edits, and a curator (or a maintainer) editing an earlier row would
    shift every fingerprint after it, causing every later response to be applied a second
    time.

    Content is. Two responses that agree on every field including the timestamp are the
    same response — Forms stamps to the second, and one curator submitting the same form
    twice within one second is not a case worth distinguishing from a duplicated export.
    """
    canonical = json.dumps({str(k): ("" if v is None else str(v))
                            for k, v in row.items()},
                           sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_applied(path: Path) -> Dict[str, Dict[str, Any]]:
    """Which responses this program has already dealt with, keyed by fingerprint."""
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # A corrupt applied-ledger must stop the run. Treating it as empty would re-apply
        # every response in the sheet, which is exactly the duplication this file prevents.
        raise SystemExit(f"{path} could not be read ({exc}). Fix or move it; continuing "
                         f"would re-apply every response in the sheet.")
    return loaded.get("responses", {}) if isinstance(loaded, dict) else {}


def save_applied(path: Path, applied: Dict[str, Dict[str, Any]]) -> None:
    """Write the applied-ledger, whole, via a temporary file in the same directory.

    **Flushed and fsynced, like ledger.save.** This file is the only thing standing
    between a re-run and every response in the sheet being applied twice, and it was
    written without either until 2026-08-10 -- so ``os.replace`` could rename an inode
    holding zero bytes after a power loss, and the next run would read an empty
    applied-ledger and re-apply everything. The review ledger it partners has been careful
    about exactly this since it was written; a durability rule applied rigorously to one
    file and not to its companion protects neither.
    """
    payload = {"schema": 1, "updated": ledger.now_utc(), "responses": applied}
    temporary = path.with_suffix(".tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

    # Persist the rename itself, not just the data.
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    except OSError:
        pass          # some network filesystems refuse this; the data is already safe
    finally:
        os.close(directory)


# ======================================================================================
# Applying one response
# ======================================================================================

def _advance_after_verdict(entry: ledger.Entry, verdict: str, actor: str,
                           at: str, config: Dict[str, Any],
                           entries: Optional[Dict[str, ledger.Entry]] = None
                           ) -> Tuple[str, str]:
    """Move the submission's state to match a verdict just recorded.

    Returns ``(moved_to, why_not)`` — exactly one of which is non-empty. Never raises:
    the verdict is already in the ledger by the time this is called, and a state that
    could not move is a thing to report, not a reason to lose the verdict.

    ``entries`` is the whole ledger, handed to ``transition`` so that an approval is
    refused when another submission already holds one of this one's names.

    See this module's docstring for why "Reject" lands on ``held`` rather than ``declined``.
    """
    moved: List[str] = []
    refusal: List[str] = []

    def attempt(to_state: str) -> bool:
        """Try one transition; report failure rather than raising."""
        try:
            ledger.transition(entry, to_state, actor=actor, at=at, config=config,
                              entries=entries)
        except ledger.LedgerError as exc:
            refusal.append(str(exc))
            return False
        moved.append(to_state)
        return True

    # Any verdict means a curator has picked the submission up. This is a no-op for a
    # submission already in review, and it is what lets the first verdict on a freshly
    # reported submission land without a separate "claim it" step no interface offers.
    if entry.state == "ready_for_review":
        attempt("in_review")

    if verdict == "approve":
        # A held submission becomes approvable again once the objection that held it was
        # retracted or overridden. Nothing else notices that, so the next approval is what
        # brings it back into review.
        if entry.state == "held" and ledger.is_approvable(entry)[0]:
            attempt("in_review")
        if entry.state == "in_review":
            # transition() re-checks is_approvable at the write, so a hold recorded between
            # the parse and here still wins.
            if not attempt("approved"):
                _, why_not = ledger.is_approvable(entry)
                # is_approvable knows about objections; it does not know about a name
                # another submission holds. When that is what stopped the approval, the
                # ledger's own message -- which names the other submission -- is the one
                # to report, or the outcome says "applied" about an approval that was
                # refused for a reason nobody can see.
                return "", (why_not or (refusal[-1] if refusal else "")
                            or f"cannot approve from state {entry.state!r}")
        else:
            # An approval that changed nothing is the single most misleading outcome this
            # program can produce: the curator believes they approved it. Say which rule
            # stopped it, not merely that the state did not move -- "no state change from
            # 'held'" sends a maintainer looking at the state machine when the answer is
            # that somebody has an objection standing.
            approvable, why_not = ledger.is_approvable(entry)
            if not approvable:
                return "", why_not
    elif verdict in ledger.BLOCKING_VERDICTS:
        # `approved` -> `held` is the late objection inside the 24-hour publish window,
        # and is the reason that window exists.
        if entry.state in ("in_review", "approved"):
            attempt("held")

    if moved:
        return " -> ".join(moved), ""
    return "", f"no state change from {entry.state!r}"


def _who(curator_id: str, fallback_address: str = "") -> str:
    """A curator as a person, not as a database key.

    The ledger stores ids -- "vellis", "sari" -- because an id is stable when a name or
    an address changes. An email addressed to curators should not show them: nobody
    signed up as "sari". The registry has the name and the address, and this reads them
    out, falling back to the address and then to the id if the registry cannot say.
    """
    try:
        registry = curators.load_registry()
    except Exception:                                          # noqa: BLE001
        registry = {}
    person = registry.get(curator_id)
    if person is not None:
        name = (getattr(person, "name", "") or "").strip()
        address = (getattr(person, "email", "") or "").strip()
        if name and address:
            return f"{name} ({address})"
        return name or address or curator_id
    return fallback_address or curator_id


def _prefilled_verdict_link(config: Dict[str, Any], submission_id: str,
                            revision: Any, hold_id: str = "") -> str:
    """The verdict form with the submission id and revision already in it.

    A curator answering a notification should not have to copy an identifier out of an
    email; that is the step that gets mistyped, and a mistyped id files a verdict against
    a submission that does not exist. The entry ids are pinned in config/project.yml
    because Google re-mints them whenever a question is deleted and re-created.

    ``hold_id`` fills the override page's "Which hold are you clearing?" as well, so a
    lead answering a flag has nothing at all to type. Read off the live form 2026-08-20;
    the ids move if a question is ever deleted and re-created rather than renamed.
    """
    review = config.get("review") or {}
    url = str(review.get("verdict_form_url") or "")
    entries = review.get("verdict_form_entries") or {}
    if not url or not entries.get("submission_id"):
        return url
    fields = {f"entry.{entries['submission_id']}": submission_id}
    if revision not in (None, "") and entries.get("revision"):
        fields[f"entry.{entries['revision']}"] = str(revision)
    if hold_id and entries.get("hold_id"):
        fields[f"entry.{entries['hold_id']}"] = str(hold_id)
    joiner = "&" if "?" in url else "?usp=pp_url&"
    return url + joiner + urllib.parse.urlencode(fields)


def _verdict_notice(outcome: Dict[str, Any], form_url: str,
                    entries: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """The subject and body telling the other curators what just happened.

    Returns None for anything not worth an email -- a response that could not be parsed,
    or one filed for a maintainer, both of which are the maintainer's problem and not a
    curator's.

    Why the maintainer's side composes this: the verdict id, whose objection was set
    aside, the state the submission landed in and the link that answers it all live in
    the ledger, which the Apps Script cannot see.
    """
    if outcome.get("status") != "applied":
        return None
    submission = str(outcome.get("submission") or "")
    who = _who(str(outcome.get("curator") or ""),
               str(outcome.get("address") or ""))
    reason = str(outcome.get("reason_text") or "").strip()
    state = str(outcome.get("state") or "")
    kind = outcome.get("kind")

    lines = ["[this is an automatic email]", ""]

    if kind == "verdict":
        verdict = str(outcome.get("verdict") or "")
        word = {"approve": "accepted", "hold": "flagged for further review",
                "reject": "rejected"}.get(verdict, verdict)
        subject = f"MalAvi: {submission} {word} by {who}"
        lines += [f"{who} has {word} submission {submission}.",
                  f"This is verdict {outcome.get('verdict_id', '')}."]
        if reason:
            lines += ["", "They wrote:", f"    {reason}"]
        if verdict == "hold":
            lines += ["",
                      "A flag outranks approvals: the submission cannot proceed while it "
                      "stands. It is answered by the curator who raised it withdrawing "
                      "it, or by a lead curator clearing it."]
    elif kind == "override":
        overridden = (_who(str(outcome.get("overridden") or ""))
                      if outcome.get("overridden") else "another curator")
        subject = f"MalAvi: {who} cleared a hold on {submission}"
        lines += [f"{who}, as a lead curator, has cleared {overridden}'s hold "
                  f"({outcome.get('hold_id', '')}) on submission {submission}.",
                  "",
                  "Clearing a hold sets aside the objection so the submission can move "
                  "again. It is recorded permanently, with who was consulted."]
        if reason:
            lines += ["", "They wrote:", f"    {reason}"]
    elif kind == "retraction":
        subject = f"MalAvi: {who} withdrew a flag on {submission}"
        lines += [f"{who} has withdrawn their own flag on submission {submission}."]
    else:
        return None

    lines += ["", f"The submission is now: {state}.", ""]
    if form_url:
        filled = ("submission id, revision and the hold id are all already filled in"
                  if (kind == "verdict" and outcome.get("verdict") == "hold")
                  else "submission id and revision are already filled in")
        lines += [f"To record your own verdict, or to answer this one — the {filled}:",
                  f"    {form_url}"]
        if kind == "verdict" and outcome.get("verdict") == "hold":
            lines += [f"That link is set to clear this hold ({outcome.get('verdict_id', '')}) "
                      f"if that is what you decide; choose \"Clear another curator's "
                      f"hold\" and the id is already in it."]
    lines += ["", "--",
              "Confidential: a submission can contain unpublished sequences."]
    return subject, "\n".join(lines)


def apply_action(entries: Dict[str, ledger.Entry], action: verdicts.Action,
                 config: Dict[str, Any],
                 registry_path: Optional[Path] = None) -> Dict[str, str]:
    """Apply one parsed response to the ledger.

    Returns a small record for the applied-ledger: what happened and, when nothing did,
    why. Every failure mode below is a *filed* outcome rather than an exception, because
    one unusable response must not stop the fifty good ones behind it.

    ``registry_path`` overrides which curator registry authorizes the responder. It exists
    so the tests can express a scenario with two curators and a lead without editing
    MalAvi's real registry; in production it is None and the configured registry is used.
    """
    entry = entries.get(action.submission_id)
    if entry is None:
        # Deliberately not created. A verdict is the only thing that would be creating it,
        # and a submission that exists only because somebody recorded a verdict about it is
        # a mistyped identifier, not a submission.
        return {"status": "unknown_submission", "submission": action.submission_id,
                "detail": ("no such submission in the review ledger; the id was probably "
                           "typed rather than carried by a prefilled link")}

    base = {"submission": action.submission_id, "kind": action.kind,
            "address": action.address, "at": action.at,
            "revision": getattr(action, "revision", None)}

    try:
        if action.kind == "verdict":
            stored = ledger.record_verdict(
                entry, action.address, action.verdict,
                reason_text=action.reason_text, at=action.at, revision=action.revision,
                registry_path=registry_path)
            if stored is None:
                # record_verdict files unrecognized addresses and unreadable revisions on
                # the entry itself and returns None. It is recorded, not acted on.
                return {**base, "status": "filed_unrecognized",
                        "detail": "not attributable to an active curator; see "
                                  "entry.unrecognized in the review ledger"}
            moved, why_not = _advance_after_verdict(
                entry, action.verdict, actor=stored.curator, at=action.at, config=config,
                entries=entries)
            return {**base, "status": "applied", "verdict": action.verdict,
                    "verdict_id": stored.id, "state": entry.state,
                    "curator": stored.curator,
                    "reason_text": action.reason_text or "",
                    "detail": f"state {moved}" if moved else f"verdict recorded; {why_not}"}

        if action.kind == "override":
            record = ledger.override_hold(
                entry, action.hold_id, action.address, consulted=action.consulted,
                consulted_on=action.consulted_on, consulted_how=action.consulted_how,
                note=action.reason_text, at=action.at, registry_path=registry_path)
            # Clearing the last standing objection is what lets a held submission move
            # again. It goes back to review rather than straight to approved: the override
            # removed an obstacle, it did not express approval.
            if entry.state == "held" and not ledger.blocking_holds(entry):
                try:
                    ledger.transition(entry, "in_review", actor=record.by, at=action.at,
                                      config=config)
                except ledger.LedgerError:
                    pass
            overridden = next((v.curator for v in entry.verdicts
                               if v.id == action.hold_id), "")
            return {**base, "status": "applied", "hold_id": action.hold_id,
                    "state": entry.state, "curator": record.by,
                    "overridden": overridden,
                    "reason_text": action.reason_text or "",
                    "detail": f"hold {action.hold_id} cleared by "
                              f"{record.by}"}

        if action.kind == "retraction":
            record = ledger.retract_verdict(entry, action.target_id, action.address,
                                            at=action.at, registry_path=registry_path)
            if entry.state == "held" and not ledger.blocking_holds(entry):
                try:
                    ledger.transition(entry, "in_review", actor=record.curator,
                                      at=action.at, config=config)
                except ledger.LedgerError:
                    pass
            return {**base, "status": "applied", "hold_id": action.target_id,
                    "state": entry.state,
                    "detail": f"{action.target_id} withdrawn by {record.curator}"}

        if action.kind == "correction":
            correction = ledger.record_correction(
                entry, action.address, change=action.change, authority=action.authority,
                consulted=action.consulted, consulted_on=action.consulted_on,
                at=action.at, registry_path=registry_path)
            return {**base, "status": "applied", "correction_id": correction.id,
                    "state": entry.state,
                    "detail": "proposed; a lead must approve it before it is applied"}

        if action.kind == "correction_approval":
            # The consultation the form required of the lead goes through to the ledger.
            # The approval page asks who was consulted and what was concluded, but not
            # when, so consulted_on is left empty here; the response timestamp is the
            # nearest thing to a date and is already recorded as `at`.
            correction = ledger.approve_correction(
                entry, action.target_id, action.address, at=action.at,
                registry_path=registry_path, consulted=action.consulted,
                note=action.reason_text)
            return {**base, "status": "applied", "correction_id": correction.id,
                    "state": entry.state,
                    "detail": f"approved by {correction.approved_by}; the maintainer "
                              f"applies it and the report is regenerated"}

        if action.kind == "close":
            # The only action here that ends a submission. ledger.decline checks that the
            # responder is a lead and that the state machine allows the move -- an approved
            # submission cannot be closed until somebody flags it, which is the rule that
            # keeps a decline following an objection rather than replacing one.
            names = list(entry.reserved_names)
            ledger.decline(entry, action.address, reason=action.reason_code,
                           note=action.reason_text, at=action.at, config=config,
                           registry_path=registry_path)
            return {**base, "status": "applied", "state": entry.state,
                    "detail": f"closed as {action.reason_code}; reserved names released: "
                              f"{', '.join(sorted(names)) or 'none'}. notify_submitters "
                              f"sends the decline notice once the publish hold has run"}

        return {**base, "status": "error", "detail": f"unknown action kind {action.kind!r}"}

    except ledger.LedgerError as exc:
        # A rule refused it. This is the system working, not a fault: the refusal is the
        # record of why nothing happened, and it is what a maintainer needs to see.
        return {**base, "status": "refused", "detail": str(exc)}


# ======================================================================================
# The run
# ======================================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and report; write nothing to either ledger")
    parser.add_argument("--sheet", default=None,
                        help="override the sheet id from config/project.yml")
    parser.add_argument("--from-csv", default=None, type=Path,
                        help="read responses from a local CSV instead of Google, for "
                             "testing the applier without a credential")
    parser.add_argument("--no-publish", action="store_true",
                        help="do not rebuild and publish the public queue afterwards")
    arguments = parser.parse_args(argv)

    config = load_config()
    review = config.get("review") or {}
    sheet_id = arguments.sheet or review.get("verdict_sheet")

    root = repo_root()
    inbox = root / (config.get("submissions", {}) or {}).get(
        "inbox_dir", "curation/intake/submissions")
    applied_path = inbox / APPLIED_LEDGER_NAME

    print("== malavi_rebuild :: fetch_verdicts ==")

    # The sheet's timezone is load-bearing, not cosmetic: Google records a response time
    # with no offset, and that time drives the 24-hour publish hold and the 60-day timeout.
    zone_name = str(review.get("verdict_sheet_timezone", "UTC")).strip().upper()
    if zone_name not in ("UTC", "GMT"):
        print(f"\nconfig says the verdict sheet is set to {zone_name!r}, but this program\n"
              f"can only read a sheet set to UTC. A response time with the wrong zone is\n"
              f"wrong by up to a day, and that lands on the publish hold.", file=sys.stderr)
        return 1
    sheet_timezone = timezone.utc

    # --- get the rows ---------------------------------------------------------------
    if arguments.from_csv is not None:
        # Through read_rows, exactly as the live sheet is, so a saved export with a
        # repeated header is refused here too. Until 2026-09-02 this path used
        # csv.DictReader directly and would have quietly read the rightmost column.
        try:
            rows = read_rows(arguments.from_csv.read_text(encoding="utf-8-sig"))
        except DuplicateColumns as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        print(f"responses: {len(rows)} row(s) from {arguments.from_csv}")
    else:
        if not sheet_id:
            print("config/project.yml has no review.verdict_sheet", file=sys.stderr)
            return 1
        # Say which identity is being used before reading anything. A run that finds
        # nothing because it could not authenticate looks exactly like a quiet week.
        print(google_auth.describe())
        try:
            token = google_auth.access_token()
        except google_auth.CredentialError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        if token is None:
            print("\nNo Google credential configured. The verdict sheet is not, and must\n"
                  "never be, link-shared — it quotes unpublished data and names curators.\n"
                  "Set up read-only access first: curation/GOOGLE_ACCESS.md.", file=sys.stderr)
            return 1
        try:
            rows = fetch_rows(sheet_id, token)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                print(f"\nDrive refused the verdict sheet ({exc.code}). Share it as Viewer "
                      f"with:\n    {google_auth.service_account_email() or 'unknown address'}",
                      file=sys.stderr)
                return 1
            raise
        except DuplicateColumns as exc:
            # A stack trace here would bury the one thing worth reading. Nothing has been
            # parsed and nothing recorded, so there is no partial state to explain.
            print(f"\n{exc}", file=sys.stderr)
            return 1
        print(f"verdict sheet: {len(rows)} row(s)\n")

    applied = load_applied(applied_path)

    # --- parse first, outside the lock ----------------------------------------------
    # Parsing touches no shared state, and doing it before taking the ledger lock keeps
    # the lock held for as short a time as possible.
    pending: List[Tuple[str, Any, Dict[str, Any]]] = []
    already = 0
    for row in rows:
        key = fingerprint(row)
        if key in applied:
            already += 1
            continue
        pending.append((key, verdicts.parse_row(row, sheet_timezone), row))

    print(f"{already} response(s) already applied, {len(pending)} new\n")
    if not pending:
        return 0

    # --- apply, under the ledger lock ------------------------------------------------
    #
    # The applied-ledger is written INSIDE this block, before the lock is released. It was
    # written after it until 2026-08-10, which left a window where the review ledger had
    # the verdicts and the applied-ledger did not: a failed write there -- permissions, a
    # full disk, an NFS hiccup -- meant the next run re-applied every response. Duplicate
    # Verdict rows survive that (current_verdicts takes the latest), but override_hold and
    # retract_verdict refuse with "already resolved" and record_correction files the
    # correction a second time.
    results: Dict[str, Dict[str, Any]] = {}
    with ledger.open_ledger(inbox, write=not arguments.dry_run) as entries:
        for key, parsed, row in pending:
            if not parsed.ok:
                # A Rejected. Filed with its reason so a maintainer can go and look at the
                # sheet row; never re-tried, because re-parsing it would fail identically.
                outcome = {"status": "unparseable", "detail": parsed.reason}
            else:
                outcome = apply_action(entries, parsed, config)
            outcome["at_run"] = ledger.now_utc()
            results[key] = outcome

            label = outcome.get("submission", "-")
            print(f"  [{outcome['status']:<20}] {label}  {outcome.get('detail', '')}")

        # Still holding the lock. open_ledger saves the review ledger on a clean exit, so
        # if this raises, the body raises, and the review ledger is not written either --
        # the two files fail together and the run is simply repeatable.
        if not arguments.dry_run:
            applied.update(results)
            save_applied(applied_path, applied)

    if arguments.dry_run:
        print("\n[dry-run] nothing was written to the review ledger or the applied ledger.")
        return 0

    # --- tell the other curators -----------------------------------------------------
    #
    # After the lock is released and both ledgers are on disk, never before. A verdict
    # that has been recorded but not announced is recoverable -- run this again, or say
    # it in person. A verdict announced but not recorded is not.
    #
    # Each send is guarded on its own. One address bouncing must not cost the other
    # curators their notification, and no delivery failure may reach the caller as a
    # failure of the fetch, which has already succeeded by this point.
    for outcome in results.values():
        composed = _verdict_notice(
            outcome,
            _prefilled_verdict_link(
                config, str(outcome.get("submission") or ""), outcome.get("revision"),
                # Only for a hold: the link then answers the very thing being announced.
                hold_id=(str(outcome.get("verdict_id") or "")
                         if outcome.get("verdict") == "hold" else "")),
            {})
        if not composed:
            continue
        subject, body = composed
        try:
            delivered = report_delivery.deliver_verdict_notice(
                str(outcome.get("submission") or ""), subject=subject, body=body,
                actor_email=str(outcome.get("address") or ""))
            print(f"  [notified] {outcome.get('submission')}  "
                  f"{delivered.notified} curator(s)")
        except Exception as exc:                                   # noqa: BLE001
            print(f"  [notify failed] {outcome.get('submission')}  {exc}",
                  file=sys.stderr)

    # --- summary ---------------------------------------------------------------------
    counts: Dict[str, int] = {}
    for outcome in results.values():
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
    print("\n" + ", ".join(f"{count} {status}" for status, count in sorted(counts.items())))

    # --- the public queue ------------------------------------------------------------
    # A verdict changes what the site should say, so the site is brought up to date here
    # rather than waiting for someone to remember. Nothing about this can fail the run:
    # the verdicts are already written to the ledger, which is the record.
    #
    # Note that an approval publishes no visible change until its publish hold elapses --
    # see build_site_feeds.public_review_state -- so the normal result of this call right
    # after an approval is "the published queue was already current". That is correct.
    if counts.get("applied") and not arguments.dry_run and not arguments.no_publish:
        print("")
        public_feeds.refresh()

    # Anything that is not a clean application wants a person to look at it. Exit 2 rather
    # than 1 so a scheduled job can distinguish "responses need attention" from "the job
    # could not run" -- the same convention check_template.py uses.
    needs_attention = sum(count for status, count in counts.items() if status != "applied")
    if needs_attention:
        print(f"{needs_attention} response(s) need a maintainer's attention.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
