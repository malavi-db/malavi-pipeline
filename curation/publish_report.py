#!/usr/bin/env python3
# @title Publish a rendered curator report to Drive and tell the curators
# @purpose Take report.pdf out of the gitignored intake tree, put it where a curator can
#   read it, email them that it is there, and record in the ledger that it happened.
# @why check_template.py has always written the report to a directory on BIOMIX that no
#   curator has an account on, so the submission email's promise that "the report follows
#   separately" was never true. This is the step that makes it true.
# @input curation/intake/submissions/<submission_id>/report.pdf
# @input config/project.yml (google.report_endpoint, google.report_secret_file)
# @output a PDF in the MalAvi curator-reports Drive folder
# @output curation/intake/submissions/review_ledger.json (a report_published history entry)
# @program python3
# @critical-var ENDPOINT_ENV
# @critical-var SECRET_ENV
# @critical-flag publish_report.py "" --dry-run
# @critical-flag publish_report.py "" --check
"""Send curator reports to Drive.

What this program is
--------------------
The last mile of the outbound half of the submission loop. ``fetch_submissions.py`` brings
a submission in, ``check_template.py`` screens it and renders a report, and until now the
report stopped there — on a filesystem the curators cannot reach.

Why it is not a service account
-------------------------------
It was designed as one, and the design was tested before it was built. A service account
cannot create a Drive file owned by a consumer Google account: it has no storage quota of
its own, and there is no Shared Drive without Workspace. The full reasoning, and the exact
error, are in :mod:`malavi_curation.report_delivery`. Delivery goes through an Apps Script
web app that runs as ``malaviadmin@gmail.com`` instead.

Three properties worth knowing
------------------------------
**Re-publishing is safe and is the intended way to correct a report.** The file is named
from the submission id, and the endpoint updates the existing file in place rather than
creating a second one, so a link already sitting in a curator's inbox keeps working and
starts showing the corrected version. Publishing twice does not produce two reports.

**It refuses to publish anything that is not a PDF**, including a zero-length or truncated
one, and checks the stored file's checksum against what it sent. A half-rendered report
reaching a curator is worse than no report: the curator-report renderer has a documented
history of sections degrading to *nothing* rather than to an error.

**It records what it did.** A ``report_published`` entry goes into the submission's ledger
history with the Drive file id, so "was this curator ever actually sent anything?" is a
question the ledger can answer.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation import ledger                                    # noqa: E402
from malavi_curation.config import load_config, repo_root             # noqa: E402
from malavi_curation.submission_id import (                           # noqa: E402
    ID_PATTERN, is_opaque, load_ledger as load_id_ledger,
)
from malavi_curation.report_delivery import (                         # noqa: E402
    DeliveryError, deliver, describe,
)

REPORT_NAME = "report.pdf"


def submissions_inbox() -> Path:
    """Where submissions live, per config, matching every other curation program."""
    config = load_config()
    return repo_root() / (config.get("submissions", {}) or {}).get(
        "inbox_dir", "curation/intake/submissions")


def resolve(inbox: Path, given: str) -> tuple:
    """(directory name, opaque public id) for whatever the operator typed.

    These are two different strings and conflating them was a real bug. The directory is
    `<timestamp>_<slugified submitter name>` -- it carries a person's NAME. The public id
    is `MALAVI-SUB-YYYY-NNNNNN` and is what the review ledger is keyed by.

    The directory is only ever used to find files on this machine. The public id is what
    goes into the Drive filename, the curator email subject, and the ledger -- because a
    report called `20260727T233146_Jane_Smith_report.pdf` sitting in a shared folder, and
    an unrecallable email subject naming her, tells every curator that Jane Smith has
    unpublished data on a particular parasite. That is exactly what the opaque id exists
    to prevent, and publish_report.gs claims in as many words that the subject "does not
    name the submitter".
    """
    ids = (load_id_ledger(inbox).get("ids") or {})
    if is_opaque(given):
        for directory, record in ids.items():
            if record.get("id") == given:
                return directory, given
        raise DeliveryError(f"No submission has the id {given}.")

    record = ids.get(given)
    if not record or not record.get("id"):
        raise DeliveryError(
            f"{given} has no minted public id yet, so it cannot be published without "
            f"putting the directory name -- which carries the submitter's own name -- "
            f"into Drive and into a curator's inbox. Run fetch_submissions.py or "
            f"build_site_feeds.py first; either mints one.")
    return given, record["id"]


def find_report(inbox: Path, submission_id: str) -> Path:
    """The rendered report for one submission, or a message saying why there is not one.

    The distinction between "no such submission" and "that submission has no report yet"
    matters: the first is a typo, the second means ``check_template.py`` has not been run,
    and they need different actions.
    """
    directory = inbox / submission_id
    if not directory.is_dir():
        raise DeliveryError(
            f"No submission directory at {directory}. Check the id — "
            f"`ls {inbox}` lists them.")
    report = directory / REPORT_NAME
    if not report.is_file():
        raise DeliveryError(
            f"{submission_id} has no {REPORT_NAME}. Run check_template.py on it first; "
            f"that is what renders the report.")
    return report


def _display(path: Path) -> str:
    """A path shortened against the repository when it is inside it, absolute otherwise.

    An inbox can be configured anywhere, and a cosmetic shortening must never be the thing
    that raises. `Path.relative_to` throws on a path outside the root, so this is the one
    place that difference is handled rather than assumed away.
    """
    try:
        return str(path.relative_to(repo_root()))
    except ValueError:
        return str(path)


def already_published(entries, submission_id: str) -> Optional[str]:
    """The Drive file id from a previous publish of this submission, if there was one.

    Used only to tell the operator that this is a re-publish. It is not a guard: correcting
    a report and sending it again is a normal, expected thing to do.
    """
    entry = entries.get(submission_id)
    if entry is None:
        return None
    for event in reversed(entry.history or []):
        if event.get("event") == "report_published":
            return event.get("file_id")
    return None


def publish_one(inbox: Path, directory: str, public_id: str, *, notify: bool,
                dry_run: bool, entries) -> bool:
    """Publish one report. Returns True when something was actually sent."""
    report = find_report(inbox, directory)
    size_kb = report.stat().st_size / 1024
    previous = already_published(entries, public_id)

    print(f"  {public_id}")
    print(f"    report : {_display(report)} ({size_kb:.0f} KB)")
    if previous:
        print(f"    note   : already published once (file {previous}); this replaces it "
              f"in place, at the same link")

    if dry_run:
        print("    [dry-run] nothing sent")
        return False

    result = deliver(public_id, report.read_bytes(), notify=notify)
    print(f"    {result.action}: {result.url}")
    if result.notified:
        print(f"    emailed {result.notified} curator(s)")
    elif notify:
        print("    WARNING: the endpoint reported that nobody was emailed — check "
              "CURATORS in publish_report.gs")

    # Record it. A publish that happened but was not written down is indistinguishable
    # from one that never happened, the next time somebody asks why a curator is silent.
    entry = entries.get(public_id)
    if entry is not None:
        entry.history.append({
            "event": "report_published",
            "at": ledger.now_utc(),
            "file_id": result.file_id,
            "url": result.url,
            "action": result.action,
            "notified": result.notified,
        })
    else:
        print("    WARNING: no ledger entry, so this publish was NOT recorded and will "
              "be sent again on the next --all-pending run. Run enroll.py.")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission_id", nargs="*",
                        help="submission ids to publish; omit with --all-pending")
    parser.add_argument("--all-pending", action="store_true",
                        help="publish every submission that has a report.pdf and no "
                             "report_published entry in the ledger")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be sent; send nothing")
    parser.add_argument("--no-notify", action="store_true",
                        help="write the file to Drive without emailing the curators, for "
                             "re-sending a report during debugging")
    parser.add_argument("--check", action="store_true",
                        help="report whether delivery is configured, and stop")
    arguments = parser.parse_args(argv)

    print("== malavi_rebuild :: publish curator report ==")
    print(describe())

    if arguments.check:
        # Deliberately does not contact the endpoint: --check must work on a machine with
        # no network and must never be the thing that emails a curator by accident.
        return 0

    inbox = submissions_inbox()

    if not arguments.submission_id and not arguments.all_pending:
        parser.error("give at least one submission id, or --all-pending")

    try:
        with ledger.open_ledger(inbox, write=not arguments.dry_run) as entries:
            if arguments.all_pending:
                # Resolve every candidate to its public id BEFORE testing whether it has
                # been published. Testing the directory name against a ledger keyed by
                # minted ids always missed, so every report was re-sent on every run and
                # the "no ledger entry" note made it look intentional.
                candidates = []
                for directory in sorted(inbox.iterdir()):
                    if not directory.is_dir() or not (directory / REPORT_NAME).is_file():
                        continue
                    try:
                        candidates.append(resolve(inbox, directory.name))
                    except DeliveryError as exc:
                        print(f"  skipping {directory.name}: {exc}")
                targets = [(d, pid) for d, pid in candidates
                           if not already_published(entries, pid)]
                if not targets:
                    print("\nnothing pending: every rendered report has been published.")
                    return 0
                print(f"\n{len(targets)} report(s) pending")
            else:
                targets = [resolve(inbox, given) for given in arguments.submission_id]

            sent = 0
            for directory, public_id in targets:
                if publish_one(inbox, directory, public_id,
                               notify=not arguments.no_notify,
                               dry_run=arguments.dry_run, entries=entries):
                    sent += 1

    except DeliveryError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print(f"\npublished {sent} report(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
