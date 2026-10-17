#!/usr/bin/env python3
# @title Record a lineage-history event and apply it to the store
# @purpose Write one lineage_history row (sequence_updated / renamed / merged_into /
#          removed) and make the change it describes in every store table, in one
#          logged step. Dry run by default.
# @why The 2026-09-24 policy lets curators complete partial lineages, merge trivial
#      synonyms and remove lineages that should not be in MalAvi, but only with a
#      public trail. One command per decision keeps the trail and the data in step.
# @input data/records/*.csv
# @output data/records/*.csv
# @program python3
# @critical-flag record_lineage_event.py "" --apply
"""Record what changed about a lineage, and change it.

    # Rule 1: a complete barcode for a partial lineage, keeping the old name
    .venv/bin/python curation/record_lineage_event.py sequence_updated H_CCF2 \\
        --sequence-file ccf2_full.fasta --accession PQ123456 \\
        --source MALAVI-SUB-2026-000011 --reason "complete 479 bp sequence from ..."

    # Rule 2: fold a one-record synonym into the complete lineage
    .venv/bin/python curation/record_lineage_event.py merged_into L_ACCBRE01 --into L_ACCBRE02 \\
        --source "curators 2026-09-24" --reason "identical over the overlap; 02 is complete"

    # Rule 2: a lineage that should not be in MalAvi
    .venv/bin/python curation/record_lineage_event.py removed TURGUL01 \\
        --source "curators 2026-09-24" --reason "..."

    # a plain rename to a free name
    .venv/bin/python curation/record_lineage_event.py renamed OLDNAME01 --into NEWNAME01 ...

Nothing is written without ``--apply``. The dry run prints the history row and every
table row the event would touch. ``--date`` defaults to today; ``--release`` defaults to
the pinned release in config/project.yml, meaning "the next edition after this one".
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import lineage_history                        # noqa: E402
from malavi_curation.config import load_config, repo_root           # noqa: E402
from malavi_curation.release_store import (                         # noqa: E402
    TABLES, assign_ids, read_high_water, read_store, store_dir, write_store,
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("event", choices=lineage_history.EVENTS)
    parser.add_argument("lineage", help="The lineage the event is about (its current name).")
    parser.add_argument("--into", metavar="NAME", default="",
                        help="renamed: the new name. merged_into: the lineage to fold into.")
    parser.add_argument("--sequence", default="",
                        help="sequence_updated: the replacement sequence, as text.")
    parser.add_argument("--sequence-file", type=Path, default=None,
                        help="sequence_updated: read the replacement sequence from a FASTA "
                             "or plain-text file (first record).")
    parser.add_argument("--accession", default="",
                        help="sequence_updated: the GenBank accession the sequence came from.")
    parser.add_argument("--reason", required=True, help="Why. Published in the history table.")
    parser.add_argument("--source", required=True,
                        help="Submission id, accession, person or 'curator decision'. Published.")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="ISO date of the decision (default: today).")
    parser.add_argument("--release", default=None,
                        help="The release the change first appears in (default: the "
                             "pinned release, i.e. the next edition).")
    parser.add_argument("--actor", default="maintainer", help="Recorded as the row's _source.")
    parser.add_argument("--apply", action="store_true", help="Write. Otherwise a dry run.")
    return parser.parse_args(argv)


def read_sequence(path: Path) -> str:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    body = []
    for ln in lines:
        if ln.startswith(">"):
            if body:
                break
            continue
        body.append(ln)
    return "".join(body).replace(" ", "").upper()


def main(argv=None) -> int:
    args = parse_args(argv)
    root = repo_root()
    cfg = load_config()
    release = args.release or str(cfg["malaviR"]["release"])
    sequence = args.sequence.replace(" ", "").upper()
    if args.sequence_file:
        sequence = read_sequence(args.sequence_file)

    ev = lineage_history.Event(
        event=args.event, lineage=args.lineage, new_name=args.into, new_sequence=sequence,
        accession=args.accession, date=args.date, reason=args.reason, source=args.source,
        release=release)

    store = read_store(store_dir(root))
    try:
        p = lineage_history.plan(store, ev)
    except lineage_history.HistoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"== {args.event} {ev.lineage}" + (f" -> {ev.new_name}" if ev.new_name else ""))
    print("history row:")
    for col in TABLES["lineage_history"].columns:
        value = p.history_row[col]
        if col.endswith("SEQUENCE") and len(value) > 40:
            value = f"{value[:20]}...{value[-12:]} ({len(value)} chars)"
        print(f"  {col:13s} {value}")
    print("store changes:")
    for line in p.changes:
        print(f"  {line}")
    if not args.apply:
        print("\n[dry-run] nothing was written. Re-run with --apply.")
        return 0

    lineage_history.apply(store, ev, source_tag=args.actor)
    high_water = read_high_water(store_dir(root))
    for name in ("lineages", "alt_names", "lineage_history") + lineage_history.CITING_TABLES:
        if name in store:
            store[name] = assign_ids(TABLES[name], store[name], floor=high_water.get(name, 0))
    write_store(store_dir(root), store)
    print(f"\napplied. {sum(p.counts.values())} store row(s) changed or dropped; one "
          f"lineage_history row written. It ships in release {release} and appears in "
          f"that edition's report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
