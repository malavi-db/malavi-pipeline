"""The intake cycle (curation/intake_cycle.py).

The cycle runs other programs and reads their exit codes, so it is tested with a stand-in
runner: a function that records each command and answers with a scripted exit code and
output. No program is executed and nothing is written outside ``tmp_path``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from malavi_curation import report_delivery
from malavi_curation.config import repo_root

_spec = importlib.util.spec_from_file_location(
    "intake_cycle", repo_root() / "curation" / "intake_cycle.py")
intake_cycle = importlib.util.module_from_spec(_spec)
# Registered before it is executed: the dataclasses in it look their own module up.
sys.modules["intake_cycle"] = intake_cycle
_spec.loader.exec_module(intake_cycle)

Cycle = intake_cycle.Cycle


class FakeRunner:
    """Records every command; answers from ``scripted``, keyed by a fragment of it."""

    def __init__(self, scripted=None):
        self.commands = []
        self.scripted = scripted or {}

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        joined = " ".join(command)
        for fragment, answer in self.scripted.items():
            if fragment in joined:
                return answer
        return 0, ""

    def ran(self, fragment):
        return [c for c in self.commands if fragment in " ".join(c)]


@pytest.fixture
def inbox(tmp_path):
    """Six directories, one for each thing the cycle has to tell apart."""
    root = tmp_path / "submissions"
    # Screened to the end: the report is there.
    (root / "20260101T000000_screened").mkdir(parents=True)
    (root / "20260101T000000_screened" / "screen.json").write_text("{}")
    (root / "20260101T000000_screened" / "report.html").write_text("<p>")
    # A paper with no template: screened, by being reported as such.
    (root / "20260102T000000_paper_only").mkdir()
    (root / "20260102T000000_paper_only" / "report.html").write_text("<p>")
    # New.
    (root / "20260103T000000_Jane_Smith").mkdir()
    # A screen that crashed after writing screen.json and before the report.
    (root / "20260104T000000_crashed").mkdir()
    (root / "20260104T000000_crashed" / "screen.json").write_text("{}")
    # A test submission, excluded in config: never screened by the cycle.
    (root / "20260105T000000_demo").mkdir()
    # A download that did not finish.
    (root / "20260106T000000_incomplete").mkdir()
    (root / "20260106T000000_incomplete" / ".download_incomplete").write_text("FILEID\n")
    (root / "_superseded").mkdir()
    (root / "review_ledger.json").write_text("{}")
    return root


EXCLUDED = ("20260105T000000_demo",)


class FakeDeployment:
    """What describe_live() answers: the endpoint's mail mode (guard rails 2026-10-16)."""

    def __init__(self, mail_mode="live"):
        self.mail_mode = mail_mode


def _live_endpoint():
    return FakeDeployment("live")


def _cycle(live, inbox, scripted=None, describe=_live_endpoint):
    runner = FakeRunner(scripted)
    cycle = Cycle(live=live, runner=runner, inbox=inbox, python="PY", excluded=EXCLUDED,
                  describe=describe)
    cycle.run()
    return cycle, runner


# --------------------------------------------------------------------------------------
# Which directories need screening
# --------------------------------------------------------------------------------------

def test_what_counts_as_unscreened(inbox):
    names = [d.name for d in intake_cycle.unscreened_directories(inbox, EXCLUDED)]
    # The new one, and the one whose screen crashed after screen.json was written. Not the
    # excluded test submission and not the one still waiting for a file.
    assert names == ["20260103T000000_Jane_Smith", "20260104T000000_crashed"]


def test_screen_json_alone_does_not_mean_screened(inbox):
    """It is written before the checks and the report; only the report means finished."""
    assert intake_cycle.SCREENED_MARKER == "report.html"
    crashed = inbox / "20260104T000000_crashed"
    assert crashed in intake_cycle.unscreened_directories(inbox, EXCLUDED)
    (crashed / "report.html").write_text("<p>")
    assert crashed not in intake_cycle.unscreened_directories(inbox, EXCLUDED)


