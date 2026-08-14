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
# @input curation/intake/submissions/review_ledger.json
# @critical-flag build_release.py "" --release
# @critical-flag build_release.py "" --diff-against
# @critical-flag build_release.py "" --dry-run
# @critical-flag build_release.py "" --i-am-overriding-the-approval-gate
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
from malavi_curation import ledger, release_gate  # noqa: E402
from malavi_curation.config import load_config, repo_root  # noqa: E402
from malavi_curation.release_build import (  # noqa: E402
    build_release, derive_summary, diff_against_release,
)
from malavi_curation import release_diff, release_notes  # noqa: E402
from malavi_curation.release_store import read_store, store_dir  # noqa: E402


# The disposition reason recorded against every submission a release publishes.
#
# It is a CLOSED VOCABULARY -- ledger.DISPOSITION_REASON_CODES -- because the reason is
# exported to the committed decision record, which must contain no unpublished science.
# This was prose ("published in release <date>") until 2026-08-10, which ledger.transition
# refuses; the rehearsal did not catch it because it passed no reason at all, and "" is
# always valid. The release tag is not lost: it goes in the release report's `approval`
# block and in the history event transition() writes.
RELEASED_REASON = "released_in_build"


def submissions_inbox(root: Path) -> Path:
    """Where the review ledger lives. Its absence is handled, not assumed away."""
    return root / "curation" / "intake" / "submissions"


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
    parser.add_argument("--actor", default="maintainer",
                        help="Who is running the build. Recorded in the ledger against "
                             "every submission this release marks released.")
    parser.add_argument("--no-notes", dest="notes", action="store_false", default=True,
                        help="Do not write the edition report. It is written by default "
                             "whenever --diff-against is given and a release is actually "
                             "built, because an edition that ships without a record of "
                             "what it changed cannot be checked afterwards.")
    parser.add_argument("--i-am-overriding-the-approval-gate", dest="override_gate",
                        action="store_true",
                        help="Build even though records in the store cannot be shown to "
                             "have been approved. Recorded in the release report. Nothing "
                             "is marked released.")
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

    # ---------------------------------------------------------------------------------
    # THE APPROVAL GATE. Nothing below this runs for a store carrying records no curator
    # approved -- see release_gate, and the note on --i-am-overriding-the-approval-gate.
    # ---------------------------------------------------------------------------------
    inbox = submissions_inbox(root)
    entries = ledger.load(inbox) if ledger.ledger_path(inbox).is_file() else None
    gate = release_gate.check(store, entries)

    refusals: list = []
    if gate.ok and gate.publishing:
        # Rehearsed before anything is written, so a release the ledger would refuse to
        # record is never built. See plan_release_transitions.
        #
        # Rehearsed on a --dry-run too, and that is the point of it. The rehearsal is what
        # checks the 24-hour publish hold and re-checks the embargoes; skipping it on a dry
        # run meant the dry run reported a clean release for submissions the real build was
        # about to refuse. A dry run exists to be read before shipping, so it has to be
        # able to say no. It still writes nothing -- plan_release_transitions only plans.
        refusals = release_gate.plan_release_transitions(
            entries or {}, gate.publishing, actor=args.actor,
            reason=RELEASED_REASON, config=load_config())

    if not gate.ok or refusals:
        print(f"error: this release may not be built -- {len(gate.violations)} "
              f"provenance problem(s), {len(refusals)} submission(s) the ledger would "
              f"refuse to record as released:", file=sys.stderr)
        for line in release_gate.describe(gate, refusals):
            print(line, file=sys.stderr)
        if not args.override_gate:
            print("\nNothing was written. Fix the ledger, or the store's provenance, and "
                  "run again.", file=sys.stderr)
            return 2
        # The override exists because a maintainer holding a broken ledger and a deadline
        # will otherwise edit this file under pressure, and an escape hatch that is used
        # once and recorded beats a code change nobody reviews. It is deliberately
        # unpleasant to type, and it is recorded in the release report.
        print("\nOVERRIDDEN. Building anyway, and recording that this was overridden.",
              file=sys.stderr)

    # Derived once and reused by the diff and by the edition report below. build_release
    # derives its own copy for the tables it writes; this one exists so that a dry run and
    # a comparison do not each pay for another pass over 18,000 records.
    summary = derive_summary(store)

    if args.dry_run:
        report = {"release": args.release, "dry_run": True,
                  "rows": {"grand_lineage_summary": len(summary),
                           **{name: len(rows) for name, rows in store.items()}}}
    else:
        report = build_release(store, args.release, destination)

    # What the release was allowed to carry, recorded beside what it carries. A release
    # report that cannot say which submissions it published, and on whose approval, is not
    # a record of anything.
    report["approval"] = {
        "seed_rows": gate.exempt_rows,
        "submissions_published": list(gate.publishing),
        "violations": [v.describe() for v in gate.violations],
        "gate_overridden": bool(args.override_gate and (gate.violations or refusals)),
    }

    # The full edition comparison: not only the derived columns diff_against_release
    # reports, but the lineages, studies, records, hosts and countries this edition adds
    # and retires. It is the data behind the printed edition report, and it is computed
    # once and shared so the document and the JSON cannot disagree about a number.
    edition = None
    if args.diff_against:
        report["diff"] = diff_against_release(summary, args.diff_against)
        edition = release_diff.compare(
            release_diff.load_previous_edition(args.diff_against),
            release_diff.current_edition(store, args.release, summary=summary))
        report["edition_diff"] = edition

    if not args.dry_run:
        report_path = destination / f"release_report_{args.release}.json"
        report["report"] = str(report_path)

        # The edition report, written before the JSON so that the JSON records where the
        # documents went. Both go to the release directory, which is gitignored: the
        # internal document names studies, their faults and the submissions this release
        # published, and none of that should reach a tracked directory by accident.
        if edition is not None and args.notes:
            report["notes"] = release_notes.write_documents(
                edition, destination, args.release,
                approval=report["approval"], build=report,
                warnings=report.get("warnings", []),
                report_json=str(report_path))

        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")

    # ---------------------------------------------------------------------------------
    # Mark them released LAST, once the ZIP and the record of it are both on disk.
    #
    # "released" means published, so the ledger must not claim it before the thing exists
    # -- the same reason promote.py refuses to apply release_eligible itself. But it must
    # not claim it before the *record* exists either. This ran before the report and the
    # edition notes were written until 2026-08-11, so a rendering failure aborted the run
    # with the ZIP already on disk and the submissions already marked released; the
    # re-run then found admissibility == RELEASED for all of them, published nothing, and
    # wrote a report saying "this release publishes no submission" -- which was false and
    # was by then the only surviving record of what the release carried.
    # ---------------------------------------------------------------------------------
    if not args.dry_run and gate.ok and gate.publishing:
        with ledger.open_ledger(inbox) as live:
            for submission_id in gate.publishing:
                ledger.transition(live[submission_id], "released", args.actor,
                                  reason=RELEASED_REASON, config=load_config())
        report["approval"]["marked_released"] = list(gate.publishing)
        # Rewritten so the report on disk records the transition it just made.
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")

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
        notes = report.get("notes") or {}
        for audience in ("internal", "public"):
            written = notes.get(audience)
            if written:
                print(f"  notes ({audience}): {written['html']}")
                if written["pdf"]:
                    print(f"                     {written['pdf']}")
        if notes.get("pdf_unavailable"):
            print("  NOTE: WeasyPrint is not installed, so only the HTML edition report "
                  "was written.")

    approval = report["approval"]
    print(f"\n  approval: {approval['seed_rows']} seed row(s); "
          f"{len(approval['submissions_published'])} submission(s) published"
          + (f" -> {', '.join(approval['submissions_published'])}"
             if approval["submissions_published"] else ""))
    if approval["gate_overridden"]:
        print("  APPROVAL GATE OVERRIDDEN -- nothing was marked released")

    for warning in report.get("warnings", []):
        print(f"\n  WARNING: {warning}")

    if edition is not None:
        hosts = edition["tables"]["host_records"]
        print(f"\nAgainst edition {edition['editions']['previous']['label']}:")
        print(f"  {edition['lineages']['added_count']} lineage(s) added, "
              f"{edition['lineages']['removed_count']} retired")
        print(f"  {len(edition['references']['added'])} study/studies added")
        print(f"  {hosts['added']} host record(s) added, {hosts['removed']} removed, "
              f"{hosts['modified']} changed")
        print(f"  {len(edition['hosts']['new_species'])} host species and "
              f"{len(edition['hosts']['new_countries'])} country/countries new to MalAvi")

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
