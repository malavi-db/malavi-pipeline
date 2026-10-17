#!/usr/bin/env python3
# @title Apply the curators' synonymy decisions to the store, one history row each
# @purpose Read a filled-in synonymy review sheet, turn every merge / remove / rename into
#          a lineage-history event, show what each would change, and under --apply make
#          the changes and write the history rows in one store write.
# @why The cleanup pass is many decisions at once; running record_lineage_event.py by
#      hand for each would be slow and error-prone. This reads the sheet the curators
#      voted on and applies exactly those rows, so what was decided is what changes.
# @input results/SYNONYMY_REVIEW_<date>.xlsx (or .csv), filled in
# @output data/records/*.csv (under --apply only)
# @program python
# @critical-flag apply_lineage_decisions.py "" --apply
"""Usage:

    .venv/bin/python curation/apply_lineage_decisions.py results/SYNONYMY_REVIEW_2026-09-27.xlsx \\
        --date 2026-10-05 --source "curator decision 2026-10-05" [--apply]

Dry run by default: prints every event, its public reason and every store row it touches,
then stops. With --apply, all events are applied in order and the store is written once.
A sheet that breaks a rule (a lineage decided twice, a merge into a removed lineage, a
remove without a note) is refused before anything is written, with the row named.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import lineage_history, synonymy_review as sr        # noqa: E402
from malavi_curation.config import load_config, repo_root                  # noqa: E402
from malavi_curation.release_store import (                                # noqa: E402
    TABLES, assign_ids, read_high_water, read_store, store_dir, write_store,
)
from review_synonymies import read_sheet                                   # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sheet", type=Path, help="The filled-in review sheet (.xlsx or .csv).")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="ISO date of the curators' decision (default: today).")
    parser.add_argument("--source", default=None,
                        help="Published SOURCE of every row (default: 'curator decision <date>').")
    parser.add_argument("--release", default=None,
                        help="The release the changes first appear in (default: the pinned one).")
    parser.add_argument("--actor", default="curators", help="Recorded as each row's _source.")
    parser.add_argument("--apply", action="store_true", help="Write. Otherwise a dry run.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = repo_root()
    release = args.release or str(load_config()["malaviR"]["release"])
    source = args.source or f"curator decision {args.date}"

    rows = read_sheet(args.sheet)
    store = read_store(store_dir(root))
    try:
        events, notes = sr.decisions_to_events(rows, store, date=args.date,
                                               release=release, source=source)
    except sr.ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not events:
        print("The sheet holds no merge, remove or rename decision; nothing to do.")
        return 0

    # Plan against a copy that already carries the earlier events, so a chain is checked
    # in the order it will be applied.
    rehearsal = {t: [dict(r) for r in rs] for t, rs in store.items()}
    touched = 0
    for ev, note in zip(events, notes):
        try:
            p = lineage_history.apply(rehearsal, ev, source_tag=args.actor)
        except lineage_history.HistoryError as exc:
            print(f"error: {note}: {exc}", file=sys.stderr)
            return 2
        print(f"== {note}")
        print(f"   reason (public): {p.history_row['REASON']}")
        for line in p.changes:
            print(f"   {line}")
        touched += sum(p.counts.values())
    print(f"\n{len(events)} event(s); {touched} store row(s) would change or be dropped; "
          f"{len(events)} lineage_history row(s) would be written for release {release}.")
    if not args.apply:
        print("[dry-run] nothing was written. Re-run with --apply.")
        return 0

    for ev in events:
        lineage_history.apply(store, ev, source_tag=args.actor)
    high_water = read_high_water(store_dir(root))
    for name in ("lineages", "alt_names", "lineage_history") + lineage_history.CITING_TABLES:
        if name in store:
            store[name] = assign_ids(TABLES[name], store[name], floor=high_water.get(name, 0))
    write_store(store_dir(root), store)
    print(f"applied. Keep {args.sheet.name} in results/ as the record of the vote.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
