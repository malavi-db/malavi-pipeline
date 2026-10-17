"""The machine-side mail guard rails of 2026-10-16 (results/GUARDRAILS_PLAN_2026-10-16.md).

Written the evening the first live cycle with the state in the repository re-applied
thirteen old verdict-form responses and sent the curators nine duplicate notices. Each
test here pins one of the rules that would have stopped that at zero emails, or one of
the rules the plan review of the same day asked for:

* the mail mode: unset means dry, dry posts nothing that carries mail, redirect marks the
  payload and never carries an address, a held reply is never recorded as sent;
* the two refusals in fetch_verdicts.py: more new responses than the limit, or any new
  response OLDER than the newest already applied (the signature of a re-apply);
* a notice that could not be sent stays pending and is retried, never lost;
* the audience of a ledger notice (the form's trigger already told everyone);
* the caps in publish_report.py and notify_submitters.py;
* a stale checkout may not write the review state.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from malavi_curation import ledger, repo_state, report_delivery, verdicts
from malavi_curation.config import repo_root
from malavi_curation.report_delivery import DeliveryError, MailHeld


def _load(name: str):
    path = repo_root() / "curation" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_guard_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fetch():
    return _load("fetch_verdicts")


@pytest.fixture(scope="module")
def notify():
    return _load("notify_submitters")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    secret_file = tmp_path / "report_secret.txt"
    secret_file.write_text("0123456789abcdef0123456789abcdef", encoding="utf-8")
    monkeypatch.setenv("MALAVI_REPORT_ENDPOINT", "https://script.google.com/x/exec")
    monkeypatch.setenv("MALAVI_REPORT_SECRET_FILE", str(secret_file))


class Recorder:
    """A transport that records the payload and answers like the endpoint."""

    def __init__(self, **reply):
        self.calls = []
        self.reply = {"ok": True, "action": "emailed", "notified": 2, "sent": 2,
                      "held": [], "mail_mode": "live"}
        self.reply.update(reply)

    def __call__(self, url, body):
        self.calls.append(json.loads(body))
        return dict(self.reply)


# ======================================================================================
# The mail mode on this machine
# ======================================================================================

def test_unset_means_dry(monkeypatch):
    monkeypatch.delenv("MALAVI_MAIL_MODE", raising=False)
    assert report_delivery.mail_mode() == "dry"
    assert "not set" in report_delivery.describe_mail_mode()


@pytest.mark.parametrize("value", ["live", "REDIRECT", " dry "])
def test_the_three_modes_are_read_case_and_space_insensitively(monkeypatch, value):
    monkeypatch.setenv("MALAVI_MAIL_MODE", value)
    assert report_delivery.mail_mode() == value.strip().lower()


def test_a_misspelled_mode_is_an_error_not_a_quiet_dry_run(monkeypatch):
    monkeypatch.setenv("MALAVI_MAIL_MODE", "lvie")
    with pytest.raises(DeliveryError, match="lvie"):
        report_delivery.mail_mode()


def test_dry_mode_posts_nothing_that_carries_mail(configured, monkeypatch):
    monkeypatch.setenv("MALAVI_MAIL_MODE", "dry")
    transport = Recorder()
    with pytest.raises(MailHeld, match="not sent"):
        report_delivery.deliver_verdict_notice("MALAVI-SUB-2026-000001", subject="s",
                                               body="b", transport=transport)
    assert transport.calls == []               # nothing reached the network


def test_dry_mode_still_posts_what_carries_no_mail(configured, monkeypatch):
    """The status sheet sync and the describe check are not mail; a dry session may
    still keep the sheet current and ask the endpoint what it is."""
    monkeypatch.setenv("MALAVI_MAIL_MODE", "dry")
    transport = Recorder(code_version="x", actions=[])
    report_delivery.deliver_status([{"id": "MALAVI-SUB-2026-000001", "state": "held"}],
                                   transport=transport)
    assert transport.calls[0]["action"] == "status"


def test_dry_mode_holds_a_report_that_would_notify_but_not_one_that_would_not(
        configured, monkeypatch):
    monkeypatch.setenv("MALAVI_MAIL_MODE", "dry")
    pdf = b"%PDF-1.7 x"
    transport = Recorder(fileId="F", url="u", action="created",
                         sha256=report_delivery.hashlib.sha256(pdf).hexdigest(), notified=0)
    with pytest.raises(MailHeld):
        report_delivery.deliver("MALAVI-SUB-2026-000001", pdf, notify=True,
                                transport=transport)
    assert transport.calls == []
    result = report_delivery.deliver("MALAVI-SUB-2026-000001", pdf, notify=False,
                                     transport=transport)
    assert result.action == "created" and transport.calls[0]["notify"] is False


def test_redirect_mode_marks_the_payload_and_carries_no_address(configured, monkeypatch):
    """The redirect address lives in the endpoint's MAIL_REDIRECT_TO property only: a
    leaked secret must not become an open mailer (review 2026-10-16, finding 3)."""
    monkeypatch.setenv("MALAVI_MAIL_MODE", "redirect")
    transport = Recorder(held=[{"to": "lead@example.edu", "reason": "redirected"}],
                         notified=0, mail_mode="redirect")
    with pytest.raises(MailHeld, match="redirected"):
        report_delivery.deliver_verdict_notice("MALAVI-SUB-2026-000001", subject="s",
                                               body="b", transport=transport)
    payload = transport.calls[0]
    assert payload["mail_mode"] == "redirect"
    assert not any("redirect_to" in key for key in payload)
    assert "@" not in json.dumps({k: v for k, v in payload.items() if k != "actor_email"})


def test_a_reply_that_held_the_message_is_never_a_success(configured, monkeypatch):
    """The endpoint's own MAIL_MODE, a cap or the quota held it: the caller must not record
    a send that did not happen (finding 5)."""
    monkeypatch.setenv("MALAVI_MAIL_MODE", "live")
    transport = Recorder(held=[{"to": "a@example.org", "reason": "per-run cap"}], notified=1)
    with pytest.raises(MailHeld, match="per-run cap"):
        report_delivery.deliver_verdict_notice("MALAVI-SUB-2026-000001", subject="s",
                                               body="b", transport=transport)


def test_a_duplicate_held_by_the_endpoint_is_a_success(configured, monkeypatch):
    """The endpoint sent that exact message once already; recording it as sent is right."""
    monkeypatch.setenv("MALAVI_MAIL_MODE", "live")
    transport = Recorder(held=[{"to": "a@example.org", "reason": "duplicate"}], notified=0,
                         action="already_sent")
    delivered = report_delivery.deliver_verdict_notice("MALAVI-SUB-2026-000001",
                                                       subject="s", body="b",
                                                       transport=transport)
    assert delivered.action == "already_sent"


def test_an_attention_notice_already_sent_is_not_an_error(configured, monkeypatch):
    monkeypatch.setenv("MALAVI_MAIL_MODE", "live")
    transport = Recorder(action="already_sent", notified=0,
                         held=[{"to": "l@example.org", "reason": "duplicate"}])
    delivered = report_delivery.deliver_attention_notice(subject="s", body="b",
                                                         mail_key_="abc", transport=transport)
    assert delivered.action == "already_sent"
    assert transport.calls[0]["mail_key"] == "abc"


def test_an_old_endpoint_without_a_held_list_still_works(configured, monkeypatch):
    monkeypatch.setenv("MALAVI_MAIL_MODE", "live")
    transport = Recorder()
    del transport.reply["held"]
    del transport.reply["mail_mode"]
    delivered = report_delivery.deliver_verdict_notice("MALAVI-SUB-2026-000001",
                                                       subject="s", body="b",
                                                       transport=transport)
    assert delivered.notified == 2


def test_every_mail_carrying_payload_has_a_stable_key():
    """The endpoint dedupes on the key, so the same message twice is one message and a
    different message is not (finding 1: a body hash cannot do this)."""
    a = report_delivery.build_verdict_notice_payload("S", subject="s", body="b",
                                                     mail_key_="k1", issued_at=1)
    b = report_delivery.build_verdict_notice_payload("S", subject="s", body="b",
                                                     mail_key_="k1", issued_at=2)
    assert a["mail_key"] == b["mail_key"] == "k1"
    pdf = b"%PDF-1.7 x"
    assert (report_delivery.build_payload("S", pdf, issued_at=1)["mail_key"]
            == report_delivery.build_payload("S", pdf, issued_at=2)["mail_key"])
    assert (report_delivery.build_payload("S", pdf)["mail_key"]
            != report_delivery.build_payload("S", b"%PDF-1.7 y")["mail_key"])
    decline = report_delivery.build_decline_payload("S", to="a@example.org", submitter_name="A")
    assert decline["mail_key"] == report_delivery.build_decline_payload(
        "S", to="A@example.org ", submitter_name="B")["mail_key"]


def test_the_verdict_notice_audience_is_curators_or_leads_and_nothing_else():
    assert report_delivery.build_verdict_notice_payload(
        "S", subject="s", body="b", audience="leads")["audience"] == "leads"
    assert report_delivery.build_verdict_notice_payload(
        "S", subject="s", body="b", audience="everyone")["audience"] == "curators"


# ======================================================================================
# fetch_verdicts.py: the two refusals and the pending notices
# ======================================================================================

SUBMISSION = "MALAVI-SUB-2026-000001"


def _row(stamp="2026-08-02 09:00:00", email="alice@example.edu", verdict="Accept"):
    return {verdicts.COL_TIMESTAMP: stamp, verdicts.COL_EMAIL: email,
            verdicts.COL_SUBMISSION: SUBMISSION, verdicts.COL_REVISION: "1",
            verdicts.COL_ACTION: verdicts.ACTION_VERDICT, verdicts.COL_VERDICT: verdict,
            verdicts.COL_WHY: "checked", verdicts.COL_CHECKED: "Sequences"}


def _pending(fetch, *rows):
    return [(fetch.fingerprint(r), verdicts.parse_row(r), r) for r in rows]


def test_the_newest_applied_timestamp_ignores_unparseable_records(fetch):
    applied = {"a": {"at": "2026-08-01T00:00:00+00:00", "status": "applied"},
               "b": {"status": "unparseable"},
               "c": {"at": "2026-08-03T00:00:00+00:00", "status": "refused"}}
    assert fetch.newest_applied_at(applied) == "2026-08-03T00:00:00+00:00"
    assert fetch.newest_applied_at({}) == ""


def test_a_new_response_older_than_the_newest_applied_is_the_signature_of_a_reapply(fetch):
    """2026-10-16: all thirteen re-applied responses were older than the newest one
    already applied. A genuinely new response never is."""
    pending = _pending(fetch, _row("2026-08-01 09:00:00"), _row("2026-08-05 09:00:00"))
    old = fetch.older_than_applied(pending, "2026-08-03T00:00:00+00:00")
    assert old == [("2026-08-01T09:00:00+00:00", SUBMISSION)]
    assert fetch.older_than_applied(pending, "") == []       # a first run has no history


def test_the_environment_can_authorize_a_bulk_run_until_a_date(fetch, monkeypatch):
    from datetime import date
    monkeypatch.setenv("MALAVI_ALLOW_BULK_UNTIL", "2026-10-20")
    assert fetch.bulk_allowed_by_environment(date(2026, 10, 20))
    assert not fetch.bulk_allowed_by_environment(date(2026, 10, 21))
    monkeypatch.setenv("MALAVI_ALLOW_BULK_UNTIL", "soon")
    assert not fetch.bulk_allowed_by_environment(date(2026, 10, 1))
    monkeypatch.delenv("MALAVI_ALLOW_BULK_UNTIL")
    assert not fetch.bulk_allowed_by_environment()


def _run_main(fetch, tmp_path, monkeypatch, rows, applied=None, args=()):
    """Run main() against a CSV of rows and a temporary inbox; return (code, stderr)."""
    inbox = tmp_path / "submissions"
    inbox.mkdir(exist_ok=True)
    if applied is not None:
        fetch.save_applied(inbox / fetch.APPLIED_LEDGER_NAME, applied)
    header = list(rows[0]) if rows else list(_row())
    csv_text = ",".join(f'"{h}"' for h in header) + "\n" + "\n".join(
        ",".join(f'"{r[h]}"' for h in header) for r in rows) + "\n"
    csv_path = tmp_path / "responses.csv"
    csv_path.write_text(csv_text, encoding="utf-8")
    monkeypatch.setattr(fetch, "load_config", lambda: {
        "review": {"verdict_sheet_timezone": "UTC", "publish_hold_hours": 24,
                   "max_new_verdicts_per_run": 2},
        "submissions": {"inbox_dir": "submissions"}})
    monkeypatch.setattr(fetch, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(fetch.repo_state, "require_fresh", lambda *a, **k: None)
    code = fetch.main(["--from-csv", str(csv_path), "--no-publish", *args])
    return code, inbox


def test_more_new_responses_than_the_limit_are_refused_before_anything_is_written(
        fetch, tmp_path, monkeypatch, capsys):
    rows = [_row("2026-08-02 09:00:00"), _row("2026-08-02 09:01:00", "bob@example.edu"),
            _row("2026-08-02 09:02:00", "lead@example.edu")]
    code, inbox = _run_main(fetch, tmp_path, monkeypatch, rows)
    assert code == 2
    assert "REFUSED: 3 new responses" in capsys.readouterr().err
    assert not (inbox / fetch.APPLIED_LEDGER_NAME).is_file()
    assert not (inbox / "review_ledger.json").is_file()


def test_the_refusal_is_lifted_by_the_flag(fetch, tmp_path, monkeypatch, capsys):
    rows = [_row("2026-08-02 09:00:00"), _row("2026-08-02 09:01:00", "bob@example.edu"),
            _row("2026-08-02 09:02:00", "lead@example.edu")]
    code, inbox = _run_main(fetch, tmp_path, monkeypatch, rows, args=["--allow-bulk"])
    assert "REFUSED" not in capsys.readouterr().err
    assert (inbox / fetch.APPLIED_LEDGER_NAME).is_file()


def test_an_old_response_is_refused_even_when_under_the_count(fetch, tmp_path, monkeypatch,
                                                              capsys):
    applied = {"zzz": {"status": "applied", "at": "2026-08-03T00:00:00+00:00",
                       "submission": SUBMISSION, "notice": {"status": "none"}}}
    code, inbox = _run_main(fetch, tmp_path, monkeypatch, [_row("2026-08-02 09:00:00")],
                            applied=applied)
    err = capsys.readouterr().err
    assert code == 2
    assert "OLDER than the newest response already applied" in err
    assert "2026-08-02T09:00:00+00:00" in err
    # The applied-ledger is exactly as it was.
    assert set(fetch.load_applied(inbox / fetch.APPLIED_LEDGER_NAME)) == {"zzz"}


def test_the_age_refusal_is_lifted_by_allow_old(fetch, tmp_path, monkeypatch, capsys):
    applied = {"zzz": {"status": "applied", "at": "2026-08-03T00:00:00+00:00",
                       "notice": {"status": "none"}}}
    _run_main(fetch, tmp_path, monkeypatch, [_row("2026-08-02 09:00:00")], applied=applied,
              args=["--allow-old"])
    assert "REFUSED" not in capsys.readouterr().err


def test_a_dry_run_reports_the_refusal_too(fetch, tmp_path, monkeypatch, capsys):
    applied = {"zzz": {"status": "applied", "at": "2026-08-03T00:00:00+00:00",
                       "notice": {"status": "none"}}}
    code, _inbox = _run_main(fetch, tmp_path, monkeypatch, [_row("2026-08-02 09:00:00")],
                             applied=applied, args=["--dry-run"])
    assert code == 2 and "REFUSED" in capsys.readouterr().err


def test_the_audience_of_a_ledger_notice(fetch):
    """The form's trigger already told every curator about every entry (finding 9)."""
    base = {"status": "applied", "kind": "verdict"}
    assert fetch.notice_audience({**base, "verdict": "approve"}) == "curators"
    assert fetch.notice_audience({**base, "verdict": "hold"}) == "leads"
    assert fetch.notice_audience({**base, "verdict": "reject"}) == "leads"
    assert fetch.notice_audience({"status": "applied", "kind": "override"}) == "curators"
    assert fetch.notice_audience({"status": "applied", "kind": "retraction"}) == "curators"
    assert fetch.notice_audience({"status": "applied", "kind": "correction"}) == ""
    assert fetch.notice_audience({"status": "refused", "kind": "verdict",
                                  "verdict": "approve"}) == ""


