#!/usr/bin/env python3
# @title Rename an unpublished reference once its study is published
# @purpose Rewrite "<Authors> unpubl" to the real citation across every table that cites
#   it, and add the reference row that could not exist while the study was unpublished.
# @why MalAvi accepts records before publication, credited as "<Authors> unpubl". That
#   name is temporary by design, and REFERENCE_NAME is the join key -- so publication
#   means rewriting the citation on every record row of the study, in four tables, without
#   disturbing RECORD_ID or the provenance columns. Done by hand that is slow and easy to
#   half-finish, and a half-finished rename splits one study into two.
# @input data/records/*.csv
# @input curation/intake/submissions/review_ledger.json
# @output data/records/*.csv (rewritten in place, only with --apply)
# @output curation/intake/submissions/review_ledger.json (embargoes lifted, with --apply)
# @program python3
# @critical-var TABLES_WITH_REFERENCES
# @critical-flag publish_reference.py "" --apply
"""Turn an unpublished reference into a published one.

    curation/publish_reference.py "Barrow et al unpubl" "Barrow et al 2027" \\
        --year 2027 --title "..." --journal "Mol Ecol" --pages "36:1123-1140" \\
        --study-type Community

**Dry run by default.** Nothing is written without ``--apply``. The default run prints
exactly what would change, per table, so the diff can be read before it exists.

What it does
------------
1. rewrites ``REFERENCE_NAME`` from the old name to the new one in every table that
   carries the column -- host_records, vector_records, alt_names, morpho_species;
2. adds the row to references.csv that was deliberately absent while the study was
   unpublished (see ``malavi_curation.reference_names`` for why it was absent);
3. refuses, before writing anything, if the rename would collide;
4. lifts the embargo on the submissions behind the study, found rather than typed in.
   Publishing the reference is the event those submitters were waiting for, and a flag
   left set would go on withholding records from every release with nothing to show that
   anything was wrong.

They are found two ways, because one was never enough. ``submissions_behind`` reads the
``_source`` column of rows in the store, which answers for every submission whose records
were ingested -- and by construction excludes every embargoed one, since the release gate
refuses those and the ingest skips them. ``embargoed_behind`` asks the ledger instead, and
reads the study out of the submitted workbook. Until 2026-08-13 only the first existed, so
the only route to lifting an embargo looked in the one place an embargoed submission's rows
could not be.

For a study whose records are **entirely** embargoed there is nothing here to rename, and
this program says so and stops rather than adding the reference row: doing that would make
the real rename refuse later, when the published name already has a row. The order in that
case is ``lift_embargo.py``, then ``ingest_submissions.py``, then this.

Why it refuses on a collision
-----------------------------
``REFERENCE_NAME`` is part of the natural key of every record table -- a record is
lineage x host x site x **reference**. So renaming does not merely relabel rows, it
changes their identity, and if some of the study's records were already entered under
the published citation the rename creates two rows with one key. That is a real
possibility here: a submitter sends unpublished records, the paper appears, and somebody
curates the paper from scratch without noticing MalAvi already had it.

Merging those rows is a judgment about which version is right, and this program does not
make judgments. It reports the collisions and stops.

What it does not touch
----------------------
``RECORD_ID``, ``_source`` and ``_added`` **on the rows it renames**. A row that changes
its citation is the same row -- it is the study that changed, not the observation -- and
rewriting its provenance would erase the fact that MalAvi held this record before the
paper existed.

The one NEW row, in references.csv, is stamped here: see ``source_for_reference``. It
inherits the provenance of the rows that cite it, and leaving it blank -- as this program
did until 2026-08-10, on the belief that the release build would fill it in -- makes
``release_gate`` refuse every subsequent build.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import embargo, ledger, reference_names       # noqa: E402
from malavi_curation.config import repo_root                       # noqa: E402
from malavi_curation.release_store import (                        # noqa: E402
    SEED, TABLES, assign_ids, read_table, row_key, store_dir, write_table,
)


def submissions_inbox(root: Path) -> Path:
    """Where the review ledger lives."""
    return root / "curation" / "intake" / "submissions"


def source_for_reference(tables: Dict[str, List[Dict[str, str]]], old_name: str,
                         behind: List[str]) -> str:
    """The provenance to stamp on the new references.csv row.

    **It must not be blank.** The comment here used to say the value was "left blank for
    the release build to stamp" -- but ``release_store.stamp`` has exactly one caller,
    ``release_seed``, and no build has ever called it. The row was therefore committed
    with an empty ``_source`` permanently, and ``release_gate`` refuses a release carrying
    rows whose provenance cannot be shown ("nothing can be shown to have approved them").
    So publishing a reference would have quietly bricked every subsequent release build,
    failing closed but with the cause a blank cell in a CSV.

    **A reference row inherits the provenance of the rows that cite it**, which is exactly
    their relationship: the citation describes the study those records came from. A study
    whose records are all seed gets ``seed`` -- this is completing the citation of data
    that arrived with the seed release, not claiming a review that never happened. A study
    with a submission behind it gets that submission, whose approval the gate then checks
    as it does for every other row of the study.

    Where several submissions contributed, the first is used and the caller has already
    printed all of them; any of them is a submission a curator approved for this study,
    which is the question the gate asks.

    **``behind`` here must be the store-derived list, not the union with the embargoed
    ones.** A reference row inherits the provenance of the rows that cite it, and an
    embargoed submission contributed none — its rows were never ingested. Stamping the row
    with it would assert the citation came from a submission that supplied nothing, and
    leave the release gate checking that submission's admissibility for a row it never
    brought.
    """
    if behind:
        return behind[0]
    return SEED

# Every table whose rows cite a study. references.csv is handled separately: it is where
# the new row is ADDED, not where a name is rewritten.
TABLES_WITH_REFERENCES = ("host_records", "vector_records", "alt_names", "morpho_species")


def submissions_behind(tables: Dict[str, List[Dict[str, str]]], name: str) -> List[str]:
    """The submissions whose records cite ``name``, from the store's own provenance.

    ``_source`` carries the submission id that brought a row, so the study being published
    already knows which ledger entries it belongs to -- no separate mapping is needed, and
    a curator does not have to remember a submission id to publish a reference.

    ``seed`` rows are skipped: they predate this project's review and have no ledger entry
    to lift. A study can legitimately have both, if MalAvi held some of its records from
    the old release and gained more by submission.

    **This finds only submissions whose rows reached the store, which by construction
    excludes every embargoed one.** ``release_gate.admissibility`` refuses an embargoed
    submission and ``ingest_submissions`` skips it, so it has no ``_source`` anywhere here.
    That was the deadlock: this was the only route to lifting an embargo, and it looked in
    the one place an embargoed submission's rows could not be. See
    :func:`embargoed_behind`, which asks the ledger and the submitted workbook instead, and
    whose result is unioned with this one before anything is lifted.
    """
    found = set()
    for rows in tables.values():
        for row in rows:
            if (row.get("REFERENCE_NAME") or "").strip() != name:
                continue
            source = str(row.get("_source") or "").strip()
            if source and source != SEED:
                found.add(source)
    return sorted(found)


def embargoed_behind(inbox: Path, name: str) -> tuple:
    """Embargoed submissions citing ``name``, found without the store. ``(ids, notes)``.

    The other half of :func:`submissions_behind`. An embargoed submission is invisible to
    the store by design, so its study is read from the workbook it arrived with — see
    :mod:`malavi_curation.embargo`.

    A missing or unopenable ledger is not an error here. This program's substance is the
    rename; a fresh clone with no intake tree should still be able to rename a reference,
    and it reports what it could not check rather than refusing to run.
    """
    if not ledger.ledger_path(inbox).is_file():
        return [], []
    try:
        entries = ledger.load(inbox)
    except Exception as exc:                                        # noqa: BLE001
        return [], [f"  NOTE: the review ledger could not be read ({exc}), so no embargo "
                    f"was checked."]
    found, notes = embargo.submissions_for_reference(inbox, entries, name)
    return found, [f"  NOTE: {line}" for line in notes]


def lift_embargoes(inbox: Path, submission_ids: List[str], new_name: str,
                   apply: bool) -> List[str]:
    """Stop holding these submissions' records back. Returns lines to print.

    **Why this belongs here.** Publishing the reference IS the event the embargo was
    waiting for: the submitter asked us to hold their records until the study was out, and
    renaming "<Authors> unpubl" to a real citation is the moment it is out. Leaving the
    flag set would keep the release gate refusing records whose paper is on a shelf, and
    the only signal that anything was wrong would be their absence from a release.

    Failures are reported, not raised. The rename is the substance of this program, and a
    ledger that cannot be opened -- a fresh clone, a lock held elsewhere -- must not undo
    a rename that already succeeded.
    """
    if not submission_ids:
        return []
    if not ledger.ledger_path(inbox).is_file():
        return [f"  NOTE: no review ledger at {inbox}, so no embargo was lifted."]

    lines: List[str] = []
    if not apply:
        for submission_id in submission_ids:
            lines.append(f"  would lift the embargo on {submission_id}")
        return lines

    try:
        with ledger.open_ledger(inbox) as entries:
            for submission_id in submission_ids:
                entry = entries.get(submission_id)
                if entry is None:
                    lines.append(f"  {submission_id}: not in the ledger; nothing lifted")
                    continue
                if not entry.embargoed:
                    lines.append(f"  {submission_id}: was not embargoed")
                    continue
                try:
                    ledger.set_embargo(entry, False, actor="maintainer",
                                       note=f"published as {new_name}")
                    lines.append(f"  {submission_id}: embargo lifted")
                except ledger.LedgerError as exc:
                    lines.append(f"  {submission_id}: could not lift -- {exc}")
    except Exception as exc:                                        # noqa: BLE001
        lines.append(f"  NOTE: the ledger could not be opened ({exc}); no embargo was "
                     f"lifted. The rename above is done and does not need repeating.")
    return lines


def _rows_citing(rows: List[Dict[str, str]], name: str) -> List[Dict[str, str]]:
    """Every row whose REFERENCE_NAME is exactly this study, compared on stripped text."""
    return [row for row in rows if (row.get("REFERENCE_NAME") or "").strip() == name]


def collisions(spec, rows: List[Dict[str, str]], old: str, new: str) -> List[tuple]:
    """Natural keys where a renamed row would land on top of a DIFFERENT study's row.

    Computed BEFORE anything is written, on a simulated rename, because the whole point
    is to find out whether the write is safe.

    **Only collisions the rename causes count.** The seed store already contains rows
    that share a natural key -- ``natural_key_violations`` in release_store exists
    precisely because it does -- and a naive "are there duplicates afterwards?" test
    reports several thousand of them, none of which this rename created. Two rows of the
    renamed study that already shared a key still share it afterwards; that is a
    pre-existing data question and blocking on it would make the program unusable.

    So the comparison is against the rows this rename does NOT touch: a problem is a
    renamed row acquiring the key of a row belonging to some other study, which is the
    case that genuinely merges two studies into one identity.
    """
    renamed = [row for row in rows if (row.get("REFERENCE_NAME") or "").strip() == old]
    untouched_keys = {
        row_key(spec, row) for row in rows
        if (row.get("REFERENCE_NAME") or "").strip() != old
    }
    found = set()
    for row in renamed:
        candidate = dict(row)
        candidate["REFERENCE_NAME"] = new
        key = row_key(spec, candidate)
        if key in untouched_keys:
            found.add(key)
    return sorted(found)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("old_name", help="the unpublished name, e.g. 'Barrow et al unpubl'")
    parser.add_argument("new_name", help="the published citation, e.g. 'Barrow et al 2027'")
    parser.add_argument("--year", default="", help="PUBLICATION_YEAR for the new row")
    parser.add_argument("--title", default="", help="TITLE for the new row")
    parser.add_argument("--journal", default="", help="JOURNAL_NAME for the new row")
    parser.add_argument("--pages", default="", help="VOLUME_PAGES, e.g. '36:1123-1140'")
    parser.add_argument("--study-type", default="", help="STUDY_TYPE, e.g. 'Community'")
    parser.add_argument("--release", default=date.today().isoformat(),
                        help="Release tag stamped as _added on the new reference row: "
                             "the release it first appears in. Defaults to today, which "
                             "is right when the reference is published shortly before a "
                             "build.")
    parser.add_argument("--apply", action="store_true",
                        help="write the changes. Without this, nothing is modified.")
    parser.add_argument("--allow-published-old-name", action="store_true",
                        help="rename a reference that is not marked unpubl. Off by "
                             "default: this program exists for the unpublished case, and "
                             "renaming a published citation is usually a typo being "
                             "propagated rather than a study appearing.")
    args = parser.parse_args(argv)

    old = args.old_name.strip()
    new = args.new_name.strip()
    directory = store_dir(repo_root())

    if old == new:
        print(f"ERROR: the old and new names are identical ({old!r}). Nothing to do.")
        return 2

    if not reference_names.is_unpublished(old) and not args.allow_published_old_name:
        print(f"ERROR: {old!r} is not marked unpublished, so this is not a study "
              f"appearing in print.\n"
              f"       If you really mean to rename a published citation, pass "
              f"--allow-published-old-name.")
        return 2

    if reference_names.is_unpublished(new):
        print(f"ERROR: the new name {new!r} is still marked unpublished. The point of "
              f"this program is to remove that marker.")
        return 2

    # ---- read everything first, decide second, write last --------------------------
    #
    # Nothing is written until every table has been checked. A rename that succeeded on
    # host_records and then aborted on alt_names would leave one study under two names,
    # which is worse than either name alone.
    tables: Dict[str, List[Dict[str, str]]] = {}
    affected: Dict[str, int] = {}
    problems: List[str] = []

    for table_name in TABLES_WITH_REFERENCES:
        spec = TABLES[table_name]
        rows = read_table(directory, spec)
        tables[table_name] = rows
        matching = _rows_citing(rows, old)
        affected[table_name] = len(matching)
        if matching:
            clashes = collisions(spec, rows, old, new)
            for key in clashes:
                problems.append(f"  {table_name}: two rows would share the key {key}")

    references = read_table(directory, TABLES["references"])
    existing = {(r.get("REFERENCE_NAME") or "").strip() for r in references}

    if new in existing:
        problems.append(
            f"  references: {new!r} is already a reference. This study appears to have "
            f"been curated twice -- once from the unpublished submission and once from "
            f"the paper. Merging them is a judgment call; this program will not make it.")
    if old in existing:
        problems.append(
            f"  references: {old!r} unexpectedly HAS a reference row. Unpublished studies "
            f"are not supposed to have one. Look at that row before renaming anything.")

    total = sum(affected.values())

    # ---- report --------------------------------------------------------------------
    print(f"{old!r}  ->  {new!r}\n")
    for table_name in TABLES_WITH_REFERENCES:
        count = affected[table_name]
        print(f"  {table_name:<16} {count:5d} row(s)")
    if total:
        # Only when there is a rename to hang it on. Printing "+1 new row" and then
        # refusing -- which is what every total==0 path below does -- announces a row that
        # is not going to be written.
        print(f"  {'references':<16} {'+1':>5} new row")
    print(f"\n  {total} record row(s) would be rewritten.")

    # Both halves of "which submissions is this study", computed before the zero check
    # because the zero case is exactly where the second half is the whole answer.
    inbox = submissions_inbox(repo_root())
    from_store = submissions_behind(tables, old)
    held, embargo_notes = embargoed_behind(inbox, old)
    behind = sorted(set(from_store) | set(held))

    if total == 0:
        if held:
            # The study is entirely embargoed: its records were never ingested, so there is
            # nothing here to rename and renaming is not the first step. Adding the new
            # reference row now would actively harm -- the later, correct rename refuses
            # when the published name already has a row ("curated twice").
            print(f"\nNOTHING IN THE STORE CITES {old!r}, because this study's records are "
                  f"still\nembargoed. Nothing to rename yet, and this program must not add "
                  f"the reference\nrow ahead of them -- doing so would make the real rename "
                  f"refuse later.\n")
            print(f"Held for this study: {', '.join(held)}\n")
            print("Do this instead:\n")
            print(f"  1. .venv/bin/python curation/lift_embargo.py \\\n"
                  f"         --reference {old!r} --apply")
            print("  2. .venv/bin/python curation/ingest_submissions.py "
                  "--release <date> --apply")
            print(f"  3. re-run this command")
            return 1
        print(f"\nNOTHING CITES {old!r}. Check the spelling -- reference names are "
              f"compared exactly.")
        for line in embargo_notes:
            print(line)
        return 1

    if problems:
        print("\nREFUSING TO WRITE. The rename would collide:\n")
        print("\n".join(problems))
        print("\nResolve these by hand, then run again.")
        return 1

    if not args.year or not args.journal:
        # A warning rather than a refusal: a curator may legitimately be renaming to a
        # citation whose details they will fill in from the paper in a moment, and
        # blocking that would only push them into editing the CSV by hand -- which is
        # the thing this program exists to stop.
        print("\nNOTE: --year and/or --journal are empty, so the new reference row will "
              "be incomplete.")

    # The submissions this study came from, so the embargo they are under can be lifted:
    # publishing the reference is precisely the event they were waiting for. Previewed
    # here; actually lifted after the rename is written, so a failed rename never leaves
    # records publishable under a citation that was not applied.
    if behind:
        print(f"\nSubmissions behind this study: {', '.join(behind)}")
        if held:
            print(f"  of which embargoed, found through the ledger rather than the store: "
                  f"{', '.join(held)}")
    for line in embargo_notes:
        print(line)
    for line in lift_embargoes(inbox, behind, new, apply=False):
        print(line)

    if not args.apply:
        print("\nDry run. Nothing was written. Re-run with --apply to make these changes.")
        return 0

    # ---- write ---------------------------------------------------------------------
    for table_name in TABLES_WITH_REFERENCES:
        spec = TABLES[table_name]
        rows = tables[table_name]
        if not affected[table_name]:
            continue
        for row in rows:
            if (row.get("REFERENCE_NAME") or "").strip() == old:
                # RECORD_ID, _source and _added are deliberately left exactly as they are.
                row["REFERENCE_NAME"] = new
        write_table(directory, spec, rows)
        print(f"  wrote {spec.filename}")

    spec = TABLES["references"]
    references.append({
        "REFERENCE_NAME": new,
        "PUBLICATION_YEAR": args.year,
        "TITLE": args.title,
        "JOURNAL_NAME": args.journal,
        "VOLUME_PAGES": args.pages,
        "STUDY_TYPE": args.study_type,
        # No RECORD_ID: assign_ids mints one and never reissues an existing id.
        # `from_store`, not `behind`: the reference row inherits the provenance of the rows
        # that cite it, and an embargoed submission contributed none of them. Stamping it
        # with one would claim the citation came from a submission whose records are not
        # in the store, and the release gate would then check that submission's
        # admissibility for a row it never supplied.
        "_source": source_for_reference(tables, old, from_store),
        "_added": args.release,
    })
    write_table(directory, spec, assign_ids(spec, references))
    print(f"  wrote {spec.filename}")

    # Only now, with the rename on disk. See the note where `behind` is computed.
    for line in lift_embargoes(inbox, behind, new, apply=True):
        print(line)

    print(f"\nDone. {total} row(s) now cite {new!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
