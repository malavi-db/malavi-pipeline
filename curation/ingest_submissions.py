#!/usr/bin/env python3
# @title Ingest approved submissions into the record store
# @purpose Read each approved submission's ImportMalavi workbook, map it into the five
#   store tables, and write the store -- so that a curator's approval actually reaches
#   the data MalAvi publishes.
# @why store_ingest existed with no caller outside its tests: the store could be seeded
#   from the last external release and never added to. A curator could approve a
#   submission and nothing would carry its records into MalAvi.
# @input curation/intake/submissions/<dir>/*.xlsx  (the filled template)
# @input curation/intake/submissions/review_ledger.json
# @input data/records/*.csv
# @output data/records/*.csv
# @program python3
# @critical-var DRY_RUN_DEFAULT
# @critical-flag ingest_submissions.py "" --release
# @critical-flag ingest_submissions.py "" --submission
# @critical-flag ingest_submissions.py "" --apply
# @critical-flag ingest_submissions.py "" --allow-blanking
"""Carry approved submissions into the record store.

This is the missing half of the release gate. ``release_gate`` refuses to publish a row
nobody approved; this is the only path by which an approved row arrives at all.

**It ingests exactly what the gate would publish, and it asks the gate.** Both call
``release_gate.admissibility``, so the ingest cannot be more permissive than the release.
That matters in one direction especially: an embargoed submission is approved but must not
be published, and writing its rows into the store would block *every* release build until
the embargo lifted, with the operator left to work out which submission was at fault.

    # what would be ingested, writing nothing
    .venv/bin/python curation/ingest_submissions.py --release 2026-08-14

    # do it
    .venv/bin/python curation/ingest_submissions.py --release 2026-08-14 --apply

    # re-ingest one submission after a correction
    .venv/bin/python curation/ingest_submissions.py --release 2026-08-14 \\
        --submission MALAVI-SUB-2026-000123 --apply

**What it does not do.** It does not mark anything released -- ``build_release`` does that,
once the ZIP exists. It does not edit the submitted workbook, which stays exactly what the
submitter sent. And it decides nothing: every value is either copied from the workbook or
derived from a lookup this project can point at, and anything MalAvi cannot source is left
blank and reported for a curator.

Exit codes: ``0`` nothing to do, or everything ingested; ``1`` the run could not start;
``2`` refused, nothing written; ``3`` written, but at least one submission was refused and
needs a person.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import ledger, release_gate, store_ingest      # noqa: E402
from malavi_curation.config import repo_root                        # noqa: E402
from malavi_curation.release_store import (                         # noqa: E402
    SEED, TABLES, assign_ids, read_store, store_dir, write_store,
)
from malavi_curation.submission_id import directory_for, is_opaque  # noqa: E402

# Writing nothing is the default. This writes the authoritative MalAvi, from files a
# submitter sent, on the strength of a decision recorded elsewhere. The operator should
# see the counts and the curator notes before any of it lands.
DRY_RUN_DEFAULT = True


def submissions_inbox(root: Path) -> Path:
    """Where the review ledger and the submission directories live."""
    return root / "curation" / "intake" / "submissions"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", required=True, metavar="YYYY-MM-DD",
                        help="The release these records will first appear in. Written to "
                             "_added, which answers 'since when has MalAvi held this?'. "
                             "Required rather than defaulted to today, because a row "
                             "ingested on one day and published on another would "
                             "otherwise claim a release that never existed.")
    parser.add_argument("--submission", action="append", default=[], metavar="ID",
                        help="Ingest exactly this submission, whether or not the store "
                             "already holds its rows. Repeatable. This is the re-ingest "
                             "path a correction takes. Without it, every approved "
                             "submission the store does not yet hold is ingested.")
    parser.add_argument("--retract", action="append", default=[], metavar="ID",
                        help="Take this submission's rows back OUT of the store, and do "
                             "nothing else. Repeatable. For a submission that was "
                             "ingested and then withdrawn or held: until this existed, "
                             "such a submission blocked every release build with no way "
                             "back except editing the CSVs by hand.")
    parser.add_argument("--apply", action="store_true",
                        help="Write the store (default is to report what would change)")
    parser.add_argument("--allow-blanking", action="store_true",
                        help="Proceed even though the workbook would empty values the "
                             "store holds -- typically taxonomy a curator filled in after "
                             "the first ingest. Every lost value is listed either way.")
    return parser.parse_args(argv)


def resolve_directory(inbox: Path, submission_id: str) -> Optional[Path]:
    """The submission's directory on disk, or None if nothing maps to the id.

    The ledger is keyed by the minted opaque id while the directory is named after the
    submitter, so an opaque id goes through the reverse lookup. A directory name passed
    directly is honored too, which is what a maintainer working from the filesystem has.
    """
    name = directory_for(inbox, submission_id) if is_opaque(submission_id) \
        else submission_id
    if not name:
        return None
    path = inbox / name
    return path if path.is_dir() else None


def candidates(entries: Optional[Dict[str, ledger.Entry]],
               already_in_store: Sequence[str],
               requested: Sequence[str]) -> List[str]:
    """Which submissions this run will attempt.

    Named submissions are attempted as given -- that is the re-ingest path, and refusing
    one because its rows are already present would make a correction impossible to apply.

    Otherwise: every submission the gate would publish and whose rows the store does not
    yet hold. Submissions already in the store are left alone, because re-running the
    ingest over them would map the mapping's deliberate blanks back over whatever a
    curator has filled in since. To re-ingest one, name it.
    """
    if requested:
        return list(dict.fromkeys(requested))       # de-duplicated, order preserved
    if entries is None:
        return []
    present = set(already_in_store)
    return sorted(
        submission_id for submission_id in entries
        if submission_id not in present
        and release_gate.admissibility(submission_id, entries)[0]
        == release_gate.PUBLISHING)


def ingest_one(store: Dict[str, List[Dict[str, Any]]],
               submission_id: str,
               workbooks: Sequence[Path],
               release: str,
               corrections: Optional[Dict[str, str]] = None,
               ) -> Tuple[Dict[str, Dict[str, int]], List[str], List[Dict[str, str]],
                          List[str]]:
    """Put one submission's workbooks into ``store`` in place.

    Returns ``(counts by table, curator notes, values this would blank, refusals)``.
    **When ``refusals`` is non-empty the store is not touched at all** and the caller must
    refuse the submission. Two things refuse: a lineage name that is already taken, which
    cannot be written without putting two sequences under one key; and a sequence that is
    not a shape a barcode arrives in, which cannot be written without putting a misframed
    row into the alignment.

    ``corrections`` is the submission's agreed renames (``ledger.Entry.name_corrections``),
    applied here so that the name MalAvi stores is the name the curator approved rather
    than the one the submitter proposed.

    **Every workbook is read before any table is replaced.** A submission carrying two
    filled templates is unusual but legal, and replacing per workbook would have the
    second one delete the first one's rows -- ``replace_submission_rows`` replaces
    everything the submission contributed, which is exactly right once and destructive
    twice. The same ordering is what lets the collision check run before anything is
    written.
    """
    incoming: Dict[str, List[Dict[str, Any]]] = {}
    notes: List[str] = []
    for path in workbooks:
        tables, book_notes = store_ingest.tables_from_workbook(
            path, submission_id, release, existing_host_rows=store["host_records"])
        for name, rows in tables.items():
            incoming.setdefault(name, []).extend(rows)
        notes.extend(f"{path.name}: {note}" for note in book_notes)

    # The name agreed at approval, applied before anything is checked or written.
    incoming, rename_notes = store_ingest.apply_name_corrections(
        incoming, corrections or {})
    notes.extend(rename_notes)

    # ...and then refused if it is still a name somebody else holds. Checked against the
    # store before a single row is merged, so a refusal leaves nothing behind.
    refusals = store_ingest.colliding_lineages(
        store, incoming.get("lineages", []), submission_id)

    # A sequence that is not a shape a barcode arrives in is refused for the same reason a
    # taken name is: the row would be wrong in a way nothing downstream could detect. The
    # length check inside lineage_rows is not enough -- NECMON01 was exactly 479 bp and
    # still two bases out of the window, and passed it in silence.
    refusals.extend(store_ingest.misframed_sequences(
        store, incoming.get("lineages", []), submission_id))

    if refusals:
        return {}, notes, [], refusals

    # Everything left fits inside the window, so padding it there is arithmetic and not a
    # judgment. Done before the merge, so the store only ever holds full-window rows: a
    # partial barcode -- a forward-primer-only read, say -- is shorter as sent, and storing
    # it at that length would leave a ragged row among 5,368 aligned ones.
    if incoming.get("lineages"):
        incoming["lineages"], placement_notes = store_ingest.place_sequences(
            incoming["lineages"], store, submission_id)
        notes.extend(placement_notes)

    counts: Dict[str, Dict[str, int]] = {}
    blanked: List[Dict[str, str]] = []
    for name, rows in incoming.items():
        spec = TABLES[name]
        before = store[name]
        merged, table_counts = store_ingest.replace_submission_rows(
            spec, before, rows, submission_id)
        merged = assign_ids(spec, merged)
        blanked.extend(store_ingest.blanked_values(spec, before, merged, submission_id))
        store[name] = merged
        counts[name] = table_counts
    return counts, notes, blanked, []


def retract(store: Dict[str, List[Dict[str, Any]]], submission_id: str
            ) -> Dict[str, int]:
    """Remove every row a submission contributed. Returns rows removed, per table.

    The exact inverse of ingesting it: :func:`store_ingest.replace_submission_rows` with
    nothing incoming. Rows from ``seed`` or from any other submission are untouched, for
    the same reason they are untouched on the way in -- deciding somebody else's record is
    superseded is a curator's judgment, not an importer's.
    """
    removed: Dict[str, int] = {}
    for name in TABLES:
        spec = TABLES[name]
        merged, counts = store_ingest.replace_submission_rows(
            spec, store[name], [], submission_id)
        store[name] = assign_ids(spec, merged)
        if counts.get("removed"):
            removed[name] = counts["removed"]
    return removed


def retract_submissions(store: Dict[str, List[Dict[str, Any]]],
                        wanted: Sequence[str],
                        entries, records: Path, apply: bool) -> int:
    """The --retract path: take rows back out, and do nothing else.

    **Why this program can do it at all, when nothing else may.** Ingest writes a
    submission's rows into the store while the submission is merely *approved*, before any
    release exists. release_gate.admissibility then refuses any source whose entry is not
    approved or released, and build_release refuses the entire build on any violation. So
    a submission that was ingested and then withdrawn -- an allowed transition, and one a
    submitter can ask for at any time -- stopped MalAvi publishing ANY release, on any
    subject, until five CSVs were edited by hand. The only escape offered was
    --i-am-overriding-the-approval-gate, which publishes the withdrawn submitter's records:
    the opposite of what they asked for.

    So the guard is inverted here on purpose. Ordinary ingest refuses a submission the
    gate refuses; this refuses one the gate *allows*, because rows for an approved
    submission belong in the store and taking them out would quietly lose them. The one
    case it will not touch is ``released``: those rows are published, and unpublishing is a
    correction to a release, which is correct_store.py's job and leaves its own record.
    """
    removed_any = False
    refused: List[str] = []
    for submission_id in wanted:
        if submission_id == SEED:
            refused.append(f"{SEED}: the seed is not a submission.")
            continue
        verdict, reason = release_gate.admissibility(submission_id, entries)
        if verdict == release_gate.RELEASED:
            refused.append(
                f"{submission_id}: already released. Its rows are published, so removing "
                f"them is a correction to a release, not a retraction -- see "
                f"correct_store.py and RUNBOOK 8a.")
            continue
        if verdict != release_gate.REFUSED:
            refused.append(
                f"{submission_id}: the release gate is happy with this submission "
                f"({reason}). Retracting rows the gate would publish would lose them "
                f"silently. Close or hold the submission first, then retract.")
            continue
        removed = retract(store, submission_id)
        if not removed:
            print(f"{submission_id}: no rows in the store; nothing to retract. ({reason})")
            continue
        removed_any = True
        print(f"{submission_id}  -- {reason}")
        for name in sorted(removed):
            print(f"    {name:16s} removed {removed[name]}")

    for line in refused:
        print(f"  REFUSED  {line}", file=sys.stderr)

    if not removed_any:
        return 3 if refused else 0
    if not apply:
        print("\nNothing was written. Re-run with --apply to take these rows out.")
        return 0
    written = write_store(records, store)
    print(f"\nWrote {len(written)} table(s) to {records}.")
    print("The release gate will stop objecting to these submissions once this is "
          "committed. Review the diff first -- the store is the authoritative MalAvi.")
    return 3 if refused else 0


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        date.fromisoformat(args.release)
    except ValueError:
        print(f"error: --release must be a date, YYYY-MM-DD, not {args.release!r}. It is "
              f"written into _added and read as a release tag.", file=sys.stderr)
        return 1

    root = repo_root()
    inbox = submissions_inbox(root)
    records = store_dir(root)

    store = read_store(records)
    if not any(store.values()):
        print(f"error: {records} holds no records. Seed the store first (RUNBOOK step 3b) "
              f"-- ingesting into an empty store would give the submission's hosts no "
              f"taxonomy and no continent, because both are read from MalAvi's own "
              f"records.", file=sys.stderr)
        return 1

    entries = ledger.load(inbox) if ledger.ledger_path(inbox).is_file() else None
    already = sorted(release_gate.sources_in_store(store))

    print("== malavi_rebuild :: ingest_submissions ==")
    print(f"store    : {records}")
    print(f"release  : {args.release}  (written to _added)")
    print(f"ledger   : {'absent' if entries is None else f'{len(entries)} submission(s)'}")

    if args.retract:
        if args.submission:
            print("error: --retract takes rows out and --submission puts them in. Run "
                  "them separately, so the store is never both at once.", file=sys.stderr)
            return 1
        print()
        return retract_submissions(store, args.retract, entries, records, args.apply)

    wanted = candidates(entries, already, args.submission)
    if not wanted:
        print("\nNothing to ingest: no approved submission is missing from the store.")
        return 0

    ingested: List[str] = []
    refused: List[str] = []
    blanked: List[Dict[str, str]] = []
    print()
    for submission_id in wanted:
        if submission_id == SEED:
            refused.append(f"{submission_id}: the seed is not a submission. Its rows come "
                           f"from the last externally-produced release.")
            continue

        verdict, reason = release_gate.admissibility(submission_id, entries)
        if verdict == release_gate.REFUSED:
            refused.append(f"{submission_id}: {reason}")
            continue
        if verdict == release_gate.RELEASED and submission_id not in args.submission:
            # Reachable only when explicitly named; the default set excludes anything
            # already in the store. Re-ingesting published records is a correction and is
            # allowed, but never by accident.
            refused.append(f"{submission_id}: already released. Name it explicitly to "
                           f"re-ingest a correction to published records.")
            continue

        directory = resolve_directory(inbox, submission_id)
        if directory is None:
            refused.append(f"{submission_id}: no directory is mapped to this id in "
                           f"submission_ids.json, so there is no workbook to read.")
            continue

        workbooks = store_ingest.template_workbooks(directory)
        if not workbooks:
            refused.append(
                f"{submission_id}: no filled ImportMalavi template in {directory.name}. A "
                f"paper-only submission has to be extracted into a template and submitted "
                f"like any other before its records can be ingested.")
            continue

        # The names this submission was approved under. A submission with no ledger entry
        # cannot have agreed a rename, so it gets none -- and if its proposed name is
        # taken, the collision check below refuses it rather than guessing a new one.
        entry = (entries or {}).get(submission_id)
        corrections = dict(getattr(entry, "name_corrections", {}) or {})

        counts, notes, lost, refusals = ingest_one(
            store, submission_id, workbooks, args.release, corrections)
        if refusals:
            # Nothing was staged for this submission. Refusing is the only safe answer:
            # writing a lineage name somebody else holds corrupts the key every downstream
            # join uses, and it cannot be undone by a later correction without knowing
            # which of the two rows was which. A misframed sequence is worse still -- it
            # looks like data, so nothing downstream reports it as missing.
            refused.append(f"{submission_id}: " + " ".join(refusals))
            continue

        blanked.extend(lost)
        ingested.append(submission_id)

        print(f"{submission_id}  ({len(workbooks)} workbook(s): "
              f"{', '.join(path.name for path in workbooks)})")
        for name in sorted(counts):
            count = counts[name]
            if not any(count.values()):
                continue
            print(f"    {name:16s} " + "  ".join(
                f"{label} {count[label]}" for label in ("added", "replaced", "kept",
                                                        "removed")))
        for note in notes:
            print(f"    note: {note}")

    for line in refused:
        print(f"REFUSED {line}")

    if blanked:
        print(f"\n{len(blanked)} value(s) the store holds would be emptied by the "
              f"workbook. These are usually values a curator filled in after the first "
              f"ingest -- taxonomy, a continent, SEQ_LENGTH -- which the mapping leaves "
              f"blank on purpose:")
        for lost in blanked[:20]:
            print(f"    {lost['table']}.{lost['column']} on {lost['record_id']}: "
                  f"{lost['was']!r} -> blank")
        if len(blanked) > 20:
            print(f"    ... and {len(blanked) - 20} more")

    if not ingested:
        print("\nNothing was ingested.")
        return 2 if refused else 0

    if blanked and not args.allow_blanking:
        print("\nNothing was written. Either put those values in the workbook and run "
              "again, or accept the loss with --allow-blanking.")
        return 2

    if not args.apply:
        print(f"\n[dry-run] nothing was written. Re-run with --apply to write "
              f"{records}.")
        return 0

    written = write_store(records, store)
    print(f"\nWrote {len(written)} table(s) to {records}.")
    print("Review the diff before committing -- the store is the authoritative MalAvi.")
    print("Nothing is marked released: build_release does that, once a ZIP exists.")
    # 3, not 0, when something was refused: the store was written and is consistent, but a
    # submission a curator believes is in MalAvi is not. A zero exit would say otherwise.
    return 3 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