def test_an_incomplete_download_is_not_screened_and_is_reported(inbox):
    cycle, runner = _cycle(True, inbox)
    assert not any("20260106T000000_incomplete" in " ".join(c)
                   for c in runner.ran("check_template.py"))
    waiting = [r for r in cycle.results
               if r.key == "screen" and r.status == intake_cycle.ATTENTION]
    assert len(waiting) == 1 and "20260106T000000" in waiting[0].title
    assert cycle.exit_code == 2


def test_the_incomplete_marker_is_the_one_the_fetch_writes():
    source = (repo_root() / "curation" / "fetch_submissions.py").read_text(encoding="utf-8")
    assert f'INCOMPLETE_MARKER = "{intake_cycle.INCOMPLETE_MARKER}"' in source


def test_a_missing_inbox_has_nothing_to_screen(tmp_path):
    assert intake_cycle.unscreened_directories(tmp_path / "absent") == []


# --------------------------------------------------------------------------------------
# A preview writes nothing
# --------------------------------------------------------------------------------------

def test_a_preview_runs_every_writer_as_a_dry_run(inbox):
    cycle, runner = _cycle(False, inbox)
    for program in ("fetch_submissions.py", "fetch_updates.py", "build_site_feeds.py",
                    "build_name_reservations.py", "build_status_board.py", "enroll.py",
                    "publish_report.py",
                    "fetch_verdicts.py", "promote.py", "notify_submitters.py",
                    "push_feeds.sh"):
        commands = runner.ran(program)
        assert commands, f"{program} was not run"
        assert all("--dry-run" in c for c in commands), f"{program} ran without --dry-run"
    # 2, not 0: the fixture holds a submission whose download is incomplete, and a preview
    # says so like a live run would.
    assert cycle.exit_code == 2


def test_a_preview_never_screens_commits_or_applies(inbox):
    _cycle_, runner = _cycle(False, inbox, {"git status": (0, " M docs/assets/data/queue.json\n")})
    assert runner.ran("check_template.py") == []
    assert runner.ran("git add") == [] and runner.ran("git commit") == []
    assert all("--apply" not in c for c in runner.commands)


def test_a_preview_says_what_it_would_have_screened_without_the_name(inbox):
    cycle, _runner = _cycle(False, inbox)
    previews = [r for r in cycle.results
                if r.key == "screen" and r.status == intake_cycle.PREVIEW_ONLY]
    assert [r.title for r in previews] == [
        "2 screen the submission received 20260103T000000",
        "2 screen the submission received 20260104T000000"]


# --------------------------------------------------------------------------------------
# A live run
# --------------------------------------------------------------------------------------

def test_a_live_run_follows_the_runbook_s_order(inbox):
    _cycle_, runner = _cycle(True, inbox)
    order = [next(part for part in command if part.endswith((".py", ".sh")) or part == "git")
             for command in runner.commands]
    programs = [Path(name).name for name in order if name != "git"]
    assert programs == [
        "fetch_submissions.py", "fetch_updates.py", "check_template.py", "check_template.py",
        "build_site_feeds.py", "build_name_reservations.py", "build_status_board.py",
        "push_feeds.sh", "enroll.py",
        "publish_report.py", "fetch_verdicts.py", "apply_corrections.py", "promote.py",
        "notify_submitters.py", "push_status.py"]
    assert all("--dry-run" not in c for c in runner.commands)


def test_a_live_run_screens_only_what_is_unscreened_and_redacts(inbox):
    _cycle_, runner = _cycle(True, inbox)
    screens = runner.ran("check_template.py")
    assert [c[-2:] for c in screens] == [
        [str(inbox / "20260103T000000_Jane_Smith"), "--redact"],
        [str(inbox / "20260104T000000_crashed"), "--redact"]]


