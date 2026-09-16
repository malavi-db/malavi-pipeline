#!/usr/bin/env python3
# @title Import the Lund master database into the record store
# @purpose Reproduce the flat MalAvi release tables from MALAVI.sqlite, compare them with
#          the pinned release to prove the export rules, merge them into data/records/
#          keeping every existing record id and provenance, replay the correction log,
#          and write a committed report of exactly what was imported.
# @why The record store was seeded from the 2026-03-23 release. Staffan Bensch then sent
#      the database itself with every waiting-list dataset added, and from that import
#      on the store is where MalAvi is curated. The database is never published; only
#      what this script derives from it is.
# @input MALAVI.sqlite (private; gitignored)
# @input data/records/*.csv
# @input data/corrections.csv
# @input docs/assets/downloads/tables/*_<pinned release>.csv
# @output data/records/*.csv (rewritten, with --apply)
# @output data/master_exports/<release>/*.csv (gitignored, with --apply)
# @output data/master_imports/import_<release>.json (committed, with --apply)
# @program python
# @critical-var UNPUBLISHED_YEAR
# @critical-var EXPECTED_SEQUENCE_LENGTH
# @critical-flag import_master_db.py "" --apply
"""Import the Lund master database into the record store.

    # what the import would do, writing nothing
    .venv/bin/python curation/import_master_db.py --db MALAVI.sqlite --release 2026-09-15

    # do it
    .venv/bin/python curation/import_master_db.py --db MALAVI.sqlite --release 2026-09-15 --apply

Read the reproduction check first. It exports the database and compares each table with
the pinned release the store was seeded from: rows only in the export are what Staffan
added or edited since that release, rows only in the release are what he removed or
edited. If those numbers look like a rule is wrong rather than like edits -- hundreds of
rows differing on one derived column -- stop and fix the rule in ``master_db.py``.

Then read the merge: kept / changed / added / removed per table, and the corrections
replay. Nothing is written without ``--apply``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation.config import load_config, repo_root  # noqa: E402
from malavi_curation.master_db import (  # noqa: E402
    STORE_TABLE_ORDER, compare_rows, export_tables, merge_store, replay_corrections,
    sha256_of, write_export,
)
from malavi_curation.release_seed import (  # noqa: E402
    _SOURCE_FILES, read_release_csv, release_table_path,
)
from malavi_curation.release_store import (  # noqa: E402
    TABLES, read_store, store_dir, write_store,
)
from malavi_curation.store_corrections import log_path, read_log  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, type=Path,
                        help="The MALAVI.sqlite file Staffan sent. Read-only.")
    parser.add_argument("--release", required=True,
                        help="Release tag, YYYY-MM-DD, that new rows will first appear "
                             "in. Written into _added.")
    parser.add_argument("--compare-with", metavar="RELEASE", default=None,
                        help="A release whose tables sit in docs/assets/downloads/tables, "
                             "to prove the export rules against. Defaults to the "
                             "pinned malaviR release in config/project.yml.")
    parser.add_argument("--export-dir", type=Path, default=None,
                        help="Where the exported tables are written with --apply. "
                             "Defaults to data/master_exports/<release>/.")
    parser.add_argument("--apply", action="store_true",
                        help="Write the store, the export and the report. Without it "
                             "nothing is written.")
    parser.add_argument("--json", action="store_true",
                        help="Print the full report as JSON instead of prose.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = repo_root()
    config = load_config()
    db_path = args.db if args.db.is_absolute() else (root / args.db)
    if not db_path.is_file():
        print(f"error: {db_path} does not exist", file=sys.stderr)
        return 1
    try:
        dt.date.fromisoformat(args.release)
    except ValueError:
        print(f"error: --release must be YYYY-MM-DD, got {args.release!r}",
              file=sys.stderr)
        return 1

    report = {
        "release": args.release,
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "database": {"path": str(db_path), "sha256": sha256_of(db_path),
                     "bytes": db_path.stat().st_size,
                     "modified": dt.datetime.fromtimestamp(
                         db_path.stat().st_mtime, dt.timezone.utc
                     ).isoformat(timespec="seconds")},
        "applied": False,
    }

    # ---- 1. export --------------------------------------------------------------
    exported, findings = export_tables(db_path)
    report["exported"] = {name: len(rows) for name, rows in exported.items()}
    report["findings"] = findings

    # ---- 2. reproduction check against the pinned release ------------------------
    compare_release = args.compare_with or str(config["malaviR"]["release"])
    downloads = root / "docs" / "assets" / "downloads" / "tables"
    reproduction = {"release": compare_release, "tables": {}}
    for name in STORE_TABLE_ORDER:
        path = release_table_path(downloads, _SOURCE_FILES[name], compare_release)
        if not path.is_file():
            reproduction["tables"][name] = {"error": f"not found: {path.name}"}
            continue
        reproduction["tables"][name] = compare_rows(
            TABLES[name], read_release_csv(path), exported[name])
    report["reproduction"] = reproduction

    # ---- 3. merge into the store ---------------------------------------------------
    existing = read_store(store_dir(root))
    merged, merge_report = merge_store(existing, exported, args.release)
    report["merge"] = merge_report

    # ---- 4. replay the corrections already decided here -----------------------------
    replay = replay_corrections(merged, read_log(log_path(root)))
    report["corrections_replayed"] = replay

    # ---- 5. write, or say what would be written -------------------------------------
    export_dir = args.export_dir or (root / "data" / "master_exports" / args.release)
    report_path = root / "data" / "master_imports" / f"import_{args.release}.json"
    if args.apply:
        write_export(export_dir, exported)
        write_store(store_dir(root), merged)
        report["applied"] = True
        report["written"] = {"store": str(store_dir(root)), "export": str(export_dir),
                             "report": str(report_path)}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(_trim_for_record(report), handle, indent=1, ensure_ascii=False)
            handle.write("\n")

    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        print_prose(report, export_dir, report_path)
    return 0


def _trim_for_record(report: dict) -> dict:
    """The committed report keeps every count and every named row, but not the
    per-row 'added' lists, which run to thousands of entries and are recoverable from
    the store's _added column."""
    out = json.loads(json.dumps(report))
    for table in out.get("merge", {}).values():
        table["added"] = f"{len(table.get('added', []))} rows; see _added={out['release']}"
    return out


