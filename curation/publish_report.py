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
# @critical-flag publish_report.py "" --live
# @critical-flag publish_report.py "" --allow-bulk
# @critical-flag publish_report.py "" --standalone
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
import os
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation import curators, ledger, repo_state, report_delivery   # noqa: E402
from malavi_curation.config import load_config, repo_root             # noqa: E402
from malavi_curation.submission_id import (                           # noqa: E402
    ID_PATTERN, is_opaque, load_ledger as load_id_ledger,
)
from malavi_curation.report_delivery import (                         # noqa: E402
    DeliveryError, MailHeld, deliver, describe,
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


def excluded_directories() -> set:
    """Directory names ``submissions.exclude`` hides: test and demonstration submissions.

    Read the way enroll.py reads it (and accepting a bare string as well as an ``id:``
    entry), so the three programs that honor the list agree on what is in it.
    """
    hidden = set()
    for item in (load_config().get("submissions") or {}).get("exclude") or []:
        if isinstance(item, dict) and item.get("id"):
            hidden.add(str(item["id"]))
        elif isinstance(item, str):
            hidden.add(item)
    return hidden


def pending_reports(inbox: Path, entries, excluded: set) -> tuple:
    """What ``--all-pending`` may send: ``(targets, skipped)``.

    A report is pending only when ALL of these hold. Each one is a way this program used
    to email every curator something nobody should have received (found by review on
    2026-10-02, before the first unattended run):

    * the directory is not in ``submissions.exclude``. The test and demonstration
      submissions have rendered reports too, and the list that keeps them out of the
      public queue and out of the ledger was never consulted here;
    * the submission is in the review ledger. One that is not has not been enrolled, so
      its publish cannot be recorded and it would be sent again on every run;
    * its state is live. A withdrawn, declined, dormant or released submission has
      nothing left for a curator to decide;
    * no report has been published since its latest revision. A correction or a
      submitter's update records a revision; the re-screened report then goes out again,
      and the endpoint replaces the earlier file in place.

    ``skipped`` is ``[(directory, reason)]`` and is printed, so a report that is being
    held back is never held back silently. An explicit submission id on the command line
    bypasses all of this on purpose: sending one named report is a deliberate act.
    """
    targets, skipped = [], []
    for directory in sorted(inbox.iterdir()):
        if not directory.is_dir() or not (directory / REPORT_NAME).is_file():
            continue
        if directory.name in excluded:
            skipped.append((directory.name, "excluded in config (submissions.exclude)"))
            continue
        try:
            name, public_id = resolve(inbox, directory.name)
        except DeliveryError as exc:
            skipped.append((directory.name, str(exc)))
            continue
        entry = entries.get(public_id)
        if entry is None:
            skipped.append((public_id, "not in the review ledger; run enroll.py first"))
            continue
        if entry.state not in ledger.LIVE_STATES:
            skipped.append((public_id, f"its state is {entry.state}"))
            continue
        if report_is_current(entry):
            continue                        # the ordinary case; not worth a line each
        if not (inbox / name / REPORT_NAME).is_file():
            skipped.append((public_id, "has a newer revision but no report yet; screen it"))
            continue
        targets.append((name, public_id))
    return targets, skipped


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


def report_is_current(entry) -> bool:
    """Has a report gone out since the submission's latest revision?

    A revision (a correction recorded, or a submitter's update filed) changes what the
    report says, and the curators have to see the new one; the endpoint replaces the file
    in place, so the link in their inbox starts showing it. Until 2026-10-02 a submission
    was "published" for good after its first report, and a re-screened revision was sent
    only when somebody typed the command.
    """
    sent = [event for event in (entry.history or []) if event.get("event") == "report_published"]
    if not sent:
        return False
    last = sent[-1]           # history is appended in order; two in one second tie on time
    if "revision" in last:
        # Since 2026-10-02 a publish records which revision it reported.
        return int(last["revision"]) >= int(entry.revision or 1)
    # An older event: fall back to time. Not strictly later: the entry's first revision
    # and its first report can share a second, and that is not a new revision.
    revised = [str(revision.at or "") for revision in (entry.revisions or [])]
    return not revised or str(last.get("at") or "") >= max(revised)


def submitter_contact(inbox: Path, directory: str, entry) -> Dict[str, str]:
    """Name, address, arrival and revision for the status sheet; blanks when unknown.

    Read from the screen's submission.json, which is where notify_submitters.py reads the
    address from too. A missing file or address yields blanks rather than an error: the
    report still goes out, and the daily digest will show a row without an address.
    """
    out: Dict[str, str] = {}
    path = inbox / directory / "submission.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        submitter = data.get("submitter") or {}
        name = str(submitter.get("name") or "").strip()
        email = str(submitter.get("email") or "").strip()
        if name:
            out["submitter_name"] = name
        if "@" in email:
            out["submitter_email"] = email
    if entry is not None:
        out["received_at"] = str(entry.received_at or "")
        out["revision"] = str(entry.revision or 1)
    return out


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

    # The submitter's name and address go with the report so the endpoint can put them
    # on the status sheet (the verdict trigger and the inbox triage reach the submitter
    # through that row). Missing contact details are not an error here: the report is
    # still published, and the sheet row simply has no address.
    submitter = submitter_contact(inbox, directory, entries.get(public_id))
    # Whether the curators were ever TOLD of a report for this submission: the endpoint
    # words the notice "updated" only then, not merely because the file existed in Drive
    # (a held first notice leaves the file there; code review 2026-10-16, finding 10).
    announced_before = any(event.get("event") == "report_published"
                           and (int(event.get("notified") or 0) > 0
                                or event.get("notice") == "already_sent")
                           for event in (getattr(entries.get(public_id), "history", None) or []))
    result = deliver(public_id, report.read_bytes(), notify=notify, submitter=submitter,
                     assign=notify, announced_before=announced_before)
    print(f"    {result.action}: {result.url}")
    if result.assigned:
        print(f"    assigned to {result.assignee}")
    elif result.assignee:
        print(f"    already assigned to {result.assignee}")
    if result.sheet_error:
        print(f"    WARNING: the status sheet was not updated: {result.sheet_error}")
    if result.notified:
        print(f"    emailed {result.notified} curator(s)")
    elif notify:
        print("    WARNING: the endpoint reported that nobody was emailed — check the "
              "CURATORS Script Property of the report publisher (run showSettings there)")

    # Record it. A publish that happened but was not written down is indistinguishable
    # from one that never happened, the next time somebody asks why a curator is silent.
    entry = entries.get(public_id)
    if entry is not None:
        entry.history.append({
            "event": "report_published",
            "at": ledger.now_utc(),
            # Which revision this report described, so a later revision is known to
            # need a report of its own (see report_is_current).
            "revision": int(entry.revision or 1),
            "file_id": result.file_id,
            "url": result.url,
            "action": result.action,
            "notified": result.notified,
            "assignee": result.assignee,
        })
    else:
        print("    WARNING: no ledger entry, so this publish was NOT recorded and will "
              "be sent again on the next --all-pending run. Run enroll.py.")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission_id", nargs="*",
                        help="submission ids to publish; omit with --all-pending")
    parser.add_argument("--allow-bulk", action="store_true",
                        help="send more reports in one run than review.max_reports_per_run "
                             "allows (guard rails of 2026-10-16)")
    parser.add_argument("--standalone", action="store_true",
                        help="skip the check that this checkout is not behind its remote")
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
    parser.add_argument("--live", action="store_true",
                        help="with --check: also ask the deployed endpoint which code it "
                             "is running and whether it matches the repository's. Sends "
                             "one signed request; emails nobody and writes nothing")
    arguments = parser.parse_args(argv)

    print("== malavi_rebuild :: publish curator report ==")
    print(describe())

    if arguments.live and not arguments.check:
        parser.error("--live only makes sense with --check")

    if arguments.check and arguments.live:
        # The one question plain --check cannot answer: is the deployed script the one in
        # the repository? The request is the endpoint's read-only `describe` action.
        try:
            deployment = report_delivery.describe_live()
        except report_delivery.StaleDeployment as exc:
            print(f"REDEPLOY NEEDED: {exc}")
            return 2
        except DeliveryError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        try:
            registered = len(curators.active_curators())
        except Exception:                                            # noqa: BLE001
            registered = None            # the comparison is a convenience; skip it
        print(report_delivery.describe_deployment(deployment,
                                                  registry_curators=registered))
        # Exit 2, "a person is needed", when a redeploy is owed; the same convention the
        # screen and the verdict fetch use.
        return 0 if deployment.current else 2

    if arguments.check:
        # Deliberately does not contact the endpoint: plain --check must work on a machine
        # with no network and must never be the thing that emails a curator by accident.
        return 0

    inbox = submissions_inbox()

    if not arguments.submission_id and not arguments.all_pending:
        parser.error("give at least one submission id, or --all-pending")

    failures = 0
    held = 0
    sent = 0
    if not arguments.dry_run:
        # A stale checkout must not write the ledger (review of 2026-10-16, finding 17).
        try:
            repo_state.require_fresh("publish_report.py", standalone=arguments.standalone)
        except repo_state.StaleCheckout as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2
    try:
        with ledger.open_ledger(inbox, write=not arguments.dry_run) as entries:
            if arguments.all_pending:
                targets, skipped = pending_reports(inbox, entries, excluded_directories())
                for what, why in skipped:
                    print(f"  not sending {what}: {why}")
                if not targets:
                    print("\nnothing pending: every report that may be sent has been.")
                    return 0
                print(f"\n{len(targets)} report(s) pending")
                # The bulk refusal (guard rails of 2026-10-16): more reports than a real
                # day produces means something upstream re-flagged old submissions as
                # pending, and every one would email every curator.
                limit = int((load_config().get("review") or {})
                            .get("max_reports_per_run", 6) or 6)
                allowed = arguments.allow_bulk or repo_state.bulk_allowed_by_environment()
                if len(targets) > limit and not allowed:
                    print(f"\nREFUSED: {len(targets)} reports pending is more than "
                          f"review.max_reports_per_run ({limit}); nothing was sent. Check "
                          f"the list above; if they are all genuinely new, run again with "
                          f"--allow-bulk.", file=sys.stderr)
                    for what, _public_id in targets:
                        print(f"    {what}", file=sys.stderr)
                    return 2
            else:
                targets = [resolve(inbox, given) for given in arguments.submission_id]

            # One report failing must not lose the record of the ones already sent. The
            # ledger is saved when this block exits cleanly, so a delivery error is caught
            # per report and counted, not allowed to unwind the whole run: otherwise every
            # report sent before the failure would be sent again, as "updated", next time.
            for directory, public_id in targets:
                try:
                    if publish_one(inbox, directory, public_id,
                                   notify=not arguments.no_notify,
                                   dry_run=arguments.dry_run, entries=entries):
                        sent += 1
                except MailHeld as exc:
                    # Not a fault: the mail mode is dry, or the endpoint is holding mail.
                    # Nothing was recorded, so the report goes out on the next run that
                    # can send (guard rails of 2026-10-16).
                    held += 1
                    print(f"    HELD: {public_id} was not published: {exc}")
                except DeliveryError as exc:
                    failures += 1
                    print(f"    ERROR: {public_id} was not published: {exc}",
                          file=sys.stderr)

    except DeliveryError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print(f"\npublished {sent} report(s)"
          + (f"; {failures} could not be published." if failures else ".")
          + (f" {held} held (mail not sent; retried next run)." if held else ""))
    if failures:
        return 1
    # Held reports want a person to set the mail mode, not a retry of the program.
    return 2 if held else 0

if __name__ == "__main__":
    raise SystemExit(main())
