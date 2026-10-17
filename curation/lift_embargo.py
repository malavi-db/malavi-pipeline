#!/usr/bin/env python3
# @title Lift the embargo on a submission whose study is now out
# @purpose Stop holding a submitter's records back, by submission id or by the study they
#   cite, without needing those records to be in the store first.
# @why The embargo could be set and never lifted. The only program that lifted one found
#   its submissions through the store's `_source` column -- and an embargoed submission is
#   refused by the release gate, so its rows are never ingested and never appear there.
#   The one thing that could release the records looked in the one place they could not be.
# @input curation/intake/submissions/review_ledger.json (via malavi_curation.ledger)
# @input curation/intake/submissions/<dir>/*.xlsx (which study a submission cites)
# @output the same ledger, with the embargo lifted or set
# @program python3
# @critical-flag lift_embargo.py "" --apply
"""Lift the embargo on a submission — or set one, when a submitter asks late.

What an embargo is
------------------
A submitter with unpublished data can ask us to hold their *records* until their study is
out. Review continues: the submission is screened, curators decide, and their lineage names
are reserved and confirmed. Only publication waits. That is the whole point — somebody
waiting on a journal gets their name secured without their unpublished data being released
from under them.

The deadlock this program fixes
-------------------------------
``publish_reference.py`` lifts embargoes as a side effect of renaming ``"<Authors> unpubl"``
to a real citation, which is the right trigger: publication is the event the submitter was
waiting for. It finds which submissions to lift through ``submissions_behind()``, which
reads the ``_source`` column of rows **in the record store**.

An embargoed submission has no rows in the store. ``release_gate.admissibility`` refuses it,
so ``ingest_submissions`` skips it — deliberately, because writing its rows in would block
every release build until the embargo lifted. So the provenance that would identify it never
exists, and for a study whose records are *entirely* embargoed ``publish_reference`` does
not even get that far: nothing in the store cites the name, and it stops.

This program does not consult the store at all. It reads the ledger for the embargo and the
submitted workbook for the study, both of which exist from the moment the submission
arrives.

The order to do things in
-------------------------
For a study whose records are entirely embargoed, renaming is not the first step — there is
nothing yet to rename::

    1. lift_embargo.py --reference "Barrow et al unpubl" --apply
    2. ingest_submissions.py --release <date> --apply     # the rows arrive, still "unpubl"
    3. publish_reference.py "Barrow et al unpubl" "Barrow et al 2027" ... --apply

Where a study is only *partly* embargoed — MalAvi already held some of its records —
``publish_reference`` lifts the rest itself and step 1 is unnecessary.

Setting an embargo
------------------
``--set`` exists for the submitter who asks after the fact: they filed expecting to publish,
then asked us to wait. Normally the embargo comes from their answer on the submission form
and enrollment applies it. Once this program has recorded a decision either way, enrollment
stops re-reading that form answer — otherwise a lift would be silently undone by the next
intake run, and the release would go on withholding records the author had released.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation.config import load_config, repo_root      # noqa: E402
from malavi_curation import embargo as embargo_mod             # noqa: E402
from malavi_curation import ledger as ledger_mod               # noqa: E402

# Writing nothing is the default, as in every other program here that changes the ledger.
DRY_RUN_DEFAULT = True


def change_embargo(entry: Any, embargoed: bool, actor: str, note: str,
                   at: Optional[str] = None) -> str:
    """Set or lift one embargo. Returns a line describing what happened.

    Never raises. ``set_embargo`` refuses a released submission — its records are already
    published and the ledger cannot un-publish them — and that refusal is something the
    operator needs to read, not a traceback.
    """
    if entry.embargoed == embargoed:
        # Still recorded, deliberately: set_embargo writes a history event either way, and
        # "somebody looked at this and confirmed it should stay held" is a different fact
        # from "nobody has considered it since intake". The second is what lets enrollment
        # keep re-reading the submitter's form answer.
        was = "already held" if embargoed else "was not embargoed"
        prefix = f"  {entry.submission_id}: {was}"
    else:
        prefix = f"  {entry.submission_id}: embargo {'set' if embargoed else 'lifted'}"

    try:
        ledger_mod.set_embargo(entry, embargoed, actor=actor, at=at, note=note)
    except ledger_mod.LedgerError as exc:
        return f"  REFUSED {entry.submission_id}: {exc}"

    return (f"{prefix}; the decision is recorded, so enrollment will stop re-reading the "
            f"submitter's original form answer")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    which = parser.add_mutually_exclusive_group()
    which.add_argument("--submission", default="", metavar="MALAVI-SUB-YYYY-NNNNNN",
                       help="the submission to act on")
    which.add_argument("--reference", default="", metavar="STUDY",
                       help="act on every embargoed submission citing this study, e.g. "
                            "'Barrow et al unpubl'. Matched exactly, as REFERENCE_NAME is "
                            "everywhere else.")

    parser.add_argument("--set", dest="set_embargo", action="store_true",
                        help="hold the records instead of releasing them, for a submitter "
                             "who asked after filing")
    parser.add_argument("--note", default="",
                        help="why, in your own words. Kept in the gitignored ledger only, "
                             "never in the committed decision record.")
    parser.add_argument("--actor", default="maintainer",
                        help="who is recording this")
    parser.add_argument("--apply", action="store_true",
                        help="write the change (default is to describe it only)")
    arguments = parser.parse_args(argv)

    configuration = load_config()
    inbox = repo_root() / (configuration.get("submissions") or {}).get(
        "inbox_dir", "curation/intake/submissions")
    lifting = not arguments.set_embargo

    print("== malavi_rebuild :: lift_embargo ==")

    with ledger_mod.open_ledger(inbox, write=arguments.apply) as entries:
        # No target: this is the listing, which is also what an operator wants when they
        # know a paper is out but not which submission it was.
        if not arguments.submission and not arguments.reference:
            for line in embargo_mod.describe(inbox, entries):
                print(line)
            print("\nName one with --submission, or the study with --reference.")
            return 0

        if arguments.submission:
            if arguments.submission not in entries:
                print(f"ERROR: no submission {arguments.submission} in the review ledger.",
                      file=sys.stderr)
                return 2
            targets = [arguments.submission]
            notes: List[str] = []
        else:
            targets, notes = embargo_mod.submissions_for_reference(
                inbox, entries, arguments.reference)
            for line in notes:
                print(f"  {line}")
            if not targets:
                # Not an error. "Nothing is held for this study" is the normal answer once
                # publish_reference has already lifted it, and is also what a typo looks
                # like — so the listing is offered rather than a bare refusal.
                print(f"\nNo embargoed submission cites {arguments.reference!r}.")
                print("Reference names are compared exactly. What is held:\n")
                for line in embargo_mod.describe(inbox, entries):
                    print(line)
                return 1

        print(f"\n{'lifting' if lifting else 'setting'} the embargo on "
              f"{len(targets)} submission(s):")
        for submission_id in targets:
            print(change_embargo(entries[submission_id], not lifting,
                                 arguments.actor, arguments.note))

    if not arguments.apply:
        print("\n[dry-run] nothing was written. Re-run with --apply to record this.")
    elif lifting:
        print("\nThe records are no longer held back. They still have to be ingested and\n"
              "released: ingest_submissions.py, then build_release.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
