#!/usr/bin/env python3
# @title Correct records MalAvi already holds
# @purpose Apply one correction to the record store -- by record id, by site, or by an
#          exact column value -- and append it to the committed correction log.
# @why The published database carries faults that have to be fixed for the next edition:
#      54 host records at one site with the longitude sign flipped, 90 vector rows
#      spelling Unknown as Unkown. Hand-editing an 18,000-row CSV is how a second fault
#      gets introduced while fixing the first.
# @input data/records/*.csv
# @output data/records/*.csv
# @output data/corrections.csv
# @program python3
# @critical-flag correct_store.py "" --apply
# @critical-flag correct_store.py "" --site
# @critical-flag correct_store.py "" --where
# @critical-flag correct_store.py "" --record
"""Correct records MalAvi already holds.

    # the 54 host records at one site whose longitude sign puts them in Somalia
    .venv/bin/python curation/correct_store.py \\
        --table host_records --site "Mata Seca State Park" \\
        --set SITE_COORDINATES="-14°50.91100', -043°59.29800'" \\
        --reason "longitude sign; the site is in Minas Gerais, Brazil, not Somalia"

    # the misspelled vector method
    .venv/bin/python curation/correct_store.py \\
        --table vector_records --where VECTOR_METHOD=Unkown \\
        --set VECTOR_METHOD=Unknown --reason "spelling"

Nothing is written without ``--apply``. The default run prints every row that would
change, with the value it holds now, so the selection can be checked before it is made.

**One decision, however many rows.** Rows sharing a wrong value are one mistake. Selecting
by site or by value and setting the field once records a single decision with a single
reason; the log records how many rows it touched.

**A published edition is never edited.** This changes the store, which is the *next*
edition. The correction then appears in that edition's report under "Records corrected",
which is what tells somebody working from the previous edition that a value they hold has
changed.

**This is not the submission-correction path.** A curator fixing a submitter's rows before
ingest goes through ``apply_corrections.py``, which records the fix as a revision over a
workbook that is never edited. This is for faults in data MalAvi has already published.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import store_corrections                      # noqa: E402
from malavi_curation.config import repo_root                       # noqa: E402
from malavi_curation.release_store import (                        # noqa: E402
    TABLES, read_store, store_dir, write_store,
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", required=True, choices=sorted(TABLES),
                        help="Which store table holds the rows to correct.")

    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--record", metavar="RECORD_ID",
                          help="Correct exactly one row, by its RECORD_ID.")
    selector.add_argument("--site", metavar="SITE_NAME",
                          help="Correct every row at this site. Matched exactly, so "
                               "'Mata Seca' does not also select 'Mata Seca II'.")
    selector.add_argument("--where", metavar="COLUMN=VALUE",
                          help="Correct every row whose COLUMN holds exactly VALUE.")

    parser.add_argument("--set", dest="assignment", required=True, metavar="COLUMN=VALUE",
                        help="The column to change and the value to set.")
    parser.add_argument("--reason", required=True,
                        help="Why. Written to the correction log and read by whoever "
                             "asks in five years why a published value changed.")
    parser.add_argument("--actor", default="maintainer",
                        help="Who is making the correction. Recorded in the log.")
    parser.add_argument("--apply", action="store_true",
                        help="Write the change. Without it nothing is written.")
    return parser.parse_args(argv)


def split_assignment(text: str, flag: str) -> tuple:
    """``COLUMN=VALUE`` -> ``(COLUMN, VALUE)``, splitting on the FIRST ``=`` only.

    A coordinate, a title or a comment can contain ``=``; the column name cannot.
    """
    if "=" not in text:
        raise SystemExit(f"error: {flag} needs COLUMN=VALUE, not {text!r}")
    column, _, value = text.partition("=")
    column = column.strip()
    if not column:
        raise SystemExit(f"error: {flag} has no column name in {text!r}")
    return column, value


def main(argv=None) -> int:
    args = parse_args(argv)
    root = repo_root()
    records = store_dir(root)

    column, new_value = split_assignment(args.assignment, "--set")

    if args.record:
        kind, selector_value, selector_column = "record", args.record, ""
    elif args.site:
        kind, selector_value, selector_column = "site", args.site, ""
    else:
        selector_column, selector_value = split_assignment(args.where, "--where")
        kind = "where"

    correction = store_corrections.Correction(
        table=args.table, column=column, new_value=new_value, reason=args.reason,
        actor=args.actor, selector_kind=kind, selector_value=selector_value,
        selector_column=selector_column)

    store = read_store(records)
    if not any(store.values()):
        print(f"error: {records} holds no records.", file=sys.stderr)
        return 1

    try:
        changes = store_corrections.plan(store, correction)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print("== malavi_rebuild :: correct_store ==")
    print(f"store    : {records}")
    print(f"table    : {correction.table}")
    print(f"selector : {correction.describe_selector()}")
    print(f"set      : {correction.column} = {correction.new_value!r}")
    print(f"reason   : {correction.reason}\n")

    if not changes:
        # Either nothing matched, or everything matched already holds the new value. Both
        # mean "there is nothing to do", and neither should be reported as a success that
        # changed something.
        print("Nothing to change: the selector matched no row that does not already hold "
              "that value.")
        print("Check the selector -- it is matched exactly, with no case folding and no "
              "partial matches.")
        return 2

    print(f"{len(changes)} row(s) would change:\n")
    for change in changes[:40]:
        context = ", ".join(f"{key}={value}" for key, value in change.context.items())
        print(f"  {change.record}  {change.was!r} -> {change.now!r}")
        print(f"      {context}")
    if len(changes) > 40:
        print(f"  ... and {len(changes) - 40} more")

    distinct = {change.was for change in changes}
    if len(distinct) > 1:
        # Not refused: correcting several wrong spellings to one right one is a legitimate
        # bulk fix. But an operator who did not expect it has selected too broadly, and
        # this is the last moment to notice.
        print(f"\n  NOTE: the selected rows do not all hold the same value -- "
              f"{len(distinct)} different values would be replaced by one. The log will "
              f"record the old value as '(various)'.")

    if not args.apply:
        print(f"\n[dry-run] nothing was written. Re-run with --apply to correct "
              f"{len(changes)} row(s).")
        return 0

    store_corrections.apply(store, correction)
    write_store(records, store)
    entry = store_corrections.append_log(
        store_corrections.log_path(root), correction, changes)

    print(f"\nCorrected {len(changes)} row(s) in {records}.")
    print(f"Logged as {entry['CORRECTION_ID']} in "
          f"{store_corrections.log_path(root)}.")
    print("Both are git-tracked: read the diff, then commit them together.")
    print("The change appears in the next release's edition report under "
          "'Records corrected'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
