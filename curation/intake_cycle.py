#!/usr/bin/env python3
# @title Run the intake chain once, in order, and say whether a person is needed
# @purpose Run every routine step between "a submission arrived on the form" and "the
#   submitter was told what was decided" as one command: fetch, screen, feeds, enroll,
#   publish reports, fetch verdicts, promote, notify. Preview by default; --live to act.
# @why Each of these steps was a command the maintainer typed by hand, in an order written
#   down only in the runbook, and nothing logged what was run. None of them needs a
#   person on the normal path (results/PROTOCOL_REVIEW_2026-10-02.md, section 5).
# @input curation/intake/submissions/ (the intake tree, the ledger, the id map)
# @input config/project.yml (via the programs this runs)
# @output logs/intake_cycle_<UTC timestamp>.log (every command, its exit code, its output)
# @output whatever the programs it runs write; nothing is written by this program itself
#   except the log and, on a live run, two git commits of generated files
# @program python3
# @program git
# @critical-var SCREENED_MARKER
# @critical-var INCOMPLETE_MARKER
# @critical-var ATTENTION_REPEAT_DAYS
# @critical-var FEED_FILES
# @critical-var DECISION_RECORD
# @critical-var CORRECTION_WAITING_MARKER
# @critical-var OUTPUT_TAIL_LINES
# @critical-flag intake_cycle.py "" --live
# @critical-flag intake_cycle.py "" --no-email
# @critical-var MAIL_MODE_ENV
"""Run the intake chain once.

What this is
------------
The runbook's table "A. A submission arrives and is reviewed" as a program. It runs the
existing programs, in the runbook's order, each as its own process with its own command
line, and reads nothing but their exit codes. It decides nothing: every rule (who is a
curator, whether a hold has elapsed, whether a name is taken) stays in the program that
already enforces it, and each of those re-checks its rules at the moment it writes.

    1  fetch_submissions.py            bring in what the form has received
       fetch_updates.py                file submitters' updates as new revisions
    2  check_template.py <directory>   screen each directory not screened yet
    3  build_site_feeds.py             the public queue and contributors feeds
       build_name_reservations.py      the reserved-names feed
       build_status_board.py           the curators' board (encrypted; optional)
    4  git commit (the feeds)          only when one of them changed
    5  publish/push_feeds.sh           put the feeds on the live site
    6  enroll.py                       put each screened submission in the ledger
    7  publish_report.py --all-pending send every unsent report to the curators
    8  fetch_verdicts.py               apply what the curators recorded on the form
    9  apply_corrections.py            REPORT ONLY: is a lead-approved correction waiting?
    10 promote.py                      run the clocks; then commit data/decisions.json
    11 notify_submitters.py            tell submitters whose hold has elapsed
    12 git commit (the feeds)          again, if steps 6 to 11 changed them

Preview is the default
----------------------
Without ``--live`` every step runs in its own dry-run mode and nothing is sent, committed
or published, and no submission, ledger or feed is written. It is not free of side
effects: ``push_feeds.sh --dry-run`` refreshes the local clone of the site repository, and
the programs create their lock and log files. And it can under-report: two of the
programs return 0 from a dry run before working out whether a person is needed. The steps that have no dry-run (the screen, the commits) are not
run; the preview says what they would have been run on. A live run emails curators and
submitters and publishes to the public site, so it has to be asked for by name.

Exit codes, the same convention as the programs it runs
-------------------------------------------------------
    0  every step ran clean
    2  the cycle ran, and at least one step wants a person (a response that could not be
       parsed, a name collision, a correction waiting, a stale submission)
    1  a step could not run; the steps after it were not attempted

On a live run that ends 2 or 1, one email goes to the lead curators through the report
endpoint, carrying the steps that need a person and the end of their output. A run that
ends 0 sends nothing: silence means it ran clean, and the log proves it.

What it does not do
-------------------
* It does not record a correction (step 9 only reports that one is waiting): recording
  it is followed by a change to the submitted data that a person makes (RUNBOOK 3da).
* It does not close a submission, lift an embargo, ingest, or build a release.
* It does not push the private repository. The two commits it makes stay local until the
  maintainer pushes; the public site is updated by ``push_feeds.sh``, which is separate.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation import repo_state, report_delivery             # noqa: E402
from malavi_curation.config import load_config, repo_root            # noqa: E402

# A directory counts as screened when it holds this file. ``report.html`` is the last
# thing check_template.py writes before the PDF, on both of its paths (a workbook screen
# and the paper-only report), so its presence means the screen ran to the end.
# ``screen.json`` is NOT the marker: it is written before the checks and the report, and
# a screen that crashed after it would be taken for one that finished.
SCREENED_MARKER = "report.html"

# Written by fetch_submissions.py into a directory whose files did not all download. Such
# a directory is not screened: without its workbook the screen would report it to the
# curators as a paper sent with no template.
INCOMPLETE_MARKER = ".download_incomplete"

# The three generated feeds the cycle commits when they change (RUNBOOK section 4).
FEED_FILES = ("docs/assets/data/queue.json",
              "docs/assets/data/contributors.json",
              "docs/assets/data/reserved_names.json",
              # The curators' board, encrypted. It exists only once a password is set up.
              "docs/assets/data/board.json")

# THE REVIEW STATE (added 2026-10-14; plan of 2026-10-02, items 4.3 and 4.4).
#
# Everything the cycle needs to continue from lives under the inbox and, until
# 2026-10-14, only on the machine that ran it: the review ledger, the id map, the
# applied-verdicts record, what was fetched, and each submission's screen outputs. For
# the cycle to run anywhere (a scheduled GitHub Actions job, a second machine) that state
# has to travel, and the private repository is where it travels. The files below are the
# whole of it; a submitter's uploads (workbooks, papers, supplements) are NOT state: they
# are re-fetched from Drive and never committed. The inbox stays gitignored; these paths
# are force-added by name, so a glob can never sweep an upload in.
STATE_TOP_FILES = ("review_ledger.json", "submission_ids.json", "verdicts_applied.json",
                   "fetched.json", "fetched_updates.json")
STATE_DIR_FILES = ("metadata.json", "screen.json", "checks.json", "submission.json",
                   "report.html", "report.pdf")
STATE_DIR_GLOBS = ("update_*.json",)

# The settings, from config/project.yml (``intake:``), each overridable by an
# environment variable so a CI job can turn them on without editing tracked configuration
# that a hand run on BIOMIX also reads.
# Since 2026-10-16 the settings reader lives in malavi_curation.repo_state, where the
# programs this cycle runs can also read it (they refuse to write from a stale checkout).
STATE_SETTINGS = repo_state.STATE_SETTINGS
state_settings = repo_state.state_settings

# The mail mode (report_delivery.MAIL_MODE_ENV). A live cycle REQUIRES it to be set, to
# live, redirect or dry: "unset" is the fail-safe default for a program run by hand, but a
# cycle that sends nothing while reporting a live run would be a quiet failure, so the
# cycle makes the person say which they mean (guard rails of 2026-10-16).
MAIL_MODE_ENV = report_delivery.MAIL_MODE_ENV


def state_files(inbox: Path, root: Path) -> List[str]:
    """The state files that exist right now, as paths relative to the repository."""
    found: List[Path] = []
    for name in STATE_TOP_FILES:
        if (inbox / name).is_file():
            found.append(inbox / name)
    for directory in sorted(p for p in inbox.iterdir() if p.is_dir()
                            and not p.name.startswith("_") and not p.name.startswith(".")):
        for name in STATE_DIR_FILES:
            if (directory / name).is_file():
                found.append(directory / name)
        for pattern in STATE_DIR_GLOBS:
            found.extend(sorted(directory.glob(pattern)))
    out = []
    for path in found:
        try:
            out.append(str(path.resolve().relative_to(root.resolve())))
        except ValueError:
            out.append(str(path))
    return out


# The committed record of decisions that promote.py writes (RUNBOOK section 3d).
DECISION_RECORD = "data/decisions.json"

# The line apply_corrections.py prints when a lead-approved correction is waiting to be
# recorded. Read from its output because that program is the only thing that knows; a test
# holds this string to the one in apply_corrections.py so the two cannot drift apart.
CORRECTION_WAITING_MARKER = "Re-run with --apply to record these."

# How much of a step's output goes into the email. The log always holds all of it.
OUTPUT_TAIL_LINES = 40

OK = "ok"
ATTENTION = "needs a person"
FAILED = "failed"
NOT_RUN = "not run"
PREVIEW_ONLY = "not run in a preview"

# What each program's exit codes mean. Anything not listed is a failure. Each line was
# checked against the program's own return statements (review of 2026-10-02):
#   fetch: 2 = a file could not be downloaded (retried next run)
#   fetch_updates: 2 = an update needs a person (words only; a closed submission; an unknown id)
#   check_template: 2 = a check found something blocking; 3 = no template, paper-only report
#   build_name_reservations: 2 = a name collision between submissions
#   fetch_verdicts / promote: 2 = their own "a person should look"
#   notify_submitters: 2 = a confirmation refused because two submissions claim one name
# publish_report.py --all-pending: 2 = reports held by the mail guard, or a bulk refusal.
#
# A PREVIEW CAN UNDER-REPORT. fetch_verdicts.py and promote.py return 0 from a dry run
# before they work out their exit 2, so "the preview ran clean" does not promise a live
# run will.
EXIT_MEANINGS: Dict[str, Dict[int, str]] = {
    "fetch": {0: OK, 2: ATTENTION},
    "updates": {0: OK, 2: ATTENTION},
    "screen": {0: OK, 2: ATTENTION, 3: OK},
    "feeds": {0: OK},
    "names": {0: OK, 2: ATTENTION},
    "board": {0: OK},
    "commit_feeds": {0: OK},
    "push_feeds": {0: OK},
    "enroll": {0: OK},
    "reports": {0: OK, 2: ATTENTION},   # 2 = reports held (mail mode) or a bulk refusal
    "verdicts": {0: OK, 2: ATTENTION},
    "corrections": {0: OK},
    "promote": {0: OK, 2: ATTENTION},
    "commit_decisions": {0: OK},
    "notify": {0: OK, 2: ATTENTION},
    "status": {0: OK},                  # push_status.py: 0 also when there is no sheet yet
    "endpoint": {0: OK},
    "pull": {0: OK},
    "commit_state": {0: OK, 1: OK},     # 1 is `git diff --cached --quiet` saying "changes"
    "push_state": {0: OK},
}

# A failure in one of these does not stop the cycle. The feeds are a publication of state,
# not a source of it: enroll, verdicts, the clocks and the notices read the ledger, and
# must not wait on GitHub being reachable (malavi_curation.public_feeds, rule 2: a publish
# never fails the thing that called it). A screen that cannot run leaves one directory
# unscreened and must not hold up everybody else's reports and verdicts. Either still ends
# the cycle with exit 1 and an email.
NON_HALTING = ("screen", "feeds", "names", "board", "commit_feeds", "push_feeds",
               "push_state", "status")

# A standing condition (a stale submission, a name two submissions claim, a correction
# waiting) recurs on every run until a person acts. The same email is not sent again
# until this many days have passed; a different one is sent at once.
ATTENTION_REPEAT_DAYS = 7
ATTENTION_STATE_FILE = "intake_cycle_last_attention.json"

Runner = Callable[[Sequence[str]], Tuple[int, str]]


@dataclass
class StepResult:
    """One step of the cycle: what was run, how it ended, and what it printed."""

    key: str
    title: str
    command: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    status: str = NOT_RUN
    output: str = ""
    note: str = ""

    @property
    def shown_command(self) -> str:
        return " ".join(self.command) if self.command else "(nothing to run)"


def _run_process(command: Sequence[str]) -> Tuple[int, str]:
    """The real runner: one process, output and errors captured together, in order."""
    try:
        done = subprocess.run(list(command), cwd=str(repo_root()), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              check=False)
    except OSError as exc:
        # A missing program or a missing execute bit. 127 is the shell's own code for
        # "command not found", and it is in no step's table, so the step is a failure.
        return 127, f"could not be started: {exc}"
    return done.returncode, done.stdout or ""


def submissions_inbox() -> Path:
    """The intake directory, as configured (the same lookup the other programs use)."""
    configuration = load_config()
    return repo_root() / (configuration.get("submissions") or {}).get(
        "inbox_dir", "curation/intake/submissions")


def excluded_directories() -> set:
    """Directory names ``submissions.exclude`` hides (test and demonstration submissions)."""
    hidden = set()
    for item in (load_config().get("submissions") or {}).get("exclude") or []:
        if isinstance(item, dict) and item.get("id"):
            hidden.add(str(item["id"]))
        elif isinstance(item, str):
            hidden.add(item)
    return hidden


def unscreened_directories(inbox: Path, excluded: Sequence[str] = ()) -> List[Path]:
    """Submission directories that still need check_template.py run on them.

    Not an excluded one (a test submission is never screened by the cycle), not one whose
    download is incomplete, and not one that already holds the report the screen writes
    last.
    """
    if not inbox.is_dir():
        return []
    skip = set(excluded)
    return sorted(
        directory for directory in inbox.iterdir()
        if directory.is_dir() and not directory.name.startswith((".", "_"))
        and directory.name not in skip
        and not (directory / INCOMPLETE_MARKER).exists()
        and not (directory / SCREENED_MARKER).exists())


def incomplete_directories(inbox: Path) -> List[Path]:
    """Directories fetch_submissions.py marked as missing a file."""
    if not inbox.is_dir():
        return []
    return sorted(directory for directory in inbox.iterdir()
                  if directory.is_dir() and (directory / INCOMPLETE_MARKER).exists())


def received_stamp(directory_name: str) -> str:
    """The arrival timestamp a directory name begins with, without the submitter's name.

    A directory is ``<timestamp>_<submitter's name>``. Step titles go into the email, and
    the email must not carry the name; the timestamp identifies the submission as well.
    """
    return directory_name.split("_", 1)[0]


class Cycle:
    """One run of the chain. Holds the results so the summary and the email can read them."""

    def __init__(self, live: bool, runner: Runner = _run_process,
                 inbox: Optional[Path] = None, python: str = sys.executable,
                 log: Optional[Callable[[str], None]] = None,
                 excluded: Optional[Sequence[str]] = None,
                 state: Optional[Dict[str, bool]] = None,
                 root: Optional[Path] = None,
                 describe: Optional[Callable[[], Any]] = None) -> None:
        self.live = live
        self.runner = runner
        # Asks the deployed endpoint which mail mode it is in (injectable for tests).
        self.describe = describe or report_delivery.describe_live
        self.inbox = inbox if inbox is not None else submissions_inbox()
        self.state = state if state is not None else state_settings()
        self.root = root if root is not None else repo_root()
        self.excluded = set(excluded if excluded is not None else excluded_directories())
        self.python = python
        self.log = log or (lambda text: None)
        self.results: List[StepResult] = []
        self.halted = False
        self.halted_at = ""

    # ---- running one step -------------------------------------------------------------

    def _record(self, result: StepResult) -> StepResult:
        self.results.append(result)
        self.log(f"\n=== {result.title} [{result.key}]\n$ {result.shown_command}\n"
                 f"{result.output.rstrip()}\n--> {result.status}"
                 + (f" (exit {result.exit_code})" if result.exit_code is not None else "")
                 + (f": {result.note}" if result.note else "") + "\n")
        print(f"  {result.status:<22} {result.title}"
              + (f" (exit {result.exit_code})" if result.exit_code not in (None, 0) else "")
              + (f": {result.note}" if result.note else ""))
        return result

    def step(self, key: str, title: str, command: Sequence[str],
             attention_marker: str = "", attention_note: str = "") -> StepResult:
        """Run one command and classify how it ended. Does nothing once the cycle halted.

        ``attention_marker`` is a line that, found in the output of a step that exited
        clean, means a person is needed all the same (see the corrections step).
        """
        result = StepResult(key=key, title=title, command=[str(part) for part in command])
        if self.halted:
            result.note = "an earlier step failed"
            return self._record(result)
        result.exit_code, result.output = self.runner(result.command)
        result.status = EXIT_MEANINGS[key].get(result.exit_code, FAILED)
        if result.status == OK and attention_marker and attention_marker in result.output:
            result.status = ATTENTION
            result.note = attention_note
        if result.status == FAILED and key not in NON_HALTING:
            # Later steps read state the failed one was meant to leave behind, so running
            # them would act on a half-finished cycle.
            self.halted = True
            self.halted_at = key
        return self._record(result)

    def skipped(self, key: str, title: str, note: str, status: str = PREVIEW_ONLY
                ) -> StepResult:
        return self._record(StepResult(key=key, title=title, status=status, note=note))

    def program(self, name: str, *arguments: str) -> List[str]:
        return [self.python, f"curation/{name}", *arguments]

    @property
    def _dry(self) -> List[str]:
        """The dry-run flag, for the programs that write by default."""
        return [] if self.live else ["--dry-run"]

    # ---- the chain --------------------------------------------------------------------

    def run(self) -> List[StepResult]:
        self._pull_state()
        self._endpoint_mail_mode()
        fetch = self.step("fetch", "1 fetch submissions from the form",
                          self.program("fetch_submissions.py", *self._dry))
        # Submitters' updates, filed as new revisions of their original submissions. The
        # program is quiet and clean until the update form is set up in config.
        self.step("updates", "1 file updates from the update form",
                  self.program("fetch_updates.py", *self._dry))

        # 2. Screen what has not been screened. The list is read after the fetch, so a
        #    submission that arrived in this run is screened in this run.
        if fetch.status in (FAILED, NOT_RUN):
            self.skipped("screen", "2 screen new submissions", "the fetch did not run",
                         status=NOT_RUN)
        else:
            for directory in incomplete_directories(self.inbox):
                self.skipped("screen", f"2 screen the submission received "
                             f"{received_stamp(directory.name)}",
                             "not screened: a file is still to be downloaded", status=ATTENTION)
            pending = unscreened_directories(self.inbox, self.excluded)
            if not pending:
                self.skipped("screen", "2 screen new submissions",
                             "no unscreened directory", status=OK)
            for directory in pending:
                title = f"2 screen the submission received {received_stamp(directory.name)}"
                if self.live:
                    # --redact: this output is captured into a log and may be mailed.
                    self.step("screen", title,
                              self.program("check_template.py", str(directory), "--redact"))
                else:
                    self.skipped("screen", title,
                                 "the screen writes; a live run would run it")

        self._publish_feeds("3", "4", "5")

        self.step("enroll", "6 enroll screened submissions",
                  self.program("enroll.py", *self._dry))
        self.step("reports", "7 send unsent reports to the curators",
                  self.program("publish_report.py", "--all-pending", *self._dry))
        self.step("verdicts", "8 apply the curators' verdicts",
                  self.program("fetch_verdicts.py", *self._dry))

        # 9. Report only, in both modes: without --apply this program writes nothing. It
        #    exits 0 either way, so the line it prints when something is waiting is what
        #    turns the step into "needs a person".
        self.step("corrections", "9 look for approved corrections",
                  self.program("apply_corrections.py"),
                  attention_marker=CORRECTION_WAITING_MARKER,
                  attention_note="a correction is waiting to be recorded (RUNBOOK "
                                 "section 3da)")

        self.step("promote", "10 run the clocks",
                  self.program("promote.py", *self._dry))
        self._commit("commit_decisions", "10 commit the decision record",
                     (DECISION_RECORD,), "Record review decisions")

        self.step("notify", "11 tell submitters whose hold has elapsed",
                  self.program("notify_submitters.py", *self._dry))

        # Steps 6 to 11 rebuild and publish the feeds themselves whenever they change a
        # state, so the files in the tree can have moved since step 4. Committed again
        # here so a cycle never ends with generated files left modified.
        self._commit("commit_feeds", "12 commit the feeds again if later steps changed them",
                     FEED_FILES, "Refresh submission feeds")

        # 12b. The curators' status sheet (2026-10-16): the ledger's state for every
        #      submission, so the assignment, reminders and inbox triage on the Google side
        #      work from today's states. Optional: the program exits 0 when there is no
        #      endpoint or no sheet yet, so it can never stop the cycle. In a preview it
        #      prints the rows and sends nothing.
        self.step("status", "12 push the ledger's view to the status sheet",
                  self.program("push_status.py", *self._dry))
        self._commit_state()
        return self.results

    # ---- the endpoint's mail mode (guard rails of 2026-10-16) --------------------------

    def _endpoint_mail_mode(self) -> None:
        """Say, before anything is sent, whether the endpoint would send it.

        The Apps Script project has its own MAIL_MODE. When it is dry or redirect, every
        message this cycle asks for is held there, the programs report it as held and
        retry later, and nothing reaches a curator or a submitter. That is correct
        behavior and a condition a person must know about, so it is recorded as a step
        that needs a person rather than discovered one held message at a time.
        """
        if self.halted:
            self.skipped("endpoint", "0 ask the endpoint its mail mode",
                         "an earlier step failed", status=NOT_RUN)
            return
        if not self.live:
            self.skipped("endpoint", "0 ask the endpoint its mail mode",
                         "a live run asks; a preview does not post")
            return
        try:
            deployment = self.describe()
        except Exception as exc:                                     # noqa: BLE001
            self.skipped("endpoint", "0 ask the endpoint its mail mode",
                         f"could not ask: {exc}", status=ATTENTION)
            return
        mode = getattr(deployment, "mail_mode", "") or ""
        if not mode:
            self.skipped("endpoint", "0 ask the endpoint its mail mode",
                         "the deployment predates the mail guard; paste the current bundle "
                         "and set MAIL_MODE (RUNBOOK section 3h)", status=ATTENTION)
        elif mode != "live":
            self.skipped("endpoint", "0 ask the endpoint its mail mode",
                         f"the endpoint's MAIL_MODE is {mode}: every message this cycle asks "
                         f"it to send is held there until a person sets it to live",
                         status=ATTENTION)
        else:
            self.skipped("endpoint", "0 ask the endpoint its mail mode", "live", status=OK)

    # ---- the review state in the repository (2026-10-14) ------------------------------

    def _pull_state(self) -> None:
        """Step 0, only when the repository is the ledger's home: refuse to run on
        uncommitted state, then fast-forward to the remote.

        Two machines must never both write the ledger. A run that starts behind its
        remote would write over state another run committed; a run that starts on
        uncommitted state would commit somebody's half-finished hand edit as its own.
        Both are refused outright rather than merged: a merge of two ledgers is not a
        ledger. A preview says what it would do and pulls nothing.
        """
        if not self.state.get("state_in_repo"):
            return
        title = "0 bring the review state up to date"
        if not self.live:
            self.skipped("pull", title, "would refuse uncommitted state, then git pull --ff-only")
            return
        command = ["git", "status", "--porcelain", "--", str(self.inbox)]
        code, output = self.runner(command)
        dirty = [line[3:].strip() for line in output.splitlines()
                 if len(line) > 3 and line[:2].strip() and not line.startswith("??")]
        if code != 0 or dirty:
            self._record(StepResult(
                key="pull", title=title, command=command, output=output, exit_code=code or 1,
                status=FAILED,
                note=("git status failed" if code != 0 else
                      "uncommitted changes to the review state: " + ", ".join(dirty[:5])
                      + ("..." if len(dirty) > 5 else "")
                      + ". Commit or discard them, then run again")))
            self.halted = True
            return
        pulled = self.step("pull", title, ["git", "pull", "--ff-only", "--quiet"])
        if pulled.status != OK:
            # step() marks FAILED on an unknown exit code; an unknown exit halts because
            # "pull" is not in NON_HALTING. Said again here so a reader of the log sees why.
            self.halted = True

    # Steps that WRITE the review state. A halt at or after one of them must not leave
    # what was written uncommitted (on Actions, uncommitted state dies with the runner
    # and the next run re-applies it; code review 2026-10-16, finding 2).
    STATE_WRITERS = ("enroll", "reports", "verdicts", "corrections", "promote",
                     "commit_decisions", "notify", "commit_feeds", "status")

    def _commit_state(self) -> None:
        """Step 13, only when the repository is the ledger's home: force-add the named
        state files (the inbox is gitignored), commit them if anything is staged, push if
        asked. The upload files beside them are never named, so never committed."""
        if not self.state.get("state_in_repo"):
            return
        title = "13 commit the review state"
        if self.halted and self.halted_at not in self.STATE_WRITERS:
            self.skipped("commit_state", title, "an earlier step failed", status=NOT_RUN)
            return
        if self.halted:
            # The halt came AFTER state was written: commit what is there, so a runner
            # does not lose it and the next run does not apply it again. The cycle still
            # ends with exit 1 (the failed step is in the results).
            self.log(f"commit_state: committing although {self.halted_at} failed, because "
                     f"earlier steps wrote the review state\n")
            self.halted = False
            try:
                self._commit_state_steps(title)
            finally:
                self.halted = True
            return
        self._commit_state_steps(title)

    def _commit_state_steps(self, title: str) -> None:
        """The git steps of _commit_state (stage, check, commit, push)."""
        files = state_files(self.inbox, self.root)
        if not files:
            self.skipped("commit_state", title, "no state files yet", status=OK)
            return
        if not self.live:
            self.skipped("commit_state", title,
                         f"would stage and commit {len(files)} state file(s)"
                         + (" and push" if self.state.get("push_state") else ""))
            return
        staged = self.step("commit_state", f"{title} (stage)",
                           ["git", "add", "-f", "--", *files])
        if staged.status != OK:
            return
        check = self.step("commit_state", f"{title} (anything staged?)",
                          ["git", "diff", "--cached", "--quiet", "--", *files])
        if check.exit_code == 0:
            self.skipped("commit_state", title, "nothing changed", status=OK)
            return
        committed = self.step("commit_state", title,
                              ["git", "commit", "-m", "Record intake state", "--", *files])
        if committed.status == OK and self.state.get("push_state"):
            self.step("push_state", "13 push the review state", ["git", "push", "--quiet"])

    def _publish_feeds(self, build: str, commit: str, push: str) -> None:
        """Steps 3 to 5: build the feeds, commit them, publish them.

        A feed that did not rebuild is not committed or published (a half-updated set
        must not go out), and none of this halts the cycle: see NON_HALTING.
        """
        feeds = self.step("feeds", f"{build} build the queue and contributors feeds",
                          self.program("build_site_feeds.py", *self._dry))
        names = self.step("names", f"{build} build the reserved-names feed",
                          self.program("build_name_reservations.py", *self._dry))
        # The curators' board: the same ledger, for the curators, encrypted. Optional (it
        # writes nothing until a password is set up) and never a reason to hold the public
        # feeds back, so its outcome is not consulted below.
        self.step("board", f"{build} build the curators' board",
                  self.program("build_status_board.py", *self._dry))
        if FAILED in (feeds.status, names.status):
            self.skipped("commit_feeds", f"{commit} commit the feeds",
                         "a feed did not rebuild", status=NOT_RUN)
            self.skipped("push_feeds", f"{push} publish the feeds to the site",
                         "a feed did not rebuild", status=NOT_RUN)
            return
        self._commit("commit_feeds", f"{commit} commit the feeds", FEED_FILES,
                     "Refresh submission feeds")
        self.step("push_feeds", f"{push} publish the feeds to the site",
                  ["bash", "publish/push_feeds.sh", *self._dry])

    def _commit(self, key: str, title: str, paths: Sequence[str], message: str) -> None:
        """Commit generated files, only if they changed, and only those files.

        ``git commit -- <paths>`` commits exactly the named paths whatever else is staged
        or modified, so a cycle run while the maintainer has work in progress cannot sweep
        that work into its commit.

        Whether anything changed is read from ``git status --porcelain``, keeping only the
        lines that name one of ``paths``. The runner returns errors and output together,
        so a warning git prints (a config permission, an NFS notice) would otherwise read
        as a change, and the commit that followed would fail with nothing to commit.
        """
        if self.halted:
            self.skipped(key, title, "an earlier step failed", status=NOT_RUN)
            return
        command = ["git", "status", "--porcelain", "--", *paths]
        code, output = self.runner(command)
        if code != 0:
            self._record(StepResult(key=key, title=title, exit_code=code, status=FAILED,
                                    command=command, output=output))
            if key not in NON_HALTING:
                self.halted = True
            return
        # A porcelain line is two status characters, a space, and the path.
        changed = [line[3:].strip().strip('"') for line in output.splitlines()
                   if len(line) > 3 and line[3:].strip().strip('"') in paths]
        if not changed:
            self.skipped(key, title, "nothing changed", status=OK)
            return
        if not self.live:
            self.skipped(key, title, "would commit: " + ", ".join(changed))
            return
        # Only the paths that changed. Naming one that does not exist (the board, before a
        # password is set up) makes `git add` refuse the whole command.
        staged = self.step(key, f"{title} (stage)", ["git", "add", "--", *changed])
        if staged.status == OK:
            self.step(key, title, ["git", "commit", "-m", message, "--", *changed])

    # ---- reading the outcome ----------------------------------------------------------

    @property
    def wanting_a_person(self) -> List[StepResult]:
        return [r for r in self.results if r.status in (ATTENTION, FAILED)]

    @property
    def exit_code(self) -> int:
        if any(r.status == FAILED for r in self.results):
            return 1
        if any(r.status == ATTENTION for r in self.results):
            return 2
        return 0


def without_names(text: str) -> str:
    """Cut the submitter's name out of every directory name in ``text``.

    A submission directory is ``<timestamp>_<submitter's name>`` and the programs print
    it freely. The log keeps it (the log stays on this machine); the email does not.
    """
    import re
    return re.sub(r"(\d{8}T\d{6})_[A-Za-z0-9][A-Za-z0-9_.\-]*", r"\1_[name removed]", text)


def attention_fingerprint(cycle: Cycle) -> str:
    """What this cycle is asking a person for, as a string that is equal when the ask is.

    The steps, how they ended, and the end of what they printed, without the log path or
    anything else that differs between two runs that found the same thing.
    """
    import hashlib
    parts = []
    for result in cycle.wanting_a_person:
        tail = "\n".join(result.output.rstrip().splitlines()[-OUTPUT_TAIL_LINES:])
        parts.append(f"{result.key}|{result.title}|{result.status}|{result.exit_code}|"
                     f"{result.note}|{tail}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def should_send_attention(state_path: Path, fingerprint: str,
                          now: Optional[datetime] = None) -> bool:
    """Whether to send the email: yes unless the same one went out recently."""
    import json
    now = now or datetime.now(timezone.utc)
    try:
        last = json.loads(state_path.read_text(encoding="utf-8"))
        sent = datetime.strptime(last["sent"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError):
        return True
    if last.get("fingerprint") != fingerprint:
        return True
    return (now - sent).days >= ATTENTION_REPEAT_DAYS


def remember_attention(state_path: Path, fingerprint: str,
                       now: Optional[datetime] = None) -> None:
    import json
    now = now or datetime.now(timezone.utc)
    state_path.write_text(json.dumps({"fingerprint": fingerprint,
                                      "sent": now.strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n",
                          encoding="utf-8")


def compose_attention(cycle: Cycle, log_path: Optional[Path]) -> Tuple[str, str]:
    """The subject and body of the one email a cycle sends, when it sends one."""
    wanting = cycle.wanting_a_person
    failed = [r for r in wanting if r.status == FAILED]
    subject = ("MalAvi intake cycle: a step failed" if failed else
               f"MalAvi intake cycle: {len(wanting)} step"
               f"{'' if len(wanting) == 1 else 's'} need{'s' if len(wanting) == 1 else ''} "
               f"a person")
    lines = ["[this is an automatic email]", "",
             "The intake cycle ran and did not end clean. Each step below is followed by "
             "the end of what it printed.", ""]
    for result in wanting:
        lines.append(f"--- {result.title}: {result.status}"
                     + (f" (exit {result.exit_code})" if result.exit_code is not None else ""))
        if result.note:
            lines.append(result.note)
        lines.append(f"$ {result.shown_command}")
        tail = result.output.rstrip().splitlines()[-OUTPUT_TAIL_LINES:]
        lines.extend(tail if tail else ["(no output)"])
        lines.append("")
    not_run = [r.title for r in cycle.results if r.status == NOT_RUN]
    if not_run:
        lines.append("Not run, because an earlier step failed: " + "; ".join(not_run) + ".")
        lines.append("")
    if log_path is not None:
        lines.append(f"The full log is {log_path} on the machine that ran the cycle.")
    lines.append("RUNBOOK section 4b says what each step is and how to run it by hand.")
    return subject, without_names("\n".join(lines))


def live_mail_mode() -> Tuple[str, str]:
    """The mail mode a live cycle runs with: ``(mode, '')``, or ``('', why not)``.

    Unset is refused rather than defaulted: a hand program defaults to dry on its own,
    but a cycle reporting "LIVE" while every message was held would be a quiet failure.
    """
    if not os.environ.get(MAIL_MODE_ENV, "").strip():
        return "", (f"A live cycle needs {MAIL_MODE_ENV} set: live (send), redirect (send "
                    f"everything to the endpoint's MAIL_REDIRECT_TO address) or dry (send "
                    f"nothing; held messages are retried later). Nothing was done.")
    try:
        return report_delivery.mail_mode(), ""
    except report_delivery.DeliveryError as exc:
        return "", f"{exc} Nothing was done."


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the intake chain once. A preview unless --live is given.")
    parser.add_argument("--live", action="store_true",
                        help="really run it: fetch, screen, publish feeds, email curators "
                             "and submitters. Without this, every step is a dry run")
    parser.add_argument("--no-email", action="store_true",
                        help="on a live run that needs a person, do not send the email "
                             "(the summary and the log still say so)")
    arguments = parser.parse_args(argv)

    # A live run says, in so many words, what may be sent (guard rails of 2026-10-16).
    if arguments.live:
        mail, problem = live_mail_mode()
        if problem:
            print(problem, file=sys.stderr)
            return 1
    else:
        mail = "preview (nothing posted)"
    # The programs this cycle runs skip their own freshness check: the cycle pulled.
    os.environ[repo_state.IN_CYCLE_ENV] = "1"

    root = repo_root()
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs / f"intake_cycle_{stamp}.log"

    # One cycle at a time. Two at once would race on the ledger's own lock at best and
    # send a report twice at worst.
    lock_file = open(logs / "intake_cycle.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another intake cycle is running (logs/intake_cycle.lock is held). "
              "Nothing was done.", file=sys.stderr)
        return 1
    except OSError as exc:
        # Not "somebody else has it": the filesystem refused the lock itself.
        print(f"Could not take logs/intake_cycle.lock ({exc}). Nothing was done.",
              file=sys.stderr)
        return 1

    mode = "LIVE" if arguments.live else "preview (nothing is written, sent or published)"
    print(f"== malavi_rebuild :: intake cycle ==\nmode : {mode}\nmail : {mail}\n"
          f"log  : {log_path}\n")

    with open(log_path, "w", encoding="utf-8") as log_file:
        log_file.write(f"intake cycle {stamp}  mode: {mode}  mail: {mail}\n")

        def log(text: str) -> None:
            log_file.write(text)
            log_file.flush()

        cycle = Cycle(live=arguments.live, log=log)
        cycle.run()
        code = cycle.exit_code
        log(f"\nexit code {code}\n")

        print("")
        if code == 0:
            print("The cycle ran clean." if arguments.live
                  else "The preview ran clean. Re-run with --live to act.")
        else:
            wanting = cycle.wanting_a_person
            print(f"{len(wanting)} step(s) need a person:")
            for result in wanting:
                print(f"  - {result.title}: {result.status}"
                      + (f" ({result.note})" if result.note else ""))
            print(f"Read {log_path} for what each one printed.")

        state_path = logs / ATTENTION_STATE_FILE
        fingerprint = attention_fingerprint(cycle)
        if code != 0 and arguments.live and not arguments.no_email \
                and not should_send_attention(state_path, fingerprint):
            print(f"The same thing was emailed within the last {ATTENTION_REPEAT_DAYS} "
                  f"days; not emailed again.")
            log("attention email not repeated: same content as the last one sent\n")
        elif code != 0 and arguments.live and not arguments.no_email:
            subject, body = compose_attention(cycle, log_path)
            try:
                # The key carries a 7-day bucket, so the endpoint's 30-day duplicate
                # memory does not silence a standing condition for a month: this side
                # repeats after ATTENTION_REPEAT_DAYS, and the endpoint agrees.
                bucket = int(datetime.now(timezone.utc).timestamp() // (ATTENTION_REPEAT_DAYS * 86400))
                delivered = report_delivery.deliver_attention_notice(
                    subject=subject, body=body, mail_key_=f"{fingerprint}|{bucket}")
                if delivered.action == "already_sent":
                    print("The endpoint had already sent this notice; not counted as new.")
                    log("attention email: the endpoint already had it\n")
                else:
                    remember_attention(state_path, fingerprint)
                    print(f"Emailed {delivered.notified} lead curator(s).")
                    log(f"attention email sent to {delivered.notified} address(es)\n")
            except Exception as exc:                                 # noqa: BLE001
                # The email is a convenience; the exit code and the log are the record.
                print(f"The attention email could not be sent: {exc}", file=sys.stderr)
                log(f"attention email FAILED: {exc}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