def test_the_notice_key_is_the_response_not_the_ledger_id(fetch):
    """A re-applied response mints a new verdict id; the key must not move with it."""
    one = {"submission": SUBMISSION, "address": "Alice@example.edu",
           "at": "2026-08-02T09:00:00+00:00", "kind": "verdict", "verdict_id": "V1"}
    two = dict(one, verdict_id="V7")
    assert fetch.notice_key(one) == fetch.notice_key(two)
    assert fetch.notice_key(one) != fetch.notice_key(dict(one, at="2026-08-02T09:00:01+00:00"))


def _outcome(**overrides):
    base = {"status": "applied", "kind": "verdict", "verdict": "approve",
            "submission": SUBMISSION, "address": "alice@example.edu", "curator": "alice",
            "at": "2026-08-02T09:00:00+00:00", "verdict_id": "V1", "state": "approved",
            "revision": 1, "at_run": "2026-08-02T10:00:00+00:00"}
    base.update(overrides)
    base["notice"] = overrides.get("notice") or {"status": "pending", "audience": "curators",
                                                 "key": "k", "attempts": 0}
    return base


def test_a_held_notice_stays_pending_and_is_sent_by_a_later_run(fetch, monkeypatch):
    """A verdict applied in a session that could not send mail is announced by the next
    run that can (finding 4). Nothing is recorded as sent that was not."""
    monkeypatch.setattr(fetch, "_who", lambda curator_id, fallback_address="": "Alice")
    applied = {"a": _outcome()}
    calls = []

    def held(*args, **kwargs):
        calls.append(kwargs)
        raise MailHeld("MALAVI_MAIL_MODE is dry")

    counts = fetch.send_pending_notices(applied, {}, deliver=held,
                                        now="2026-08-02T11:00:00+00:00")
    assert counts == {"sent": 0, "held": 1, "failed": 0, "abandoned": 0}
    assert applied["a"]["notice"]["status"] == "pending"
    assert applied["a"]["notice"]["attempts"] == 1
    assert "dry" in applied["a"]["notice"]["held"]
    assert calls[0]["mail_key_"] == "k" and calls[0]["audience"] == "curators"

    class Sent:
        notified = 2
        action = "emailed"

    counts = fetch.send_pending_notices(applied, {}, deliver=lambda *a, **k: Sent(),
                                        now="2026-08-03T11:00:00+00:00")
    assert counts["sent"] == 1
    assert applied["a"]["notice"]["status"] == "sent"
    assert applied["a"]["notice"]["sent_at"] == "2026-08-03T11:00:00+00:00"
    assert "held" not in applied["a"]["notice"]
    # Sent once: nothing is pending any more.
    assert fetch.pending_notices(applied) == []


