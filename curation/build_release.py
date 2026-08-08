#!/usr/bin/env python3
# @title Build a MalAvi release from the record store
# @purpose Command-line front end to release_build: regenerate the Grand Lineage Summary,
#          emit the five tables and the alignment, pack the ZIP, and print what changed
#          against the release being superseded.
# @why The store became authoritative when it was seeded and nothing could get a release
#      back out of it. This is the step that makes the store's authority real.
# @input data/records/*.csv
# @input reference/country_regions.csv
# @output data/releases/MalAvi_<release>.zip
# @output data/releases/release_report_<release>.json
# @program python3
# @critical-flag build_release.py "" --release
# @critical-flag build_release.py "" --diff-against
# @critical-flag build_release.py "" --dry-run
"""Build a release, and say plainly what it changes.

Run it with ``--diff-against`` pointed at the previous release's Grand Lineage Summary and
read the report before shipping anything. The derived columns are regenerated from the
records every time, so a rebuild silently corrects wherever the previous summary had gone
stale -- and a correction nobody saw is indistinguishable from a bug nobody caught.

    # what would change, writing nothing
    .venv/bin/python curation/build_release.py --dry-run \\
        --diff-against docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv

    # build it
    .venv/bin/python curation/build_release.py --release 2026-08-07 \\
        --diff-against docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation.config import repo_root  # noqa: E402
from malavi_curation.release_build import (  # noqa: E402
    build_release, derive_summary, diff_against_release,
)
from malavi_curation.release_store import read_store, store_dir  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=date.today().isoformat(),
                        help="Release tag, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--destination", type=Path, default=None,
                        help="Where the ZIP is written. Defaults to data/releases/.")
    parser.add_argument("--diff-against", type=Path, default=None, metavar="CSV",
                        help="A previous release's grand_lineage_summary CSV. Every "
                             "derived value that differs is reported.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Derive and diff, but write nothing.")
    parser.add_argument("--json", action="store_true",
                        help="Print the full report as JSON instead of prose.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = repo_root()

    store = read_store(store_dir(root))
    if not any(store.values()):
        print(f"error: {store_dir(root)} holds no records. Seed the store first "
              f"(RUNBOOK step 3b).", file=sys.stderr)
        return 1

    destination = args.destination or root / "data" / "releases"

    if args.dry_run:
        summary = derive_summary(store)
        report = {"release": args.release, "dry_run": True,
                  "rows": {"grand_lineage_summary": len(summary),
                           **{name: len(rows) for name, rows in store.items()}}}
    else:
        report = build_release(store, args.release, destination)

    if args.diff_against:
        report["diff"] = diff_against_release(derive_summary(store), args.diff_against)

    if not args.dry_run:
        report_path = destination / f"release_report_{args.release}.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
        report["report"] = str(report_path)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    # Prose, because this is read by a person deciding whether to ship.
    print(f"Release {report['release']}" + ("  (dry run, nothing written)"
                                            if args.dry_run else ""))
    for name, count in sorted(report["rows"].items()):
        print(f"  {name:24s} {count:6d} rows")
    if not args.dry_run:
        print(f"  alignment                {report['alignment_records']:6d} sequences")
        print(f"\n  archive: {report['archive']}")
        print(f"  report:  {report['report']}")

    for warning in report.get("warnings", []):
        print(f"\n  WARNING: {warning}")

    diff = report.get("diff")
    if diff:
        print(f"\nAgainst {Path(diff['reference']).name}:")
        print(f"  {diff['lineages_compared']} lineages compared, "
              f"{diff['changed_lineages']} with at least one derived value changed")
        if diff["only_in_build"]:
            print(f"  {len(diff['only_in_build'])} new lineage(s): "
                  f"{', '.join(diff['only_in_build'][:10])}")
        if diff["only_in_reference"]:
            print(f"  {len(diff['only_in_reference'])} lineage(s) no longer present: "
                  f"{', '.join(diff['only_in_reference'][:10])}")
        for column, entry in sorted(diff["by_column"].items(),
                                    key=lambda kv: -kv[1]["changed"]):
            example = entry["examples"][0]
            print(f"    {column:30s} {entry['changed']:5d} changed "
                  f"(e.g. {example['lineage']}: {example['was']!r} -> {example['now']!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
