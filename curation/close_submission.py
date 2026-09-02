#!/usr/bin/env python3
# @title Close a submission — decline it, withdraw it, wait on its submitter, or reopen it
# @purpose Produce the ledger states nothing else could reach: declined, withdrawn and
#   awaiting_submitter, so that a rejected submission actually finishes and a submitter
#   waiting on us is recorded as such -- and reopen a finished one when they come back.
# @why Every rule downstream of a closed submission was written and tested and none of it
#   ran, because no program moved a submission into any of these states. A rejection got a
#   second look and then sat in `held` forever: the reserved names were never given back,
#   the 60-day timeout never started, and the submitter was never told anything.
# @input curation/intake/submissions/review_ledger.json (via malavi_curation.ledger)
# @output the same ledger, with the submission closed and its disposition recorded
# @program python3
# @critical-var DECLINE_REASONS
# @critical-var WITHDRAW_REASON
# @critical-flag close_submission.py "" --apply
"""Finish a submission — or revive one: decline, withdraw, wait on the submitter, reopen.

Why this is a maintainer program
--------------------------------
Curators decide; they do not run programs. Their interface is the verdict form, and it
already offers **Reject** — which lands the submission on ``held``, deliberately, so that a
rejection gets a second look rather than being terminal on one person's say-so. What was
missing was the second act: somebody actually closing it afterwards.

**A lead can now do that from the form** — "Close a submission for good (lead curators
only)", added 2026-08-13, which goes through :func:`ledger.decline`. So this program is no
longer the only route to ``declined``, and for a curator's own judgment the form is the
right one.

What stays here:

* ``withdrawn`` — the submitter took it back. It arrives as an email to the maintainer, so
  there is nobody on the form to record it.
* ``awaiting_submitter`` — the record that we asked them something and are waiting.
* ``declined`` **on a lead's instruction**, when the lead is not at a browser. The form
  resolves the lead through their verified Google address; here the maintainer is acting on
  their behalf and ``--actor`` records who typed it. Both routes are legitimate and the
  ledger tells them apart, because the form's route is attributed to the lead's curator id
  and this one to whatever ``--actor`` says.

What each of the three means
----------------------------
``--decline``
    We will not include this submission. Terminal, though reopening it is allowed and is
    logged as a deliberate act. The submitter is told — see below.

``--withdraw``
    The submitter took it back. Terminal with no way back, because the record of a
    submission somebody withdrew should not be revivable by us.

``--ask``
    We have asked the submitter a question and cannot proceed until they answer. This
    starts the 60-day clock; ``promote.py`` moves it to ``dormant`` when that expires.
    Going dormant **keeps** the reserved names — see ``--reopen`` below. **No message is
    sent by this** — asking the question is an email a human writes. This only records that
    we are waiting, which is what makes the clock run and what stops the submission looking
    abandoned.

``--reopen``
    The submitter came back. Moves a ``declined`` or ``dormant`` submission to
    ``in_review``, re-claims its reserved names, and clears the disposition so the decision
    record stops reporting it as finished.

    **This is the resubmission path, and it is deliberately not "file the form again".** A
    curator who rejects a sequence for an indel asks the submitter to resequence; the work
    that comes back belongs to the submission that was already reserved for it. Filing a
    fresh form would mint a new identifier, start a new date, and — because name
    reservation priority is earliest-timestamp-wins — put the submitter behind anyone who
    claimed the name while they were at the bench. Reopening keeps the identifier, the
    original date and the claim.

    The new material still has to reach the submission directory: reopening moves the
    ledger, not files. Re-screen after the corrected workbook is in place.

``--decline`` and ``--withdraw`` release the submission's reserved lineage names.
``--ask`` does not, and neither does the dormancy that follows it. All of them drop the
submission out of the public queue, because ``public_queue`` shows live submissions only.

Why the reason is a code and not a sentence
-------------------------------------------
``--reason`` is drawn from :data:`ledger.DISPOSITION_REASON_CODES`, and this program
narrows that list further per action. The reason reaches ``data/decisions.json``, the one
committed file whose entire premise is that it contains no unpublished science. A free-text
reason there would eventually receive a sentence describing somebody's data, in the one
place meant to survive the erasure of that data.

Telling the submitter
---------------------
This program sends nothing. ``notify_submitters.py`` finds declined submissions and sends
the decline notice, after the same 24-hour wait an approval gets — which here does the
plainer job of giving anyone a window to notice a mistake before a person is told their
work was refused. A withdrawal sends nothing, because the submitter is the one who asked.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation.config import load_config, repo_root      # noqa: E402
from malavi_curation import ledger as ledger_mod               # noqa: E402
from malavi_curation import public_feeds                       # noqa: E402
from malavi_curation import release_gate                       # noqa: E402
from malavi_curation.release_store import read_store, store_dir  # noqa: E402

# Writing nothing is the default, as in every other program here that changes the ledger.
DRY_RUN_DEFAULT = True

# Why a submission may be declined. Defined in the ledger, not here: since 2026-08-13 a
# lead can also close a submission from the verdict form, and a vocabulary that lived in
# this file would be one the form could disagree with.
DECLINE_REASONS = ledger_mod.DECLINE_REASON_CODES

# A withdrawal has exactly one reason, and it is not the maintainer's to characterize.
WITHDRAW_REASON = "withdrawn_by_submitter"

# action -> (target state, what to call it in a sentence)
#
# `reopen` targets in_review rather than ready_for_review because a reopened submission has
# already been screened once and already carries verdicts; sending it back to
# ready_for_review would describe it as untouched. The ledger permits the move from both
# closed states (see ALLOWED_TRANSITIONS) and re-claims the names on the way.
ACTIONS = {
    "decline": ("declined", "declined"),
    "withdraw": ("withdrawn", "withdrawn by the submitter"),
    "ask": ("awaiting_submitter", "waiting on the submitter"),
    "reopen": ("in_review", "reopened"),
}

# The reason recorded when a submission is revived. Not the maintainer's to choose: what
# happened is that a finished submission came back, and `reopened` is the vocabulary the
# ledger already keeps for it so that history reads sensibly.
REOPEN_REASON = "reopened"


def chosen_action(arguments: argparse.Namespace) -> str:
    """Which of the three the operator asked for. Argparse guarantees exactly one."""
    for name in ACTIONS:
        if getattr(arguments, name):
            return name
    raise AssertionError("argparse should have required one of the three")   # unreachable


def reason_for(action: str, requested: str) -> Tuple[str, str]:
    """The reason code to record, or an explanation of why the requested one is refused.

    Returns ``(code, complaint)`` — exactly one of which is non-empty.
    """
    if action == "withdraw":
        # Offered as a courtesy so the command reads the same as the other two, but there
        # is nothing to choose: we are recording somebody else's decision, not judging it.
        if requested and requested != WITHDRAW_REASON:
            return "", (f"a withdrawal is always recorded as {WITHDRAW_REASON!r}; the "
                        f"submitter's own reason is not ours to characterize in the "
                        f"decision record.")
        return WITHDRAW_REASON, ""

    if action == "reopen":
        # Same reasoning as a withdrawal: we are recording what happened, not judging it.
        if requested and requested != REOPEN_REASON:
            return "", (f"a reopening is always recorded as {REOPEN_REASON!r}; why the "
                        f"submission was closed is already in its history, and why it came "
                        f"back is not a disposition.")
        return REOPEN_REASON, ""

    if action == "ask":
        # No disposition is being recorded, so there is nothing for a code to describe.
        if requested:
            return "", ("--reason does not apply to --ask: nothing is being disposed of, "
                        "we are only recording that we are waiting.")
        return "", ""

    if not requested:
        return "", (f"--decline needs --reason, one of: {', '.join(DECLINE_REASONS)}. A "
                    f"decline with no reason produces a decision record that says a "
                    f"submission was refused and nothing about why.")
    if requested not in DECLINE_REASONS:
        return "", (f"{requested!r} is not a reason a submission may be declined. "
                    f"Choose one of: {', '.join(DECLINE_REASONS)}.")
    return requested, ""


def retract_command(submission_id: str) -> str:
    """The exact ``ingest_submissions.py --retract`` invocation for one submission.

    ``--release`` is required by that program's parser even though a retraction writes no
    release tag, so today's date is filled in to make the line copy-pasteable rather than
    a template the operator has to finish.
    """
    return (f".venv/bin/python curation/ingest_submissions.py --release "
            f"{date.today().isoformat()} --retract {submission_id} --apply")


def submission_rows_in_store(submission_id: str, root: Optional[Path] = None) -> bool:
    """Does the record store hold rows this submission contributed?

    Read from the store itself rather than inferred from the ledger, because the ledger
    does not know whether ingest ran. A missing store (a fresh checkout) means no.
    """
    records = store_dir(root or repo_root())
    if not records.is_dir():
        return False
    return submission_id in release_gate.sources_in_store(read_store(records))


def describe_close(action: str, before: str, names: List[str],
                   config: Optional[Dict[str, Any]] = None,
                   submission_id: str = "", rows_in_store: bool = False) -> List[str]:
    """The lines reporting one close, written for somebody checking it did what they meant.

    ``rows_in_store`` says whether the submission was already ingested. A withdrawn or
    declined submission whose rows are still in the store blocks every release build, and
    the only thing that unblocks it is a command in another program; so that command is
    printed here, where the operator is, rather than left for RUNBOOK row 12b.
    """
    target, phrase = ACTIONS[action]
    lines = [f"  {before} -> {target}"]

    # Only a decline or a withdrawal gives a name back. Saying "released" on the other two
    # would be the single most damaging thing this program could misreport: an operator who
    # believes NECMON01 is free will offer it to the next submitter.
    if action in ("decline", "withdraw"):
        if names:
            lines.append(f"  reserved names released: {', '.join(sorted(names))}")
        else:
            lines.append("  reserved names released: none were held")
    elif action == "reopen":
        if names:
            lines.append(f"  reserved names re-claimed: {', '.join(sorted(names))}")
            lines.append("  no other ledger entry holds them (checked); still verify them "
                         "against the reservation store before relying on them")
        else:
            lines.append("  reserved names re-claimed: none were held")
    else:
        if names:
            lines.append(f"  reserved names KEPT: {', '.join(sorted(names))}")
        else:
            lines.append("  reserved names KEPT: none were held")

    if action == "reopen":
        lines.append("  it returns to the public queue as a live submission")
    else:
        lines.append("  it drops out of the public queue, which lists live submissions only")

    if action == "decline":
        lines.append("  notify_submitters.py will send the decline notice once the 24-hour "
                     "wait has run")
    elif action == "withdraw":
        lines.append("  no message is sent; the submitter is the one who asked")
    if action in ("decline", "withdraw") and rows_in_store:
        # The release gate refuses any store row whose submission is not approved or
        # released, and build_release refuses the whole build on one refusal. Until the
        # rows are taken out, NO release can be built -- see RUNBOOK row 12b.
        lines.append("  its rows are in the record store and now block every release "
                     "build; take them out with:")
        lines.append(f"      {retract_command(submission_id)}")
        lines.append("      (drop --apply to preview what would be removed)")
    if action == "reopen":
        lines.append("  no message is sent; re-screen once the new material is in the "
                     "submission directory, then the curators review it as usual")
    else:
        # The same reader the clock itself uses, so the number quoted here is the number
        # that will actually fire rather than a 60 hard-coded beside a configurable one.
        days = ledger_mod._review_config(config)["awaiting_submitter_timeout_days"]
        lines.append(f"  the {days}-day clock starts now; promote.py moves it to dormant "
                     f"if they do not reply")
        lines.append("  dormancy does NOT give the names back; only a decline does")
        lines.append("  no message is sent; asking the question is an email a human writes")

    lines.append(f"  recorded as: {phrase}")
    return lines


def close(entry: Any, action: str, reason: str, actor: str, at: Optional[str] = None,
          config: Optional[Dict[str, Any]] = None,
          entries: Optional[Dict[str, Any]] = None,
          rows_in_store: bool = False) -> Tuple[bool, List[str]]:
    """Move one submission, in either direction. Returns ``(moved, lines to print)``.

    Named ``close`` because finishing a submission is what it was written for; it also
    reopens one, which is the same act of moving the ledger and refusing to if the rules
    say no.

    Never raises for a move the rules forbid. A refusal here is ordinary — an approved
    submission cannot be declined until somebody flags it, which is the ledger insisting
    that a decline follows an objection rather than replacing one; a submission that is
    still live cannot be reopened, because there is nothing to revive — and the operator
    needs to read why, not a traceback.

    ``entries`` is the rest of the ledger, and it is what lets a reopening be refused
    rather than merely warned about when a re-claimed name has since been issued to
    another submission (``ledger.transition`` does the check when it is given). Until
    2026-09-02 this program only printed "verify them against the reservation store", and
    a maintainer who did not would have revived a claim on a name somebody else had
    already been told was theirs.
    """
    target, _ = ACTIONS[action]
    before = entry.state
    # Read before the move: transition() empties nothing, but name_state changes underneath
    # and the names are the thing the operator most wants confirmed.
    names = list(entry.reserved_names)

    # Reopening is only ever the revival of a FINISHED submission, and this check is what
    # keeps it that way.
    #
    # It is not a tidiness rule. `held -> in_review` is a legitimate transition -- it is
    # what clearing a hold does -- and transition() guards only `approved` and `released`,
    # because the lead-only rule and the consultation record live in override_hold(), not
    # in the state machine. So without this, `--reopen` on a held submission would walk it
    # back to in_review with the curator's objection still standing, attributed to nobody,
    # with no record of who was consulted: a maintainer-side bypass of the one power the
    # ledger deliberately reserves to a lead. Caught by
    # test_a_live_submission_cannot_be_reopened, 2026-08-20.
    if action == "reopen" and before not in ledger_mod.CLOSED_STATES:
        why = ("is still live, so there is nothing to revive"
               if before in ledger_mod.LIVE_STATES
               else "is terminal and is not ours to revive")
        lines = [
            f"  REFUSED: {entry.submission_id} is {before}, which {why}.",
            f"           Reopening revives a finished submission "
            f"({' or '.join(ledger_mod.CLOSED_STATES)}).",
        ]
        if before in ledger_mod.LIVE_STATES:
            lines.append("           To clear a hold that is blocking this one, a lead "
                         "answers the verdict form; that route records who was consulted.")
        elif before == "withdrawn":
            lines.append("           A submitter who took their submission back has to "
                         "send it again; we do not reinstate it on their behalf.")
        elif before == "released":
            lines.append("           It is published. Correct the records with "
                         "correct_store.py instead.")
        return False, lines

    try:
        ledger_mod.transition(entry, target, actor=actor, at=at, reason=reason,
                              config=config, entries=entries)
    except ledger_mod.LedgerError as exc:
        return False, [f"  REFUSED: {exc}"]

    return True, describe_close(action, before, names, config,
                                submission_id=entry.submission_id,
                                rows_in_store=rows_in_store)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--submission", required=True, metavar="MALAVI-SUB-YYYY-NNNNNN",
                        help="the opaque submission id, as the curator report prints it")

    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--decline", action="store_true",
                      help="we will not include this submission (needs --reason)")
    what.add_argument("--withdraw", action="store_true",
                      help="the submitter took it back")
    what.add_argument("--ask", action="store_true",
                      help="we asked the submitter something; start the 60-day clock")
    what.add_argument("--reopen", action="store_true",
                      help="the submitter came back; revive a declined or dormant "
                           "submission, keeping its id, date and reserved names")

    parser.add_argument("--reason", default="", metavar="CODE",
                        help=f"why, for --decline: {', '.join(DECLINE_REASONS)}")
    parser.add_argument("--actor", default="maintainer",
                        help="who is recording this; goes in the ledger and the decision "
                             "record")
    parser.add_argument("--no-publish", action="store_true",
                        help="do not rebuild and publish the public queue afterwards")
    parser.add_argument("--apply", action="store_true",
                        help="write the change (default is to describe it only)")
    arguments = parser.parse_args(argv)

    action = chosen_action(arguments)
    reason, complaint = reason_for(action, arguments.reason.strip())
    if complaint:
        print(f"ERROR: {complaint}", file=sys.stderr)
        return 2

    configuration = load_config()
    inbox = repo_root() / (configuration.get("submissions") or {}).get(
        "inbox_dir", "curation/intake/submissions")

    print("== malavi_rebuild :: close_submission ==")
    print(f"{arguments.submission}: {ACTIONS[action][1]}"
          f"{f', reason {reason}' if reason else ''}\n")

    # write=False on a dry run: the entry below is still moved in memory, so the operator
    # sees the real refusal or the real result rather than a prediction of it.
    with ledger_mod.open_ledger(inbox, write=arguments.apply) as entries:
        entry = entries.get(arguments.submission)
        if entry is None:
            # Deliberately not created. Closing a submission that does not exist is a
            # mistyped id, and inventing an entry for it would put a closed submission
            # nobody ever made into the decision record.
            print(f"ERROR: no submission {arguments.submission} in the review ledger.",
                  file=sys.stderr)
            live = [sid for sid, other in sorted(entries.items())
                    if other.state in ledger_mod.LIVE_STATES]
            if live:
                print(f"       live submissions: {', '.join(live)}", file=sys.stderr)
            return 2

        # Only a decline or a withdrawal can leave ingested rows stranded in the store, so
        # only those two pay for reading it.
        in_store = (action in ("decline", "withdraw")
                    and submission_rows_in_store(arguments.submission))
        moved, lines = close(entry, action, reason, arguments.actor,
                             config=configuration, entries=entries,
                             rows_in_store=in_store)
        for line in lines:
            print(line)

    if not moved:
        return 1
    if not arguments.apply:
        print("\n[dry-run] nothing was written. Re-run with --apply to record this.")
        return 0

    # A closed submission drops off the public queue entirely -- it is never labeled
    # "declined". A reopened one reappears on it. Publishing here is what makes either
    # change prompt rather than dependent on someone rebuilding the feeds later.
    if not arguments.no_publish:
        print("")
        public_feeds.refresh()
    return 0


if __name__ == "__main__":
    sys.exit(main())