def test_a_notice_that_could_not_be_sent_for_two_weeks_is_abandoned_and_said_so(
        fetch, monkeypatch, capsys):
    monkeypatch.setattr(fetch, "_who", lambda curator_id, fallback_address="": "Alice")
    applied = {"a": _outcome(at_run="2026-08-02T10:00:00+00:00")}
    counts = fetch.send_pending_notices(applied, {}, deliver=lambda *a, **k: 1 / 0,
                                        now="2026-08-20T10:00:00+00:00")
    assert counts["abandoned"] == 1
    assert applied["a"]["notice"]["status"] == "abandoned"
    assert "tell the curators by hand" in capsys.readouterr().err


def test_a_failed_send_keeps_the_notice_pending_with_the_error(fetch, monkeypatch):
    monkeypatch.setattr(fetch, "_who", lambda curator_id, fallback_address="": "Alice")
    applied = {"a": _outcome()}

    def boom(*a, **k):
        raise RuntimeError("connection reset")

    counts = fetch.send_pending_notices(applied, {}, deliver=boom,
                                        now="2026-08-02T11:00:00+00:00")
    assert counts["failed"] == 1
    assert applied["a"]["notice"]["status"] == "pending"
    assert "connection reset" in applied["a"]["notice"]["last_error"]


def test_records_from_before_the_notice_bookkeeping_are_left_alone(fetch):
    """The thirteen records already in verdicts_applied.json have no `notice`; they must
    neither be announced again nor crash the run."""
    applied = {"old": {"status": "applied", "at": "2026-08-01T00:00:00+00:00"}}
    assert fetch.pending_notices(applied) == []
    assert fetch.send_pending_notices(applied, {}, deliver=lambda *a, **k: 1 / 0) == {
        "sent": 0, "held": 0, "failed": 0, "abandoned": 0}