def print_prose(report: dict, export_dir: Path, report_path: Path) -> None:
    print("== malavi_rebuild :: import_master_db ==")
    db = report["database"]
    print(f"database: {db['path']}\n  sha256 {db['sha256']}\n  {db['bytes']:,} bytes, "
          f"modified {db['modified']}")
    print(f"release tag for new rows: {report['release']}")

    print("\n-- exported --")
    for name in STORE_TABLE_ORDER:
        print(f"  {name:16s} {report['exported'][name]:>7,} rows")

    rep = report["reproduction"]
    print(f"\n-- reproduction check against release {rep['release']} --")
    print("   (only-export = added or edited since; only-release = removed or edited)")
    for name in STORE_TABLE_ORDER:
        entry = rep["tables"][name]
        if "error" in entry:
            print(f"  {name:16s} {entry['error']}")
            continue
        flag = "identical" if entry["identical"] else ""
        print(f"  {name:16s} release {entry['n_before']:>6,}  export {entry['n_after']:>6,}"
              f"  only-export {entry['n_only_after']:>5,}  only-release "
              f"{entry['n_only_before']:>5,}  {flag}")

    print("\n-- findings in the database (reported, not changed) --")
    for finding in report["findings"]:
        detail = finding.get("detail")
        if isinstance(detail, list):
            shown = ", ".join(str(item) for item in detail[:12])
            more = f" ... and {len(detail) - 12} more" if len(detail) > 12 else ""
            detail = f"{len(detail)}: {shown}{more}"
        note = f"  ({finding['note']})" if finding.get("note") else ""
        print(f"  {finding['kind']}: {detail}{note}")

    print("\n-- merge into the store --")
    for name in STORE_TABLE_ORDER:
        m = report["merge"][name]
        print(f"  {name:16s} store {m['n_existing']:>6,} -> {m['n_merged']:>6,}   "
              f"kept {m['n_kept_identical']:>6,}  changed {m['n_changed']:>4,}  "
              f"added {m['n_added']:>5,}  removed {m['n_removed']:>4,}")
    for name in STORE_TABLE_ORDER:
        m = report["merge"][name]
        if m["changed"]:
            print(f"\n  changed rows in {name} (first 15):")
            for change in m["changed"][:15]:
                cols = "; ".join(f"{column}: {v['was']!r} -> {v['now']!r}"
                                 for column, v in change["columns"].items())
                print(f"    {change['record_id']} {change['key']}: {cols[:200]}")
        if m["removed"]:
            print(f"\n  removed rows in {name} (first 15):")
            for removed in m["removed"][:15]:
                print(f"    {removed['record_id']} {removed['key']}")

    print("\n-- corrections replayed --")
    for entry in report["corrections_replayed"]:
        print(f"  {entry['correction_id']} {entry['table']} {entry['selector']} "
              f"-> {entry['column']}={entry['new_value']!r}: {entry['status']}"
              f" ({entry['rows_changed']} row(s); originally {entry['originally_changed']})")

    if report["applied"]:
        print(f"\nwrote the store, {export_dir}, and {report_path}")
    else:
        print("\ndry run: nothing written. Add --apply to write the store, the export "
              f"under {export_dir}, and the report {report_path}.")


if __name__ == "__main__":
    sys.exit(main())
