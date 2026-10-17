#!/usr/bin/env python3
# @title Record an independent review of the guarded files in curation/REVIEWS.md
# @purpose Compute the two hashes that identify the code as reviewed (the Apps Script
#   bundle, and the guarded Python programs and modules concatenated) and append one line
#   to curation/REVIEWS.md naming the reviewer and the findings record. A test fails until
#   the current hashes are on record; the Actions workflow runs that test before a live
#   cycle; build_bundle.py says whether the bundle it wrote is reviewed.
# @why "Independent review before implementation" is a rule that lapses unless something
#   checks it (plan review of 2026-10-16, finding 14). This is the something.
# @input curation/apps_script/*.gs
# @input the files in GUARDED_PYTHON
# @output curation/REVIEWS.md (one line appended)
# @program python3
# @critical-var GUARDED_PYTHON
"""Record a review.

    .venv/bin/python ops/record_review.py --reviewer "review-code agent" \\
        --findings results/REVIEW_2026-10-16_guardrails.md --note "19 findings, all fixed"
    .venv/bin/python ops/record_review.py --show       # the current hashes, nothing written

The guarded files are the ones that can send mail or publish to a public destination.
Changing any of them changes a hash, and the change must be reviewed again before it is
committed, pasted or run live.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REVIEWS = ROOT / "curation" / "REVIEWS.md"
BUNDLE = ROOT / "curation" / "apps_script" / "dist" / "malavi_curation.gs"

# Every Python file that can cause an email or a public push. Order matters for the hash;
# keep it sorted. Add a file here when it gains either power.
GUARDED_PYTHON = (
    ".github/workflows/intake_cycle.yml",
    "curation/apps_script/build_bundle.py",
    "curation/fetch_verdicts.py",
    "curation/intake_cycle.py",
    "curation/notify_submitters.py",
    "curation/privacy_scan.py",
    "curation/promote.py",
    "curation/publish_report.py",
    "curation/push_status.py",
    "curation/src/malavi_curation/public_feeds.py",
    "curation/src/malavi_curation/repo_state.py",
    "curation/src/malavi_curation/report_delivery.py",
    "ops/public_audit.py",
    "ops/record_review.py",
    "publish/push_feeds.sh",
    "publish/push_pipeline.sh",
    "publish/push_site.sh",
)


def bundle_hash() -> str:
    """The hash of the Apps Script SOURCES the bundle is built from (every .gs file in
    the directory, builders included), not of dist/: an edited source with a stale,
    previously reviewed dist/ must not pass (code review 2026-10-16, finding 17)."""
    digest = hashlib.sha256()
    for path in sorted(BUNDLE.parent.parent.glob("*.gs")):
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def guarded_hash() -> str:
    digest = hashlib.sha256()
    for relative in GUARDED_PYTHON:
        path = ROOT / relative
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def recorded_pairs() -> List[Tuple[str, str]]:
    """Every (bundle hash, guarded hash) pair on record."""
    pairs = []
    if not REVIEWS.is_file():
        return pairs
    for line in REVIEWS.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| 20"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3:
            pairs.append((cells[1], cells[2]))
    return pairs


def is_reviewed(bundle: Optional[str] = None, guarded: Optional[str] = None) -> bool:
    """Whether THIS pair of hashes was reviewed together (one row carries both)."""
    bundle = bundle or bundle_hash()
    guarded = guarded or guarded_hash()
    return (bundle, guarded) in recorded_pairs()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reviewer", default="", help="who or what reviewed (required to record)")
    parser.add_argument("--findings", default="",
                        help="the findings record, a path under results/ (required to record)")
    parser.add_argument("--note", default="", help="one short phrase")
    parser.add_argument("--show", action="store_true", help="print the hashes and whether "
                                                             "they are on record; write nothing")
    arguments = parser.parse_args(argv)

    bundle, guarded = bundle_hash(), guarded_hash()
    print(f"bundle  {bundle}\nguarded {guarded}\n"
          f"{'REVIEWED' if is_reviewed(bundle, guarded) else 'NOT REVIEWED'}")
    if arguments.show:
        return 0
    if not arguments.reviewer or not arguments.findings:
        print("to record a review give --reviewer and --findings", file=sys.stderr)
        return 2
    line = (f"| {date.today().isoformat()} | {bundle} | {guarded} | {arguments.reviewer} | "
            f"{arguments.findings} | {arguments.note} |")
    text = REVIEWS.read_text(encoding="utf-8") if REVIEWS.is_file() else ""
    if not text.rstrip().endswith("|"):
        text = text.rstrip("\n") + "\n"
    REVIEWS.write_text(text.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
    shown = REVIEWS.relative_to(ROOT) if ROOT in REVIEWS.parents else REVIEWS
    print(f"recorded in {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
