#!/usr/bin/env python3
# @title Tell submitters what a curator decided about their submission
# @purpose Find submissions a curator has settled -- approved or declined -- whose hold has
#   elapsed, email the submitter the outcome, and record that it was sent.
# @why The site tells every visitor that a curator will confirm their proposed name before
#   they deposit in GenBank. Until now that email existed only if a person remembered to
#   write it, and a submitter who gives up waiting deposits the wrong name.
# @input curation/intake/submissions/review_ledger.json
# @input curation/intake/submissions/<id>/submission.json (the submitter's address)
# @output an email to the submitter, via the Apps Script endpoint
# @output a name_confirmation_sent entry in the submission's ledger history
# @program python3
# @critical-var CONFIRMATION_EVENT
# @critical-var DECLINE_EVENT
# @critical-flag notify_submitters.py "" --dry-run
"""Tell a submitter what happened to their submission.

What decides that a submission is ready
---------------------------------------
Three things, all of them:

1. the ledger says **approved**;
2. the **publish hold has elapsed** (``review.publish_hold_hours``, 24 by default);
3. **no blocking verdict stands** — a hold recorded late in the window still wins.

Why not sooner, which is the whole design question
--------------------------------------------------
Screening cannot send it. A proposed name is derived from the host species, and the host
species is the single thing curators most often correct; a name confirmed by machine could
be withdrawn by a person the next day.

Approval alone cannot send it either, and this is the less obvious half. The publish hold
exists so that a second curator can still object *after* an approval. A confirmation posted
into that window can be contradicted — and by then the submitter may have put the name in a
manuscript. A slow email is recoverable. A retracted lineage name is not.

So the trigger is the same moment the submission becomes genuinely settled, which is the
moment ``promote.py`` would call it release-eligible.

Sent once, and only once
------------------------
Every send writes a ``name_confirmation_sent`` event into the submission's ledger history,
and a submission carrying one is skipped forever after. Re-sending would be worse than it
sounds: a second "your names are confirmed" invites the reader to wonder which message was
right, and to go looking for a difference that is not there.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from datetime import datetime, timedelta, timezone                     # noqa: E402

from malavi_curation import ledger                                     # noqa: E402
from malavi_curation.config import load_config, repo_root              # noqa: E402
from malavi_curation.report_delivery import (                          # noqa: E402
    DeliveryError, deliver_decline_notice, deliver_name_confirmation, describe,
)

CONFIRMATION_EVENT = "name_confirmation_sent"
DECLINE_EVENT = "decline_notice_sent"

# Which ledger state produces which message, and which history event records it. Adding a
# third outcome should mean adding a row here, not another branch somewhere.
OUTCOMES = {
    "approved": CONFIRMATION_EVENT,
    "declined": DECLINE_EVENT,
}


def submissions_inbox() -> Path:
    config = load_config()
    return repo_root() / (config.get("submissions", {}) or {}).get(
        "inbox_dir", "curation/intake/submissions")


def already_sent(entry, event: str) -> Optional[str]:
    """When this particular message went out, or None. The guard against sending twice.

    Keyed by event, so an approval notice and a decline notice are tracked separately: a
    submission that was declined, reopened and then approved should receive the
    confirmation it never got, and must not be blocked by the decline it did.
    """
    for record in reversed(entry.history or []):
        if record.get("event") == event:
            return record.get("at")
    return None


def settled(entry, config: Dict, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """Is this submission finished enough to tell the submitter? With the reason if not.

    Deliberately re-derived here rather than read off a flag, for the same reason
    ``transition()`` re-checks its rules at the moment of the write: the state of the
    ledger when a scan started is not the state when the message goes out.

    The **same wait applies to a decline as to an approval**, which is worth saying out
    loud because it is a choice. The hold exists so a second curator can still object to an
    approval; applied to a decline it does the plainer job of giving anyone a window to
    undo a mis-click before a stranger is told unwelcome news. One concept, one knob, both
    directions.
    """
    if entry.state not in OUTCOMES:
        return False, f"state is {entry.state}; nothing to tell the submitter yet"

    if entry.state == "approved" and ledger.blocking_holds(entry):
        return False, "a blocking verdict stands"

    stamp = entry.approved_at if entry.state == "approved" else _closed_at(entry)
    if not stamp:
        return False, f"{entry.state} but carries no timestamp for when that happened"

    hours = ((config.get("review") or {}).get("publish_hold_hours", 24))
    moment = now or datetime.now(timezone.utc)
    waited = moment - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if waited < timedelta(hours=hours):
        remaining = timedelta(hours=hours) - waited
        return False, f"hold has {remaining} left to run"

    return True, ""


def _closed_at(entry) -> str:
    """When the submission was declined, read from its history.

    There is no dedicated field for it the way there is for approval, so the history is
    the record. Falling back to an empty string rather than to "now" matters: treating a
    missing timestamp as this instant would send the message immediately, which is exactly
    what the wait exists to prevent.
    """
    for record in reversed(entry.history or []):
        if record.get("to") == "declined" or record.get("state") == "declined":
            return record.get("at", "")
    return ""


def submitter_of(inbox: Path, submission_id: str) -> Tuple[str, str, str]:
    """The submitter's address, their name, and a short reference, from submission.json.

    A missing address is an error rather than a skip. It means a submission was approved
    that we cannot answer, and that is something a maintainer has to see.
    """
    path = inbox / submission_id / "submission.json"
    if not path.is_file():
        raise DeliveryError(
            f"{submission_id}: no submission.json, so there is no submitter address to "
            f"send to. Has check_template.py run on it?")
    data = json.loads(path.read_text(encoding="utf-8"))
    submitter = data.get("submitter") or {}
    reference = data.get("reference") or {}
    label = " ".join(str(reference.get(k, "")) for k in ("title", "year")).strip()
    return submitter.get("email", ""), submitter.get("name", ""), label


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission_id", nargs="*",
                        help="only consider these; default is every eligible submission")
    parser.add_argument("--dry-run", action="store_true",
                        help="show who would be written to and what they would be told; "
                             "send nothing and record nothing")
    parser.add_argument("--check", action="store_true",
                        help="report whether delivery is configured, and stop")
    arguments = parser.parse_args(argv)

    print("== malavi_rebuild :: notify submitters ==")
    print(describe())
    if arguments.check:
        return 0

    config = load_config()
    inbox = submissions_inbox()
    sent = skipped = 0
    named = bool(arguments.submission_id)   # explain skips only when asked about one

    try:
        with ledger.open_ledger(inbox, write=not arguments.dry_run) as entries:
            for submission_id in (arguments.submission_id or sorted(entries)):
                entry = entries.get(submission_id)
                if entry is None:
                    print(f"  {submission_id}: not in the ledger")
                    continue

                event = OUTCOMES.get(entry.state)
                if event is None:
                    skipped += 1
                    if named:
                        print(f"  {submission_id}: state is {entry.state}; nothing to "
                              f"tell the submitter yet")
                    continue

                when = already_sent(entry, event)
                if when:
                    skipped += 1
                    if named:
                        print(f"  {submission_id}: already told, on {when}")
                    continue

                ready, why_not = settled(entry, config)
                if not ready:
                    skipped += 1
                    if named:
                        print(f"  {submission_id}: not ready -- {why_not}")
                    continue

                email, who, reference = submitter_of(inbox, submission_id)
                print(f"  {submission_id}  [{entry.state}]")
                print(f"    to     : {email or '(none)'}")

                if entry.state == "approved":
                    names = ledger.agreed_names(entry)
                    if not names:
                        # A records-only submission claims no names. "Your names are
                        # confirmed" listing nothing would read as a mistake.
                        print("    (approved but claims no names -- records-only "
                              "submission, nothing to confirm)")
                        skipped += 1
                        continue
                    changed = {p: g for p, g in (entry.name_corrections or {}).items()
                               if p != g}
                    print(f"    names  : {', '.join(names)}")
                    for proposed, granted in sorted(changed.items()):
                        print(f"    changed: {proposed} -> {granted}")
                    if arguments.dry_run:
                        print("    [dry-run] nothing sent")
                        continue
                    result = deliver_name_confirmation(
                        submission_id, to=email, submitter_name=who, names=names,
                        corrections=changed, reference=reference)
                    record = {"event": event, "at": ledger.now_utc(), "names": names,
                              "corrections": changed, "to": email}
                else:
                    print("    message: not accepted in its current form; invites a reply")
                    if arguments.dry_run:
                        print("    [dry-run] nothing sent")
                        continue
                    result = deliver_decline_notice(
                        submission_id, to=email, submitter_name=who, reference=reference)
                    record = {"event": event, "at": ledger.now_utc(), "to": email}

                print(f"    sent to {result.notified} address(es)")
                entry.history.append(record)
                sent += 1

    except DeliveryError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print(f"\nnotified {sent} submitter(s); {skipped} not due.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