def test_the_notice_wording_no_longer_claims_the_submitter_message_was_sent(fetch,
                                                                            monkeypatch):
    """Only the status sheet knows whether the trigger sent it (finding 16)."""
    monkeypatch.setattr(fetch, "_who", lambda curator_id, fallback_address="": "Alice")
    outcome = _outcome(verdict="hold", state="held", message_to_submitter="please re-read")
    subject, body = fetch._verdict_notice(outcome, "", {})
    assert "sent to them by the form's trigger" not in body
    assert "says whether it was sent" in body


# ======================================================================================
# The other two caps
# ======================================================================================

def _entry(submission_id, state="approved", names=("TUMIG19",)):
    from datetime import datetime, timedelta, timezone
    entry = ledger.Entry(submission_id=submission_id, track="A",
                         received_at=ledger.now_utc())
    entry.state = state
    entry.approved_at = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    entry.reserved_names = list(names)
    return entry


def test_notify_submitters_counts_the_due_ones_before_sending_any(notify):
    config = {"review": {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60}}
    entries = {f"MALAVI-SUB-2026-00000{i}": _entry(f"MALAVI-SUB-2026-00000{i}",
                                                   names=(f"TUMIG{10 + i}",))
               for i in range(1, 4)}
    entries["MALAVI-SUB-2026-000009"] = _entry("MALAVI-SUB-2026-000009", state="held")
    due = notify.due_submissions(entries, config)
    assert due == ["MALAVI-SUB-2026-000001", "MALAVI-SUB-2026-000002",
                   "MALAVI-SUB-2026-000003"]
    assert notify.due_submissions(entries, config, ["MALAVI-SUB-2026-000002"]) == [
        "MALAVI-SUB-2026-000002"]


