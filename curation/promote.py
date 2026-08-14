#!/usr/bin/env python3
# @title Apply the review clocks and write the committed decision record
# @purpose Run the two clocks the review ledger keeps -- the 24-hour publish hold and the
#   60-day awaiting-submitter timeout -- apply what they make due, and report what needs a
#   person.
# @why due_actions() has always reported what is ripe and nothing ever applied it, so a
#   submission approved on Monday was still merely "approved" in December and an abandoned
#   one held its reserved lineage names forever.
# @input curation/intake/submissions/review_ledger.json
# @output curation/intake/submissions/review_ledger.json (state changes)
# @output data/decisions.json (the committable decision record)
# @program python3
# @critical-var STALE_DAYS
# @critical-flag promote.py "" --dry-run
"""Run the review clocks, and write the record that outlives the ledger.

**What this applies, and what it refuses to.** ``ledger.due_actions()`` returns proposals.
This program applies exactly one kind of them:

* ``timeout_dormant`` — a submission has sat waiting on its submitter past the configured
  timeout. It goes ``dormant`` and its reserved lineage names go back. This is applied
  automatically because nobody disagrees with a clock: the submitter was asked, the time
  elapsed, and holding a name for work that will never arrive is the harm the timeout
  exists to prevent.

It deliberately does **not** apply ``release_eligible``. That proposal means the publish
hold has elapsed with no standing objection, so the submission *may* go into a release —
not that it has. The ``released`` state means "published in a release", and only
``build_release`` can make that true. Marking it here would put a claim in the ledger, in
the decision record, and on the public queue that no release supports, and ``released`` is
terminal, so there would be no way back. Release-eligible submissions are therefore
reported, and picked up by the release build.

**Why a separate program from the verdict fetch.** They write the same file and are
therefore serialized by the ledger's lock, but they answer to different things: the fetcher
runs when a curator acts, the promoter runs when time passes. Folding the clocks into the
fetcher would mean a timeout only fired on days somebody happened to submit a verdict.

**The decision record.** Written here rather than by the fetcher because it is a view of
the whole ledger, not of one response, and because this is the job that runs on a schedule
whether or not anything happened. It holds identifiers, dates, curator ids and
closed-vocabulary reason codes and no free prose, which is what lets it be committed to a
repository while the ledger it derives from stays in the gitignored intake tree. That
split is the whole reason a withdrawn submission can be erased and "what did we decide
about that, and when?" stays answerable.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import ledger  # noqa: E402
from malavi_curation.config import load_config, repo_root  # noqa: E402

# Where the committable record goes. Under data/ rather than the intake tree precisely
# because it is meant to be committed; the intake tree is gitignored.
DECISION_RECORD = "data/decisions.json"

# How long a live submission may sit with nothing happening to it before the promoter
# names it for a person to chase. The ledger refuses to invent this number -- it is a
# policy decision -- so it is read from config/project.yml and only defaulted here if the
# config is silent, with a warning.
DEFAULT_STALE_DAYS = 30


def write_decision_record(path: Path, entries: Dict[str, ledger.Entry],
                          dry_run: bool = False) -> bool:
    """Write the decision record, returning whether the file changed.

    Compared before writing so a scheduled run that decided nothing produces no commit.
    A daily "no change" commit is not harmless: it buries the commits that do mean
    something and it re-dates a file whose dates are its point.
    """
    record = ledger.decision_record(entries)
    payload = json.dumps({"schema": 1, "decisions": record},
                         indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    if path.is_file() and path.read_text() == payload:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what is due; change nothing")
    parser.add_argument("--now", default=None,
                        help="evaluate the clocks at this ISO 8601 UTC moment instead of "
                             "now, for testing")
    parser.add_argument("--stale-days", type=int, default=None,
                        help="override review.stale_review_days from config")
    arguments = parser.parse_args(argv)

    config = load_config()
    review = config.get("review") or {}
    root = repo_root()
    inbox = root / (config.get("submissions", {}) or {}).get(
        "inbox_dir", "curation/intake/submissions")

    # `is not None`, not `or`: --stale-days 0 is a legitimate request (treat everything as
    # stale, e.g. to see the whole board at once) and 0 is falsy, so `or` silently
    # substituted the configured value for it.
    stale_days = (arguments.stale_days if arguments.stale_days is not None
                  else review.get("stale_review_days"))
    if stale_days is None:
        stale_days = DEFAULT_STALE_DAYS
        print(f"NOTE: config has no review.stale_review_days; using {stale_days} days.")

    print("== malavi_rebuild :: promote ==")
    moment = arguments.now or ledger.now_utc()
    print(f"evaluating the clocks at {moment}\n")

    applied = 0
    reported: List[ledger.DueAction] = []

    with ledger.open_ledger(inbox, write=not arguments.dry_run) as entries:
        # An empty ledger still gets a decision record written, at the bottom of this
        # block. It used to return here instead, which is why data/decisions.json did not
        # exist at all until 2026-08-13: nothing has been enrolled yet, so every run took
        # this path. An absent record is indistinguishable from "this program has never
        # run", and the record is the ONLY committed thing that will resolve a submission
        # id later -- the ledger and the id map are both gitignored. Establishing it while
        # it is empty means the first real decision arrives as a diff to a tracked file
        # rather than as a new file nobody has reviewed the shape of.
        if not entries:
            print("the review ledger is empty; nothing is due.")

        due = ledger.due_actions(entries, now=arguments.now, config=config)

        for proposal in due:
            entry = entries[proposal.submission_id]

            if proposal.action == "timeout_dormant":
                try:
                    # transition() re-checks everything at the write, so a submitter who
                    # replied between the scan and here -- which moved the submission out
                    # of awaiting_submitter -- makes this refuse rather than go dormant.
                    ledger.transition(entry, "dormant", actor="promoter", at=moment,
                                      reason="submitter_unresponsive", config=config)
                except ledger.LedgerError as exc:
                    print(f"  [refused  ] {proposal.submission_id}  {exc}")
                    continue
                applied += 1
                print(f"  [dormant  ] {proposal.submission_id}  {proposal.because}")
                print(f"               reserved names released: "
                      f"{', '.join(ledger.agreed_names(entry)) or 'none'}")

            elif proposal.action == "release_eligible":
                # Reported, never applied. See this module's docstring.
                reported.append(proposal)
                print(f"  [ready    ] {proposal.submission_id}  {proposal.because}")

            elif proposal.action == "malformed":
                reported.append(proposal)
                print(f"  [MALFORMED] {proposal.submission_id}  {proposal.because}")

        # Submissions with no clock on them at all, which the timeout deliberately does not
        # cover: a held submission nobody followed up sits holding its names indefinitely.
        for proposal in ledger.stale_live(entries, days=int(stale_days), now=arguments.now):
            reported.append(proposal)
            print(f"  [stale    ] {proposal.submission_id}  {proposal.because}")

        if entries and not due and not reported:
            print("  nothing is due.")

        # Taken inside the lock, so the record describes the ledger as it is after this
        # run's own changes rather than as it was before them.
        record_path = root / DECISION_RECORD
        record_changed = write_decision_record(record_path, entries,
                                               dry_run=arguments.dry_run)

    print(f"\n{applied} state change(s) applied, {len(reported)} item(s) reported.")
    print(f"decision record: {'updated' if record_changed else 'unchanged'} "
          f"({DECISION_RECORD})")

    if arguments.dry_run:
        print("[dry-run] nothing was written.")
        return 0

    # Exit 2 means "a person should look at this" -- a malformed timestamp, or a submission
    # that has been sitting untouched. Same convention as check_template.py and
    # fetch_verdicts.py, so a scheduled job can tell it apart from a job that could not run.
    needs_person = [p for p in reported if p.action in ("malformed", "stale")]
    if needs_person:
        print(f"{len(needs_person)} item(s) need a maintainer's attention.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
