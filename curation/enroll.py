#!/usr/bin/env python3
# @title Enroll fetched submissions into the review ledger
# @purpose Give every screened submission a ledger entry, so a curator's verdict has
#   something to attach to and the review clocks have something to run on.
# @why fetch_submissions.py and check_template.py never told the ledger a submission
#   existed, so the ledger stayed empty and every verdict would have been filed as
#   "unknown submission".
# @input curation/intake/submissions/<dir>/screen.json
# @input curation/intake/submissions/submission_ids.json
# @output curation/intake/submissions/review_ledger.json
# @program python3
# @critical-var INBOX_DIR
# @critical-flag enroll.py "" --dry-run
"""Put every fetched submission into the review ledger.

Runs after the screen and before the feeds, once per submissions job. It is the step that
turns "a folder on disk" into "a thing a curator has been asked about".

**Excluded submissions are excluded here too.** ``config/project.yml`` lists submission
directories that must not reach the public queue — the test and demo submissions built
while the form was being wired up. Enrolling one would put an invented study into the
review ledger, and from there into the decision record and into a release. The exclusion
list is read from the same place the feeds read it, so the two cannot drift.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import enrollment, ledger  # noqa: E402
from malavi_curation.config import load_config, repo_root  # noqa: E402
from malavi_curation.submission_id import load_ledger  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be enrolled; write nothing")
    parser.add_argument("--track", default="A",
                        help="which intake track these arrived by (default A, the public "
                             "form)")
    arguments = parser.parse_args(argv)

    config = load_config()
    submissions = config.get("submissions") or {}
    inbox = repo_root() / submissions.get("inbox_dir", "curation/intake/submissions")

    excluded = {str(item.get("id")) for item in (submissions.get("exclude") or [])
                if isinstance(item, dict) and item.get("id")}

    print("== malavi_rebuild :: enroll ==")
    if not inbox.is_dir():
        print(f"no inbox at {inbox}; nothing to enroll.")
        return 0

    # The opaque identifier is minted by the fetch, not here. A submission with no minted
    # id has not been through the intake and enrolling it would invent an identity for it.
    minted = load_ledger(inbox)["ids"]

    created = advanced = skipped = 0
    with ledger.open_ledger(inbox, write=not arguments.dry_run) as entries:
        for sub_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
            if sub_dir.name in excluded:
                print(f"  [excluded ] {sub_dir.name}")
                skipped += 1
                continue

            record = minted.get(sub_dir.name)
            if not record or not record.get("id"):
                print(f"  [no id    ] {sub_dir.name}  not minted by the intake; skipped")
                skipped += 1
                continue

            outcome = enrollment.enroll_one(entries, record["id"], sub_dir,
                                            track=arguments.track)
            marker = "enrolled " if outcome["created"] else "present  "
            print(f"  [{marker}] {outcome['submission_id']}  {outcome['state']:<17} "
                  f"{outcome['note']}")
            created += int(outcome["created"])
            advanced += int(not outcome["created"] and bool(outcome["note"]))

    print(f"\n{created} enrolled, {advanced} updated, {skipped} skipped.")
    if arguments.dry_run:
        print("[dry-run] nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
