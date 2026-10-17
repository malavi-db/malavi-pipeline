#!/usr/bin/env python3
# @title Write the edition report comparing this database with the last release
# @purpose Compare the record store with a previously published edition of MalAvi and
#          write the two documents that accompany a release: the internal record and the
#          public release notes, each as HTML and PDF.
# @why build_release.py writes these whenever it builds a release, but the report is also
#      wanted on its own -- to see what an edition would say before committing to it, and
#      to re-render an edition's report after a wording or layout fix without rebuilding
#      the release the world already has.
# @input data/records/*.csv
# @input docs/assets/downloads/tables/grand_lineage_summary_<previous release>.csv
# @output data/releases/release_notes_<release>.html (+ .pdf)
# @output data/releases/release_notes_<release>_public.html (+ .pdf)
# @output data/releases/release_diff_<release>.json
# @program python3
# @program weasyprint
# @critical-flag edition_report.py "" --release
# @critical-flag edition_report.py "" --previous
# @critical-flag edition_report.py "" --audience
"""Write the report that accompanies an edition of MalAvi.

    # what the next edition's report would say, from the store as it stands
    .venv/bin/python curation/edition_report.py --release 2026-08-14 \\
        --previous docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv

Two documents are written by default. The **internal** one is the printed record: it
carries the approval block, every data fault the build found, the detail of removed
records, and the lines somebody signs. The **public** one carries what changed in the
database and nothing about who was asked or what was wrong -- a data fault names a study,
and through it the people who contributed the records, and MalAvi does not publish that.

Both land in ``data/releases/``, which is gitignored. Publishing the public one is a
separate, deliberate act: copy it into ``docs/`` when the edition ships.

**This does not consult the approval gate**, because it builds nothing. It reads the store
as it stands and says what an edition built from it would change. ``build_release.py``
remains the only path that produces a release, and it writes these same documents itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import release_diff, release_notes  # noqa: E402
from malavi_curation.config import repo_root  # noqa: E402
from malavi_curation.release_store import read_store, store_dir  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", default=date.today().isoformat(),
                        help="The release tag this report describes. Defaults to today.")
    parser.add_argument("--previous", type=Path, required=True, metavar="CSV",
                        help="The previous edition's grand_lineage_summary CSV. Its "
                             "sibling tables are read from the same directory.")
    parser.add_argument("--destination", type=Path, default=None,
                        help="Where the documents are written. Defaults to data/releases/.")
    parser.add_argument("--audience", choices=(*release_notes.AUDIENCES, "both"),
                        default="both",
                        help="Which document to write. Defaults to both.")
    parser.add_argument("--json", action="store_true",
                        help="Print the comparison as JSON instead of prose.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = repo_root()

    if not args.previous.is_file():
        print(f"error: {args.previous} is not a file. Point --previous at a published "
              f"edition's grand_lineage_summary CSV.", file=sys.stderr)
        return 1

    store = read_store(store_dir(root))
    if not any(store.values()):
        print(f"error: {store_dir(root)} holds no records. Seed the store first "
              f"(RUNBOOK step 3b).", file=sys.stderr)
        return 1

    previous = release_diff.load_previous_edition(args.previous)
    current = release_diff.current_edition(store, args.release)
    edition = release_diff.compare(previous, current)

    if args.json:
        print(json.dumps(edition, indent=2, sort_keys=True))
        return 0

    destination = args.destination or root / "data" / "releases"
    audiences = (release_notes.AUDIENCES if args.audience == "both"
                 else (args.audience,))

    # The comparison is written beside the documents so the numbers in them can be
    # checked against a machine-readable record without rebuilding anything.
    destination.mkdir(parents=True, exist_ok=True)
    diff_path = destination / f"release_diff_{args.release}.json"
    diff_path.write_text(json.dumps(edition, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")

    # warnings=None means "not checked", which is the truth here: the data faults are
    # found by the release build and this program does not build. Passing () would have
    # the document print "The build flagged nothing" on a page somebody signs.
    written = release_notes.write_documents(
        edition, destination, args.release,
        warnings=None,
        report_json=str(diff_path),
        audiences=audiences)

    # The store's own warnings are not available here: this reads the store rather than
    # building a release, and the faults are found by the build. Say so, so that a blank
    # "Faults to look at" section in a report produced this way is never read as a clean
    # bill of health.
    print(f"Edition {args.release} against {previous.label}")
    print(f"  {edition['lineages']['added_count']} lineage(s) added, "
          f"{edition['lineages']['removed_count']} retired")
    print(f"  {len(edition['references']['added'])} study/studies added")
    hosts = edition["tables"]["host_records"]
    print(f"  {hosts['added']} host record(s) added, {hosts['removed']} removed, "
          f"{hosts['modified']} changed")
    print(f"  {edition['summary_columns']['changed_lineages']} lineage(s) with a "
          f"corrected derived value")
    print(f"\n  diff:  {diff_path}")
    for audience in audiences:
        entry = written[audience]
        print(f"  {audience}: {entry['html']}")
        if entry["pdf"]:
            print(f"  {' ' * len(audience)}  {entry['pdf']}")
    if written["pdf_unavailable"]:
        print("\n  NOTE: WeasyPrint is not installed, so only HTML was written.")
    if "internal" in audiences:
        print("\n  The 'Faults to look at' section is empty in a report produced this "
              "way: the data faults are found by the build. Build the release to get "
              "them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