def test_corrections_are_never_applied_by_the_cycle(inbox):
    _cycle_, runner = _cycle(True, inbox)
    assert runner.ran("apply_corrections.py") == [["PY", "curation/apply_corrections.py"]]


def test_feeds_are_committed_only_when_they_changed(inbox):
    _cycle_, quiet = _cycle(True, inbox)
    assert quiet.ran("git commit") == []

    _cycle_, changed = _cycle(True, inbox, {
        "git status --porcelain -- docs/": (0, " M docs/assets/data/queue.json\n")})
    commits = changed.ran("git commit")
    # Once at step 4 and once at step 12 (the stand-in reports the change both times).
    assert len(commits) == 2
    # Exactly the file that changed, named after "--": nothing else in the tree is swept
    # in, and a feed that does not exist (the board, before it is set up) is not named,
    # because git refuses a path it cannot find.
    assert commits[0][:4] == ["git", "commit", "-m", "Refresh submission feeds"]
    assert commits[0][commits[0].index("--") + 1:] == ["docs/assets/data/queue.json"]
    assert "docs/assets/data/queue.json" in intake_cycle.FEED_FILES


def test_the_board_is_built_with_the_feeds_and_cannot_stop_the_cycle(inbox):
    cycle, runner = _cycle(True, inbox, {"build_status_board.py": (1, "no cryptography")})
    assert runner.ran("build_status_board.py")
    # A board that fails is reported, and everything else still happens, the publish of
    # the public feeds included.
    assert cycle.exit_code == 1
    assert runner.ran("push_feeds.sh") and runner.ran("notify_submitters.py")


def test_a_git_warning_is_not_mistaken_for_a_change(inbox):
    """The runner returns errors and output together; only porcelain lines count."""
    _cycle_, runner = _cycle(True, inbox, {
        "git status --porcelain": (0, "warning: unable to access '/x/.gitconfig'\n")})
    assert runner.ran("git add") == [] and runner.ran("git commit") == []


def test_the_decision_record_is_committed_when_it_changed(inbox):
    _cycle_, runner = _cycle(True, inbox, {
        "git status --porcelain -- data/decisions.json": (0, " M data/decisions.json\n")})
    commits = runner.ran("git commit")
    assert len(commits) == 1 and commits[0][-1] == "data/decisions.json"


# --------------------------------------------------------------------------------------
# How it ends
# --------------------------------------------------------------------------------------