def test_notify_submitters_bulk_authorization_reads_the_environment(notify, monkeypatch):
    monkeypatch.setenv("MALAVI_ALLOW_BULK_UNTIL", "2999-01-01")
    assert notify.bulk_allowed_by_environment()
    monkeypatch.setenv("MALAVI_ALLOW_BULK_UNTIL", "2000-01-01")
    assert not notify.bulk_allowed_by_environment()


# ======================================================================================
# A stale checkout may not write the review state (finding 17)
# ======================================================================================

def _git_stub(answers):
    def runner(command):
        key = " ".join(command)
        for fragment, answer in answers.items():
            if fragment in key:
                return answer
        return 0, ""
    return runner


def test_a_current_checkout_passes():
    runner = _git_stub({"rev-parse --abbrev-ref": (0, "main"),
                        "merge-base --is-ancestor": (0, "")})
    assert repo_state.behind_remote(runner) is None


def test_a_checkout_behind_its_remote_is_refused():
    runner = _git_stub({"rev-parse --abbrev-ref": (0, "main"),
                        "merge-base --is-ancestor": (1, "")})
    assert "behind origin/main" in repo_state.behind_remote(runner)
    with pytest.raises(repo_state.StaleCheckout, match="fetch_verdicts.py"):
        repo_state.require_fresh("fetch_verdicts.py",
                                 configuration={"intake": {"state_in_repo": True}},
                                 runner=runner)


def test_a_fetch_that_fails_cannot_prove_freshness():
    runner = _git_stub({"fetch": (128, "could not resolve host")})
    assert "could not fetch" in repo_state.behind_remote(runner)


def test_the_check_is_skipped_inside_the_cycle_and_with_standalone(monkeypatch):
    runner = _git_stub({"fetch": (128, "offline")})
    config = {"intake": {"state_in_repo": True}}
    repo_state.require_fresh("x", configuration=config, runner=runner, standalone=True)
    monkeypatch.setenv(repo_state.IN_CYCLE_ENV, "1")
    repo_state.require_fresh("x", configuration=config, runner=runner)
    monkeypatch.delenv(repo_state.IN_CYCLE_ENV)
    with pytest.raises(repo_state.StaleCheckout):
        repo_state.require_fresh("x", configuration=config, runner=runner)


def test_the_check_does_nothing_when_the_state_is_not_in_the_repository(monkeypatch):
    monkeypatch.delenv("MALAVI_STATE_IN_REPO", raising=False)
    runner = _git_stub({"fetch": (128, "offline")})
    repo_state.require_fresh("x", configuration={"intake": {"state_in_repo": False}},
                             runner=runner)


# ======================================================================================
# Code review of 2026-10-16: the fixes it asked for
# ======================================================================================