def test_exit_2_from_a_program_is_a_person_needed_not_a_failure(inbox, tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    for program in ("fetch_submissions.py", "fetch_updates.py", "fetch_verdicts.py",
                    "promote.py", "notify_submitters.py", "build_name_reservations.py"):
        cycle, runner = _cycle(True, clean, {program: (2, "needs a person")})
        assert cycle.exit_code == 2, program
        assert len(cycle.wanting_a_person) == 1, program
        # ...and the cycle carried on to the end.
        assert runner.ran("notify_submitters.py"), program


def test_a_failed_step_stops_the_cycle(inbox):
    cycle, runner = _cycle(True, inbox, {"enroll.py": (1, "Traceback")})
    assert cycle.exit_code == 1
    assert runner.ran("publish_report.py") == [] and runner.ran("notify_submitters.py") == []
    not_run = [r.key for r in cycle.results if r.status == intake_cycle.NOT_RUN]
    assert "reports" in not_run and "notify" in not_run


def test_a_failed_fetch_leaves_the_screen_not_run_rather_than_ok(inbox):
    cycle, runner = _cycle(True, inbox, {"fetch_submissions.py": (1, "no network")})
    assert runner.ran("check_template.py") == []
    screens = [r for r in cycle.results if r.key == "screen"]
    assert [r.status for r in screens] == [intake_cycle.NOT_RUN]


def test_one_unscreenable_submission_does_not_block_the_rest(inbox):
    cycle, runner = _cycle(True, inbox, {"check_template.py": (1, "could not read")})
    assert cycle.exit_code == 1                       # still reported as a failure
    assert runner.ran("fetch_verdicts.py") and runner.ran("notify_submitters.py")


@pytest.mark.parametrize("broken", ["build_site_feeds.py", "build_name_reservations.py",
                                    "push_feeds.sh"])
def test_a_feed_failure_does_not_block_verdicts_and_notices(inbox, broken):
    """Publishing is downstream of the ledger; GitHub being down must not stop review."""
    cycle, runner = _cycle(True, inbox, {broken: (1, "remote unreachable")})
    assert cycle.exit_code == 1
    for program in ("enroll.py", "publish_report.py", "fetch_verdicts.py", "promote.py",
                    "notify_submitters.py"):
        assert runner.ran(program), f"{program} did not run after {broken} failed"


def test_feeds_that_did_not_rebuild_are_not_published(inbox):
    _cycle_, runner = _cycle(True, inbox, {
        "build_site_feeds.py": (1, "boom"),
        "git status --porcelain -- docs/": (0, " M docs/assets/data/queue.json\n")})
    assert runner.ran("push_feeds.sh") == []
    # Step 4's commit is skipped too; only step 12's remains.
    assert len(runner.ran("git commit")) == 1


def test_a_program_that_cannot_be_started_is_a_failed_step_not_a_crash(monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("push_feeds.sh: Permission denied")
    monkeypatch.setattr(intake_cycle.subprocess, "run", refuse)
    code, output = intake_cycle._run_process(["publish/push_feeds.sh"])
    assert code == 127 and "could not be started" in output


def test_a_paper_only_screen_is_not_an_alarm(tmp_path):
    root = tmp_path / "s"
    (root / "20260103T000000_new").mkdir(parents=True)
    cycle, _runner = _cycle(True, root, {"check_template.py": (3, "paper-only report")})
    assert cycle.exit_code == 0


def test_a_blocking_screen_finding_wants_a_person(tmp_path):
    root = tmp_path / "s"
    (root / "20260103T000000_new").mkdir(parents=True)
    cycle, _runner = _cycle(True, root, {"check_template.py": (2, "blocking")})
    assert cycle.exit_code == 2


def test_a_correction_waiting_wants_a_person(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    output = ("1 approved correction(s) to record:\n  ...\n\n[dry-run] nothing was "
              "written. " + intake_cycle.CORRECTION_WAITING_MARKER)
    cycle, _runner = _cycle(True, root, {"apply_corrections.py": (0, output)})
    assert cycle.exit_code == 2
    waiting = [r for r in cycle.wanting_a_person if r.key == "corrections"]
    assert waiting and "RUNBOOK section 3da" in waiting[0].note


def test_the_correction_marker_is_the_line_apply_corrections_prints():
    """The cycle reads that program's output; the string must be the one it prints."""
    source = (repo_root() / "curation" / "apply_corrections.py").read_text(encoding="utf-8")
    assert intake_cycle.CORRECTION_WAITING_MARKER in source


def test_every_step_key_has_its_exit_codes_declared(inbox):
    cycle, _runner = _cycle(True, inbox)
    assert {r.key for r in cycle.results} <= set(intake_cycle.EXIT_MEANINGS)


# --------------------------------------------------------------------------------------
# The email
# --------------------------------------------------------------------------------------

def test_the_email_carries_the_steps_that_need_a_person_and_their_output(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    long_output = "\n".join(f"line {i}" for i in range(200))
    cycle, _runner = _cycle(True, root, {"fetch_verdicts.py": (2, long_output)})
    subject, body = intake_cycle.compose_attention(cycle, Path("logs/intake_cycle_X.log"))
    assert subject == "MalAvi intake cycle: 1 step needs a person"
    assert "8 apply the curators' verdicts" in body
    assert "line 199" in body and "line 100" not in body       # the end of it, not all
    assert "logs/intake_cycle_X.log" in body
    assert "1 fetch submissions" not in body                    # clean steps are left out


def test_a_failure_is_named_as_one_and_lists_what_did_not_run(inbox):
    cycle, _runner = _cycle(True, inbox, {"enroll.py": (1, "Traceback")})
    subject, body = intake_cycle.compose_attention(cycle, None)
    assert subject == "MalAvi intake cycle: a step failed"
    assert "Not run, because an earlier step failed" in body
    assert "11 tell submitters" in body


def test_the_email_never_carries_a_submitter_s_name(inbox):
    """Directory names hold the name; the log keeps them and the email must not."""
    printed = "  20260103T000000_Jane_Smith: FAILED could not read the workbook"
    cycle, _runner = _cycle(True, inbox, {"check_template.py": (1, printed)})
    _subject, body = intake_cycle.compose_attention(cycle, None)
    assert "Jane" not in body and "Smith" not in body
    assert "20260103T000000_[name removed]" in body
    # The log's copy of the same step keeps the name: it never leaves the machine.
    assert any("Jane_Smith" in r.shown_command for r in cycle.results)


def test_the_same_email_is_not_sent_again_within_the_week(tmp_path, inbox):
    from datetime import datetime, timedelta, timezone
    state = tmp_path / "last.json"
    cycle, _runner = _cycle(True, inbox, {"promote.py": (2, "1 item(s) need attention")})
    fingerprint = intake_cycle.attention_fingerprint(cycle)
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

    assert intake_cycle.should_send_attention(state, fingerprint, now)      # never sent
    intake_cycle.remember_attention(state, fingerprint, now)
    assert not intake_cycle.should_send_attention(state, fingerprint, now + timedelta(days=1))
    # A week on, the standing condition is raised again.
    assert intake_cycle.should_send_attention(
        state, fingerprint, now + timedelta(days=intake_cycle.ATTENTION_REPEAT_DAYS))
    # Something different is sent at once.
    other, _r = _cycle(True, inbox, {"fetch_verdicts.py": (2, "1 response unparsed")})
    assert intake_cycle.should_send_attention(
        state, intake_cycle.attention_fingerprint(other), now + timedelta(hours=1))
    # Two runs that found the same thing have the same fingerprint.
    again, _r = _cycle(True, inbox, {"promote.py": (2, "1 item(s) need attention")})
    assert intake_cycle.attention_fingerprint(again) == fingerprint


def test_the_attention_payload_names_no_recipient_and_is_cut_to_size():
    payload = report_delivery.build_attention_payload(
        subject=" s ", body="x" * (report_delivery.MAX_ATTENTION_BODY + 500), issued_at=1)
    assert payload["action"] == "attention_notice" and payload["subject"] == "s"
    assert "to" not in payload and "recipients" not in payload
    assert len(payload["body"]) < report_delivery.MAX_ATTENTION_BODY + 200
    assert payload["body"].endswith("has the rest]")
    with pytest.raises(report_delivery.DeliveryError):
        report_delivery.build_attention_payload(subject="", body="x")


# --------------------------------------------------------------------------------------
# The review state in the repository (2026-10-14)
# --------------------------------------------------------------------------------------

STATE_ON = {"state_in_repo": True, "push_state": False}


def _state_cycle(live, inbox, scripted=None, state=None, push=False):
    runner = FakeRunner(scripted)
    settings = dict(state or STATE_ON)
    settings["push_state"] = push
    cycle = Cycle(live=live, runner=runner, inbox=inbox, python="PY", excluded=EXCLUDED,
                  state=settings, root=inbox.parent, describe=_live_endpoint)
    cycle.run()
    return cycle, runner


def test_state_files_name_only_the_ledger_and_screen_outputs(inbox):
    """Never an upload: a workbook or a paper beside the state is not state."""
    (inbox / "20260103T000000_Jane_Smith" / "metadata.json").write_text("{}")
    (inbox / "20260103T000000_Jane_Smith" / "Template - Jane Smith.xlsx").write_bytes(b"x")
    (inbox / "20260103T000000_Jane_Smith" / "paper.pdf").write_bytes(b"x")
    (inbox / "20260103T000000_Jane_Smith" / "update_20260110T000000Z.json").write_text("{}")
    (inbox / "submission_ids.json").write_text("{}")
    (inbox / "_superseded" / "old.json").write_text("{}")
    files = intake_cycle.state_files(inbox, inbox.parent)
    assert "submissions/review_ledger.json" in files
    assert "submissions/submission_ids.json" in files
    assert "submissions/20260103T000000_Jane_Smith/metadata.json" in files
    assert "submissions/20260103T000000_Jane_Smith/update_20260110T000000Z.json" in files
    assert "submissions/20260101T000000_screened/report.html" in files
    assert not any(f.endswith((".xlsx", ".pdf")) and "report.pdf" not in f for f in files)
    assert not any("_superseded" in f for f in files)


def test_with_the_setting_off_nothing_about_git_state_runs(inbox):
    # The settings are given explicitly: the live config has had them ON since
    # 2026-10-16, and this test is about the cycle's behavior when they are off.
    cycle, runner = _state_cycle(True, inbox,
                                 state={"state_in_repo": False, "push_state": False})
    assert not runner.ran("git pull") and not runner.ran("add -f")
    assert not any(r.key in ("pull", "commit_state", "push_state") for r in cycle.results)


def test_a_live_run_pulls_first_and_commits_the_state_last(inbox):
    cycle, runner = _state_cycle(True, inbox, {"git diff --cached --quiet": (1, "")}, push=True)
    commands = [" ".join(c) for c in runner.commands]
    pull = next(i for i, c in enumerate(commands) if "git pull --ff-only" in c)
    assert pull < next(i for i, c in enumerate(commands) if "fetch_submissions.py" in c)
    add = next(i for i, c in enumerate(commands) if c.startswith("git add -f --"))
    assert "submissions/review_ledger.json" in commands[add]
    assert add > max(i for i, c in enumerate(commands) if "notify_submitters.py" in c)
    commit = next(i for i, c in enumerate(commands) if "Record intake state" in c)
    assert commit > add
    push = next(i for i, c in enumerate(commands) if c == "git push --quiet")
    assert push > commit
    # The fixture's incomplete download makes the run a 2; nothing about the state failed.
    assert not any(r.status == intake_cycle.FAILED for r in cycle.results)
    assert cycle.exit_code == 2


def test_nothing_staged_means_no_commit_and_no_push(inbox):
    cycle, runner = _state_cycle(True, inbox, {"git diff --cached --quiet": (0, "")}, push=True)
    assert runner.ran("add -f") and not runner.ran("Record intake state")
    assert not runner.ran("git push")
    assert [r.note for r in cycle.results if r.key == "commit_state" and r.note][-1] == "nothing changed"


def test_a_preview_pulls_and_commits_nothing(inbox):
    cycle, runner = _state_cycle(False, inbox, push=True)
    assert not runner.ran("git pull") and not runner.ran("add -f") and not runner.ran("git push")
    notes = {r.key: r.note for r in cycle.results if r.key in ("pull", "commit_state")}
    assert "git pull --ff-only" in notes["pull"]
    assert "would stage and commit" in notes["commit_state"] and "and push" in notes["commit_state"]


def test_uncommitted_state_refuses_the_whole_run(inbox):
    cycle, runner = _state_cycle(
        True, inbox, {f"git status --porcelain -- {inbox}": (0, " M submissions/review_ledger.json\n?? submissions/new/\n")})
    assert not runner.ran("git pull") and not runner.ran("fetch_submissions.py")
    first = cycle.results[0]
    assert first.key == "pull" and first.status == intake_cycle.FAILED
    assert "uncommitted changes to the review state: submissions/review_ledger.json" in first.note
    assert cycle.exit_code == 1


def test_untracked_files_under_the_inbox_do_not_count_as_uncommitted(inbox):
    cycle, runner = _state_cycle(
        True, inbox, {f"git status --porcelain -- {inbox}": (0, "?? submissions/20260107T000000_new/\n")})
    assert runner.ran("git pull")


def test_a_pull_that_cannot_fast_forward_halts_everything(inbox):
    cycle, runner = _state_cycle(True, inbox, {"git pull --ff-only": (128, "fatal: Not possible to fast-forward")})
    assert not runner.ran("fetch_submissions.py") and not runner.ran("add -f")
    assert cycle.results[0].status == intake_cycle.FAILED
    assert all(r.status == intake_cycle.NOT_RUN for r in cycle.results[1:])
    assert cycle.exit_code == 1


def test_the_settings_come_from_the_environment_first(monkeypatch):
    monkeypatch.delenv("MALAVI_STATE_IN_REPO", raising=False)
    monkeypatch.delenv("MALAVI_PUSH_STATE", raising=False)
    assert intake_cycle.state_settings({"intake": {"state_in_repo": True}}) == {
        "state_in_repo": True, "push_state": False}
    assert intake_cycle.state_settings({}) == {"state_in_repo": False, "push_state": False}
    monkeypatch.setenv("MALAVI_STATE_IN_REPO", "1")
    monkeypatch.setenv("MALAVI_PUSH_STATE", "true")
    assert intake_cycle.state_settings({"intake": {"state_in_repo": False}}) == {
        "state_in_repo": True, "push_state": True}
    monkeypatch.setenv("MALAVI_STATE_IN_REPO", "0")
    assert intake_cycle.state_settings({"intake": {"state_in_repo": True}})["state_in_repo"] is False


def test_the_workflow_turns_the_state_on_and_names_the_three_secrets():
    text = (repo_root() / ".github" / "workflows" / "intake_cycle.yml").read_text(encoding="utf-8")
    assert 'MALAVI_STATE_IN_REPO: "1"' in text and 'MALAVI_PUSH_STATE: "1"' in text
    for secret in ("MALAVI_GOOGLE_KEY", "MALAVI_REPORT_SECRET", "MALAVI_SITE_TOKEN"):
        assert f"secrets.{secret}" in text, secret
    assert "intake_cycle.py --live" in text and "vars.MALAVI_INTAKE_MODE" in text
    assert "concurrency" in text


# --------------------------------------------------------------------------------------
# The mail guard (2026-10-16): the endpoint's mode is asked first; a live run names its own
# --------------------------------------------------------------------------------------

def _endpoint_step(cycle):
    return next(r for r in cycle.results if r.key == "endpoint")


def test_a_live_cycle_asks_the_endpoint_its_mail_mode_first(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    cycle, _runner = _cycle(True, root)
    keys = [r.key for r in cycle.results]
    assert keys.index("endpoint") < keys.index("fetch")
    assert _endpoint_step(cycle).status == intake_cycle.OK
    assert cycle.exit_code == 0


def test_an_endpoint_in_dry_mode_makes_the_cycle_need_a_person(tmp_path):
    """Every message the cycle asks for would be held there; a person must know."""
    root = tmp_path / "s"
    root.mkdir()
    cycle, _runner = _cycle(True, root, describe=lambda: FakeDeployment("dry"))
    assert _endpoint_step(cycle).status == intake_cycle.ATTENTION
    assert "MAIL_MODE is dry" in _endpoint_step(cycle).note
    assert cycle.exit_code == 2


def test_an_endpoint_without_the_mail_guard_is_reported(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    cycle, _runner = _cycle(True, root, describe=lambda: FakeDeployment(""))
    assert _endpoint_step(cycle).status == intake_cycle.ATTENTION
    assert "predates the mail guard" in _endpoint_step(cycle).note


def test_an_endpoint_that_cannot_be_asked_is_reported_not_fatal(tmp_path):
    root = tmp_path / "s"
    root.mkdir()

    def broken():
        raise RuntimeError("no endpoint configured")

    cycle, runner = _cycle(True, root, describe=broken)
    assert _endpoint_step(cycle).status == intake_cycle.ATTENTION
    assert runner.ran("fetch_submissions.py")          # the cycle still ran


def test_a_preview_does_not_ask_the_endpoint(tmp_path):
    root = tmp_path / "s"
    root.mkdir()

    def must_not_be_called():
        raise AssertionError("a preview posted to the endpoint")

    cycle, _runner = _cycle(False, root, describe=must_not_be_called)
    assert _endpoint_step(cycle).status == intake_cycle.PREVIEW_ONLY


def test_a_live_run_refuses_without_an_explicit_mail_mode(monkeypatch):
    monkeypatch.delenv("MALAVI_MAIL_MODE", raising=False)
    mode, problem = intake_cycle.live_mail_mode()
    assert mode == "" and "needs MALAVI_MAIL_MODE set" in problem


def test_a_live_run_refuses_a_misspelled_mail_mode(monkeypatch):
    monkeypatch.setenv("MALAVI_MAIL_MODE", "lvie")
    mode, problem = intake_cycle.live_mail_mode()
    assert mode == "" and "lvie" in problem


@pytest.mark.parametrize("value", ["live", "redirect", "dry"])
def test_each_mail_mode_is_accepted_by_name(monkeypatch, value):
    monkeypatch.setenv("MALAVI_MAIL_MODE", value)
    assert intake_cycle.live_mail_mode() == (value, "")


def test_the_attention_notice_carries_the_cycle_fingerprint_as_its_mail_key(tmp_path):
    """The endpoint dedupes on the key; the cycle's own fingerprint is that key."""
    root = tmp_path / "s"
    root.mkdir()
    cycle, _runner = _cycle(True, root, {"fetch_verdicts.py": (2, "needs a person")})
    fingerprint = intake_cycle.attention_fingerprint(cycle)
    payload = report_delivery.build_attention_payload(subject="s", body="b",
                                                      mail_key_=fingerprint)
    assert payload["mail_key"] == fingerprint


def test_a_halt_after_the_state_was_written_still_commits_it(inbox):
    """Code review 2026-10-16, finding 2: a failure in step 11 must not leave steps 6 to 10's
    writes uncommitted (on Actions they would die with the runner and be applied again)."""
    cycle, runner = _state_cycle(True, inbox, {"notify_submitters.py": (1, "boom"),
                                               "git diff --cached --quiet": (1, "")})
    assert cycle.exit_code == 1
    assert runner.ran("git add -f")
    assert any("Record intake state" in " ".join(c) for c in runner.commands)


def test_a_halt_before_any_state_was_written_commits_nothing(inbox):
    cycle, runner = _state_cycle(True, inbox, {"fetch_submissions.py": (1, "boom")})
    assert cycle.exit_code == 1
    assert not runner.ran("git add -f")


def test_the_attention_key_changes_every_seven_days():
    """Code review 2026-10-16, finding 8: the endpoint's 30-day memory must not silence a
    standing condition for a month."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    a = int(now // (intake_cycle.ATTENTION_REPEAT_DAYS * 86400))
    b = int((now + 8 * 86400) // (intake_cycle.ATTENTION_REPEAT_DAYS * 86400))
    assert a != b
    text = (repo_root() / "curation" / "intake_cycle.py").read_text(encoding="utf-8")
    assert 'mail_key_=f"{fingerprint}|{bucket}"' in text
    assert 'if delivered.action == "already_sent":' in text