def test_a_held_submitter_notice_is_exit_2_not_a_failure(notify, tmp_path, monkeypatch,
                                                         capsys):
    """Finding 2: a held message must not halt the cycle before the state is committed."""
    import json as _json
    from malavi_curation import ledger as _ledger
    inbox = tmp_path / "submissions"
    inbox.mkdir()
    entries = {"MALAVI-SUB-2026-000001": _entry("MALAVI-SUB-2026-000001")}
    with _ledger.open_ledger(inbox, write=True) as live:
        live.update(entries)
    directory = inbox / "20260101T000000_Jane_Smith"
    directory.mkdir()
    (directory / "submission.json").write_text(_json.dumps(
        {"submitter": {"name": "Jane Smith", "email": "jane@example.edu"}}))
    (inbox / "submission_ids.json").write_text(_json.dumps(
        {"version": 1, "next": 2, "ids": {directory.name: {"id": "MALAVI-SUB-2026-000001",
                                                            "minted_at": "x"}}}))
    monkeypatch.setattr(notify, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(notify, "load_config", lambda: {
        "review": {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60,
                   "max_submitter_notices_per_run": 5}})
    monkeypatch.setattr(notify.repo_state, "require_fresh", lambda *a, **k: None)

    def held(*args, **kwargs):
        raise MailHeld("MALAVI_MAIL_MODE is dry")

    monkeypatch.setattr(notify, "deliver_name_confirmation", held)
    code = notify.main(["--no-publish"])
    out = capsys.readouterr().out
    assert code == 2, out
    assert "HELD" in out and "1 held" in out
    with _ledger.open_ledger(inbox, write=False) as after:
        assert not any(e.get("event") == "name_confirmation_sent"
                       for e in after["MALAVI-SUB-2026-000001"].history)


def test_the_environment_lifts_only_the_count_rule(fetch, tmp_path, monkeypatch, capsys):
    """Finding 6a: an authorized bulk window must not let a re-apply through."""
    monkeypatch.setenv("MALAVI_ALLOW_BULK_UNTIL", "2999-01-01")
    applied = {"zzz": {"status": "applied", "at": "2026-08-03T00:00:00+00:00",
                       "notice": {"status": "none"}}}
    code, _inbox = _run_main(fetch, tmp_path, monkeypatch, [_row("2026-08-02 09:00:00")],
                             applied=applied)
    assert code == 2 and "OLDER" in capsys.readouterr().err


def test_an_empty_applied_ledger_with_a_decided_review_ledger_is_refused(
        fetch, tmp_path, monkeypatch, capsys):
    """Finding 6b: a lost applied-ledger would re-apply the whole sheet."""
    monkeypatch.setattr(fetch, "ledger_has_decisions", lambda entries: True)
    inbox = tmp_path / "submissions"
    inbox.mkdir()
    code, _inbox = _run_main(fetch, tmp_path, monkeypatch, [_row("2026-08-02 09:00:00")])
    err = capsys.readouterr().err
    assert code == 2 and "empty or missing" in err
    assert not (tmp_path / "submissions" / fetch.APPLIED_LEDGER_NAME).is_file()
    code, _inbox = _run_main(fetch, tmp_path, monkeypatch, [_row("2026-08-02 09:00:00")],
                             args=["--allow-old"])
    assert "REFUSED" not in capsys.readouterr().err


def test_ledger_has_decisions(fetch):
    empty = ledger.Entry(submission_id="S", track="A", received_at=ledger.now_utc())
    assert not fetch.ledger_has_decisions({"S": empty})
    decided = ledger.Entry(submission_id="S", track="A", received_at=ledger.now_utc())
    decided.verdicts.append(object())
    assert fetch.ledger_has_decisions({"S": decided})


def test_one_bulk_parser_for_every_program(fetch, notify, monkeypatch):
    """Finding 7: 'soon' authorized publish_report; now nothing but a date does."""
    monkeypatch.setenv("MALAVI_ALLOW_BULK_UNTIL", "soon")
    assert not repo_state.bulk_allowed_by_environment()
    assert not fetch.bulk_allowed_by_environment()
    assert not notify.bulk_allowed_by_environment()
    text = (repo_root() / "curation" / "publish_report.py").read_text(encoding="utf-8")
    assert "repo_state.bulk_allowed_by_environment()" in text
    assert 'os.environ.get("MALAVI_ALLOW_BULK_UNTIL"' not in text


def test_the_report_carries_whether_the_curators_were_told_before(configured, monkeypatch):
    """Finding 10: 'updated' only when a notice was ever sent, not because Drive had the file."""
    monkeypatch.setenv("MALAVI_MAIL_MODE", "live")
    pdf = b"%PDF-1.7 x"
    transport = Recorder(fileId="F", url="u", action="updated",
                         sha256=report_delivery.hashlib.sha256(pdf).hexdigest(), notified=2)
    report_delivery.deliver("S", pdf, notify=True, transport=transport, announced_before=False)
    assert transport.calls[0]["announced_before"] is False
    report_delivery.deliver("S", pdf, notify=True, transport=transport)
    assert "announced_before" not in transport.calls[1]


def test_git_runs_without_a_terminal_prompt(monkeypatch):
    """Finding 18: an unattended fetch must fail, not hang, on a credential prompt."""
    seen = {}

    def fake_run(command, **kwargs):
        seen["env"] = kwargs.get("env") or {}

        class Done:
            returncode = 0
            stdout = "main"
        return Done()

    monkeypatch.setattr(repo_state.subprocess, "run", fake_run)
    repo_state._git(["rev-parse", "--abbrev-ref", "HEAD"])
    assert seen["env"].get("GIT_TERMINAL_PROMPT") == "0"


def test_enroll_and_promote_refuse_a_stale_checkout_too():
    """Finding 18: every program that writes the review state checks, not only the mailers."""
    for name in ("enroll.py", "promote.py"):
        text = (repo_root() / "curation" / name).read_text(encoding="utf-8")
        assert "repo_state.require_fresh(" in text, name
        assert "--standalone" in text, name
