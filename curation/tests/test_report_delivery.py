"""Tests for curator report delivery (report_delivery.py).

The endpoint this talks to is a public URL protected by a shared secret, and the thing it
publishes is read by a curator who then makes a decision that changes MalAvi. So the tests
here are about the ways that could go wrong quietly:

* a report that is not a PDF, or is truncated, reaching a curator anyway;
* a corrected report landing on a *new* file id, leaving the link already in a curator's
  inbox pointing at the superseded version;
* the secret drifting into the repository, where it cannot be un-published;
* a signature that does not cover what is actually sent.

The transport is injected, so none of this needs a network or a deployed script.
"""
import base64
import hashlib
import hmac
import json
import re

import pytest

from malavi_curation import report_delivery
from malavi_curation.report_delivery import (
    DeliveryError, MAX_PDF_BYTES, build_payload, deliver, sign,
)

PDF = b"%PDF-1.7\nnot really a pdf, but it starts like one\n%%EOF\n"
SECRET = b"0123456789abcdef0123456789abcdef"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """A working configuration pointing at a secret outside any repository."""
    secret_file = tmp_path / "report_secret.txt"
    secret_file.write_text(SECRET.decode(), encoding="utf-8")
    monkeypatch.setenv("MALAVI_REPORT_ENDPOINT", "https://script.google.com/x/exec")
    monkeypatch.setenv("MALAVI_REPORT_SECRET_FILE", str(secret_file))
    return secret_file


class Recorder:
    """A transport that records what it was given and replies as the endpoint would."""

    def __init__(self, **overrides):
        self.url = None
        self.body = None
        self.overrides = overrides

    def __call__(self, url, body):
        self.url = url
        self.body = body
        payload = json.loads(body)
        reply = {
            "ok": True,
            "fileId": "FILE123",
            "url": "https://drive.google.com/file/d/FILE123/view",
            "action": "created",
            "sha256": payload["sha256"],
            "notified": 1,
        }
        reply.update(self.overrides)
        return reply


# --------------------------------------------------------------------------------------
# What may be published at all
# --------------------------------------------------------------------------------------

def test_refuses_a_file_that_is_not_a_pdf():
    """The renderer has a history of producing empty or half-built output.

    A curator who opens a blank report cannot tell it apart from a submission with nothing
    wrong, which is the worst possible confusion for this particular document.
    """
    with pytest.raises(DeliveryError, match="%PDF"):
        build_payload("20260101T000000_X", b"<html>oops</html>")


def test_refuses_an_empty_report():
    with pytest.raises(DeliveryError, match="empty"):
        build_payload("20260101T000000_X", b"")


def test_refuses_an_oversized_report():
    with pytest.raises(DeliveryError, match="limit"):
        build_payload("20260101T000000_X", b"%PDF" + b"\0" * MAX_PDF_BYTES)


@pytest.mark.parametrize("bad_id", ["", "../escape", "dir/sub"])
def test_refuses_a_submission_id_that_could_escape_the_folder(bad_id):
    with pytest.raises(DeliveryError):
        build_payload(bad_id, PDF)


# --------------------------------------------------------------------------------------
# Idempotency -- the property that keeps an emailed link valid
# --------------------------------------------------------------------------------------

def test_filename_depends_only_on_the_submission_id():
    """Re-publishing must overwrite, not accumulate.

    The endpoint finds the previous file by name. If the name carried a timestamp or a
    revision, a corrected report would become a second file, and the curator would go on
    reading the first one from the link already in their inbox.
    """
    first = build_payload("20260101T000000_X", PDF)
    second = build_payload("20260101T000000_X", PDF + b"corrected\n")
    assert first["filename"] == second["filename"] == "20260101T000000_X_report.pdf"


def test_checksum_covers_the_content_not_the_name():
    first = build_payload("20260101T000000_X", PDF)
    second = build_payload("20260101T000000_X", PDF + b"corrected\n")
    assert first["sha256"] != second["sha256"]
    assert first["sha256"] == hashlib.sha256(PDF).hexdigest()


# --------------------------------------------------------------------------------------
# The signature
# --------------------------------------------------------------------------------------

def test_signature_is_over_the_exact_bytes_sent(configured):
    """Whatever the endpoint hashes must be what it received, byte for byte.

    The two sides agree by hashing the serialized body rather than a reconstructed
    canonical form, so this test asserts that relationship directly.
    """
    recorder = Recorder()
    deliver("20260101T000000_X", PDF, transport=recorder)

    signature = recorder.url.split("sig=")[1]
    expected = hmac.new(SECRET, recorder.body, hashlib.sha256).hexdigest()
    assert signature == expected


def test_body_carries_the_pdf_and_a_timestamp(configured):
    recorder = Recorder()
    deliver("20260101T000000_X", PDF, transport=recorder)

    payload = json.loads(recorder.body)
    assert base64.b64decode(payload["pdf_b64"]) == PDF
    assert payload["issued_at"] > 0
    assert payload["notify"] is True


def test_notify_can_be_turned_off(configured):
    recorder = Recorder()
    deliver("20260101T000000_X", PDF, notify=False, transport=recorder)
    assert json.loads(recorder.body)["notify"] is False


def test_sign_is_stable():
    body = b'{"a":1}'
    assert sign(body, SECRET) == sign(body, SECRET)
    assert sign(body, SECRET) != sign(body + b" ", SECRET)


# --------------------------------------------------------------------------------------
# What the endpoint says back
# --------------------------------------------------------------------------------------

def test_a_refusal_is_raised_with_the_endpoint_reason(configured):
    recorder = Recorder(ok=False, error="bad signature")
    with pytest.raises(DeliveryError, match="bad signature"):
        deliver("20260101T000000_X", PDF, transport=recorder)


def test_a_checksum_mismatch_is_treated_as_corruption(configured):
    """If Drive holds different bytes than we sent, the link must not be trusted.

    This is the case where everything reports success and the curator reads a truncated
    report, so it has to fail loudly rather than return a Delivered.
    """
    recorder = Recorder(sha256="0" * 64)
    with pytest.raises(DeliveryError, match="checksum"):
        deliver("20260101T000000_X", PDF, transport=recorder)


def test_a_successful_delivery_reports_what_the_endpoint_did(configured):
    result = deliver("20260101T000000_X", PDF, transport=Recorder(action="updated"))
    assert result.file_id == "FILE123"
    assert result.action == "updated"
    assert result.created is False
    assert result.notified == 1


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def test_refuses_a_secret_stored_inside_the_repository(monkeypatch):
    """Unlike a service-account key, a leaked shared secret cannot be revoked in a console.

    It has to be rotated in two places at once, so keeping it out of the tree matters more
    here, not less.
    """
    inside = report_delivery.repo_root() / "report_secret.txt"
    monkeypatch.setenv("MALAVI_REPORT_SECRET_FILE", str(inside))
    with pytest.raises(DeliveryError, match="inside the repository"):
        report_delivery.secret_path()


def test_missing_endpoint_says_what_to_do(tmp_path, monkeypatch):
    secret_file = tmp_path / "s.txt"
    secret_file.write_text(SECRET.decode(), encoding="utf-8")
    monkeypatch.setenv("MALAVI_REPORT_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("MALAVI_REPORT_ENDPOINT", "")
    monkeypatch.setattr(report_delivery, "load_config", lambda: {"google": {}})
    with pytest.raises(DeliveryError, match="publish_report.gs"):
        deliver("20260101T000000_X", PDF, transport=Recorder())


def test_missing_secret_says_how_to_make_one(tmp_path, monkeypatch):
    monkeypatch.setenv("MALAVI_REPORT_ENDPOINT", "https://script.google.com/x/exec")
    monkeypatch.setenv("MALAVI_REPORT_SECRET_FILE", str(tmp_path / "absent.txt"))
    with pytest.raises(DeliveryError, match="No report secret at"):
        report_delivery.load_secret()


def test_whitespace_around_the_secret_is_stripped(tmp_path, monkeypatch):
    """A secret pasted from a browser or saved on Windows carries a trailing newline.

    Without stripping, every signature would be wrong and the endpoint would return
    'bad signature', which points at the wrong problem entirely.
    """
    secret_file = tmp_path / "s.txt"
    secret_file.write_text(SECRET.decode() + "\r\n", encoding="utf-8")
    monkeypatch.setenv("MALAVI_REPORT_SECRET_FILE", str(secret_file))
    assert report_delivery.load_secret() == SECRET


# --------------------------------------------------------------------------------------
# The program, not just the module
# --------------------------------------------------------------------------------------

import importlib.util                                                   # noqa: E402

from malavi_curation.config import repo_root                            # noqa: E402


@pytest.fixture(scope="module")
def cli():
    """The publish program, loaded from the script it lives in."""
    path = repo_root() / "curation" / "publish_report.py"
    spec = importlib.util.spec_from_file_location("_publish_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publishing_is_written_into_the_ledger(cli, tmp_path, monkeypatch, capsys):
    """A publish nobody recorded is indistinguishable from one that never happened.

    That question -- "was this curator ever actually sent anything?" -- is exactly what
    gets asked when a submission has been sitting unreviewed, so the answer has to be in
    the ledger rather than in an inbox.
    """
    from malavi_curation import ledger
    from malavi_curation.report_delivery import Delivered

    inbox = tmp_path / "submissions"
    directory = "20260101T000000_Jane_Smith"      # carries the submitter's NAME
    public_id = "MALAVI-SUB-2026-000001"          # what may leave this machine
    (inbox / directory).mkdir(parents=True)
    (inbox / directory / "report.pdf").write_bytes(PDF)
    (inbox / "submission_ids.json").write_text(
        json.dumps({"ids": {directory: {"id": public_id}}}), encoding="utf-8")

    with ledger.open_ledger(inbox, write=True) as entries:
        ledger.ensure_entry(entries, public_id, "A", ledger.now_utc())

    monkeypatch.setattr(cli, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cli, "describe", lambda: "Report delivery: stubbed")
    # Capture what the delivery layer is handed: it becomes the Drive filename and the
    # curator email subject, so it must be the opaque id and never the directory name.
    handed = {}

    def fake_deliver(sid, pdf, notify=True, **_kwargs):
        handed["submission_id"] = sid
        return Delivered(sid, "FILE123", "https://drive/FILE123", "created", 1)

    monkeypatch.setattr(cli, "deliver", fake_deliver)

    assert cli.main([directory]) == 0
    assert handed["submission_id"] == public_id, (
        "the submitter's name must never reach Drive or a curator's inbox")

    with ledger.open_ledger(inbox, write=False) as entries:
        history = entries[public_id].history
    published = [event for event in history if event.get("event") == "report_published"]
    assert len(published) == 1
    assert published[0]["file_id"] == "FILE123"
    assert published[0]["action"] == "created"


def test_a_missing_report_says_to_run_the_screen_first(cli, tmp_path, monkeypatch):
    """"No such submission" and "not screened yet" need different actions from the operator."""
    inbox = tmp_path / "submissions"
    (inbox / "20260101T000000_X").mkdir(parents=True)
    monkeypatch.setattr(cli, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cli, "describe", lambda: "stubbed")

    assert cli.main(["20260101T000000_X"]) == 1


def test_an_unknown_submission_id_is_not_confused_with_an_unscreened_one(cli, tmp_path,
                                                                        monkeypatch):
    inbox = tmp_path / "submissions"
    inbox.mkdir(parents=True)
    monkeypatch.setattr(cli, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cli, "describe", lambda: "stubbed")

    assert cli.main(["typo_in_the_id"]) == 1


# --------------------------------------------------------------------------------------
# Which code is deployed? (the describe action, added 2026-10-02)
# --------------------------------------------------------------------------------------

def _describing(**reply):
    """A transport answering a describe request the way the deployed script would."""
    seen = {}

    def transport(url, body):
        seen["payload"] = json.loads(body)
        answer = {"ok": True, "action": "described",
                  "code_version": report_delivery.repository_code_version(),
                  "actions": list(report_delivery.ACTIONS_SENT), "curators": 5}
        answer.update(reply)
        return answer

    transport.seen = seen
    return transport


def test_describe_sends_a_signed_request_that_carries_nothing_but_the_action(configured):
    transport = _describing()
    deployment = report_delivery.describe_live(transport=transport, issued_at=1_700_000_000)
    assert transport.seen["payload"] == {"action": "describe", "issued_at": 1_700_000_000}
    assert deployment.current
    assert deployment.curators == 5
    assert deployment.missing_actions == ()


def test_an_older_version_is_reported_as_needing_a_redeploy(configured):
    deployment = report_delivery.describe_live(
        transport=_describing(code_version="2026-01-01"))
    assert not deployment.current
    text = report_delivery.describe_deployment(deployment)
    assert "REDEPLOY NEEDED" in text and "2026-01-01" in text


def test_a_deployed_script_missing_an_action_is_named(configured):
    deployment = report_delivery.describe_live(
        transport=_describing(actions=["publish_report", "describe"]))
    assert set(deployment.missing_actions) == {"confirm_names", "decline_notice",
                                               "verdict_notice", "attention_notice",
                                               "status"}
    assert "decline_notice" in report_delivery.describe_deployment(deployment)


def test_a_deployment_older_than_describe_itself_is_recognized(configured):
    """An old router sends an unknown action to the PDF branch, which refuses it."""
    def old_script(url, body):
        return {"ok": False, "error": "unusable filename"}

    with pytest.raises(report_delivery.StaleDeployment, match="older than the repository"):
        report_delivery.describe_live(transport=old_script)


def test_a_curator_count_that_differs_from_the_registry_is_pointed_out(configured):
    deployment = report_delivery.describe_live(transport=_describing(curators=4))
    assert "disagree" in report_delivery.describe_deployment(deployment,
                                                             registry_curators=5)
    assert "disagree" not in report_delivery.describe_deployment(deployment,
                                                                 registry_curators=4)


def test_a_publish_that_emailed_nobody_is_an_error_not_a_success(configured):
    """notify=True and notified=0 means the report sits in Drive unannounced; the caller
    would record it as sent. Raise so nothing is recorded and it is retried."""
    with pytest.raises(DeliveryError, match="emailed nobody"):
        deliver("20260101T000000_X", PDF, notify=True, transport=Recorder(notified=0))
    # With notification switched off, zero is the expected answer.
    result = deliver("20260101T000000_X", PDF, notify=False, transport=Recorder(notified=0))
    assert result.notified == 0


def test_the_script_declares_every_action_python_sends():
    """An action added on one side and not the other fails here, not in production."""
    script = report_delivery.script_path().read_text(encoding="utf-8")
    block = re.search(r"var ACTIONS = \[(.*?)\];", script, flags=re.DOTALL)
    assert block, "ACTIONS is not declared in publish_report.gs"
    declared = set(re.findall(r"'([^']+)'", block.group(1)))
    assert declared == set(report_delivery.ACTIONS_SENT)
    # ...and each one is actually routed, not only listed.
    for action in declared - {"publish_report"}:      # the PDF branch is the fall-through
        assert f"action === '{action}'" in script, f"{action} is listed but not routed"


# --------------------------------------------------------------------------------------
# What --all-pending may send (review of 2026-10-02, findings 1 and 2)
# --------------------------------------------------------------------------------------

def _inbox_with(tmp_path, submissions):
    """An inbox holding ``{directory: (public_id, ledger state or None)}``, each with a
    rendered report. ``None`` leaves the submission out of the review ledger."""
    from malavi_curation import ledger

    inbox = tmp_path / "submissions"
    ids = {}
    for directory, (public_id, _state) in submissions.items():
        (inbox / directory).mkdir(parents=True)
        (inbox / directory / "report.pdf").write_bytes(PDF)
        ids[directory] = {"id": public_id}
    (inbox / "submission_ids.json").write_text(json.dumps({"ids": ids}), encoding="utf-8")
    with ledger.open_ledger(inbox, write=True) as entries:
        for _directory, (public_id, state) in submissions.items():
            if state is None:
                continue
            entry = ledger.ensure_entry(entries, public_id, "A", ledger.now_utc())
            entry.state = state
    return inbox


def test_all_pending_sends_only_live_enrolled_unexcluded_reports(cli, tmp_path, monkeypatch,
                                                                 capsys):
    from malavi_curation import ledger
    from malavi_curation.report_delivery import Delivered

    inbox = _inbox_with(tmp_path, {
        "20260101T000000_live": ("MALAVI-SUB-0000-000001", "ready_for_review"),
        "20260102T000000_withdrawn": ("MALAVI-SUB-0000-000002", "withdrawn"),
        "20260103T000000_released": ("MALAVI-SUB-0000-000003", "released"),
        "20260104T000000_not_enrolled": ("MALAVI-SUB-0000-000004", None),
        "20260105T000000_demo": ("MALAVI-SUB-0000-000005", "ready_for_review"),
    })
    monkeypatch.setattr(cli, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cli, "describe", lambda: "stubbed")
    monkeypatch.setattr(cli, "excluded_directories", lambda: {"20260105T000000_demo"})
    sent = []
    monkeypatch.setattr(cli, "deliver", lambda sid, pdf, notify=True, **_kw: (
        sent.append(sid) or Delivered(sid, "F", "https://drive/F", "created", 1)))

    assert cli.main(["--all-pending"]) == 0
    assert sent == ["MALAVI-SUB-0000-000001"]
    printed = capsys.readouterr().out
    # Nothing is held back silently.
    assert "20260105T000000_demo: excluded in config" in printed
    assert "MALAVI-SUB-0000-000002: its state is withdrawn" in printed
    assert "MALAVI-SUB-0000-000004: not in the review ledger" in printed

    # ...and the second run has nothing to send: the publish was recorded.
    sent.clear()
    assert cli.main(["--all-pending"]) == 0
    assert sent == []
    with ledger.open_ledger(inbox, write=False) as entries:
        assert all("report_published" not in [h.get("event") for h in entries[pid].history]
                   for pid in ("MALAVI-SUB-0000-000002", "MALAVI-SUB-0000-000003"))


def test_a_failed_delivery_does_not_lose_the_record_of_the_ones_already_sent(
        cli, tmp_path, monkeypatch):
    from malavi_curation import ledger
    from malavi_curation.report_delivery import Delivered

    inbox = _inbox_with(tmp_path, {
        "20260101T000000_first": ("MALAVI-SUB-0000-000001", "ready_for_review"),
        "20260102T000000_second": ("MALAVI-SUB-0000-000002", "ready_for_review"),
        "20260103T000000_third": ("MALAVI-SUB-0000-000003", "ready_for_review"),
    })
    monkeypatch.setattr(cli, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cli, "describe", lambda: "stubbed")
    monkeypatch.setattr(cli, "excluded_directories", lambda: set())

    def deliver(sid, pdf, notify=True, **_kw):
        if sid.endswith("2"):
            raise DeliveryError("The endpoint returned HTTP 503.")
        return Delivered(sid, "F" + sid[-1], "https://drive/F", "created", 1)

    monkeypatch.setattr(cli, "deliver", deliver)

    assert cli.main(["--all-pending"]) == 1                 # a failure is still a failure
    with ledger.open_ledger(inbox, write=False) as entries:
        recorded = {pid: any(h.get("event") == "report_published" for h in entry.history)
                    for pid, entry in entries.items()}
    # The first was sent before the failure and the third after it; both are on record,
    # so neither is sent again. Only the one that failed is still pending.
    assert recorded == {"MALAVI-SUB-0000-000001": True, "MALAVI-SUB-0000-000002": False,
                        "MALAVI-SUB-0000-000003": True}


def test_naming_a_submission_still_sends_it_whatever_its_state(cli, tmp_path, monkeypatch):
    """--all-pending is filtered; an id typed on the command line is a deliberate act."""
    from malavi_curation.report_delivery import Delivered

    inbox = _inbox_with(tmp_path, {
        "20260102T000000_withdrawn": ("MALAVI-SUB-0000-000002", "withdrawn")})
    monkeypatch.setattr(cli, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cli, "describe", lambda: "stubbed")
    sent = []
    monkeypatch.setattr(cli, "deliver", lambda sid, pdf, notify=True, **_kw: (
        sent.append(sid) or Delivered(sid, "F", "https://drive/F", "created", 1)))
    assert cli.main(["MALAVI-SUB-0000-000002"]) == 0
    assert sent == ["MALAVI-SUB-0000-000002"]


def test_a_newer_revision_makes_the_report_pending_again(cli, tmp_path, monkeypatch):
    """A correction or a submitter's update records a revision; the re-screened report
    has to reach the curators again, in place of the old one."""
    from malavi_curation import ledger
    from malavi_curation.report_delivery import Delivered

    inbox = _inbox_with(tmp_path, {
        "20260101T000000_one": ("MALAVI-SUB-0000-000001", "held")})
    monkeypatch.setattr(cli, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cli, "describe", lambda: "stubbed")
    monkeypatch.setattr(cli, "excluded_directories", lambda: set())
    sent = []
    monkeypatch.setattr(cli, "deliver", lambda sid, pdf, notify=True, **_kw: (
        sent.append(sid) or Delivered(sid, "F", "https://drive/F", "updated", 1)))

    assert cli.main(["--all-pending"]) == 0 and sent == ["MALAVI-SUB-0000-000001"]
    assert cli.main(["--all-pending"]) == 0 and len(sent) == 1       # current: not again

    with ledger.open_ledger(inbox, write=True) as entries:
        ledger.bump_revision(entries["MALAVI-SUB-0000-000001"],
                             "the submitter sent a corrected workbook")
    assert cli.main(["--all-pending"]) == 0
    assert len(sent) == 2                                              # the revision's report
    assert cli.main(["--all-pending"]) == 0 and len(sent) == 2


def test_a_report_published_before_revisions_were_recorded_is_still_current(cli, tmp_path,
                                                                           monkeypatch):
    """Older report_published events carry no revision number; they are read by time, and
    a first report in the same second as the entry's own first revision is current."""
    from malavi_curation import ledger
    from malavi_curation.report_delivery import Delivered

    inbox = _inbox_with(tmp_path, {"20260101T000000_one": ("MALAVI-SUB-0000-000001", "held")})
    with ledger.open_ledger(inbox, write=True) as entries:
        entries["MALAVI-SUB-0000-000001"].history.append(
            {"event": "report_published", "at": ledger.now_utc(), "file_id": "OLD"})
    monkeypatch.setattr(cli, "submissions_inbox", lambda: inbox)
    monkeypatch.setattr(cli, "describe", lambda: "stubbed")
    monkeypatch.setattr(cli, "excluded_directories", lambda: set())
    sent = []
    monkeypatch.setattr(cli, "deliver", lambda sid, pdf, notify=True, **_kw: (
        sent.append(sid) or Delivered(sid, "F", "https://drive/F", "updated", 1)))
    assert cli.main(["--all-pending"]) == 0 and sent == []



# --------------------------------------------------------------------------------------
# 2026-10-16: the submitter on a publish, and the status action
# --------------------------------------------------------------------------------------

def test_a_payload_built_the_old_way_is_unchanged():
    """Callers that pass nothing new must produce exactly the keys they always did."""
    payload = build_payload("20260101T000000_X", PDF, issued_at=1)
    assert set(payload) == {"action", "submission_id", "filename", "sha256", "issued_at",
                            "notify", "pdf_b64", "mail_key"}


def test_the_submitter_and_the_assign_flag_ride_with_the_report():
    payload = build_payload("20260101T000000_X", PDF, issued_at=1,
                            submitter={"submitter_name": "Jane Smith",
                                       "submitter_email": "jane@example.edu",
                                       "received_at": "2026-10-07T11:55:31+00:00",
                                       "revision": "1"},
                            assign=False)
    assert payload["submitter_name"] == "Jane Smith"
    assert payload["submitter_email"] == "jane@example.edu"
    assert payload["received_at"] == "2026-10-07T11:55:31+00:00"
    assert payload["revision"] == "1"
    assert payload["assign"] is False
    # Blank contact details are left out rather than sent as empty strings, so the sheet
    # keeps whatever it already has.
    sparse = build_payload("20260101T000000_X", PDF, issued_at=1,
                           submitter={"submitter_name": "", "submitter_email": None})
    assert "submitter_name" not in sparse and "submitter_email" not in sparse


def test_deliver_reports_the_assignment_the_endpoint_made():
    class Recorder:
        def __call__(self, url, body):
            payload = json.loads(body)
            return {"ok": True, "fileId": "F", "url": "https://x/F", "action": "created",
                    "sha256": payload["sha256"], "notified": 5,
                    "assignee": "luz@example.org", "assigned": True, "sheet_error": ""}
    result = deliver("20260101T000000_X", PDF, transport=Recorder(),
                     submitter={"submitter_email": "jane@example.edu"})
    assert result.assignee == "luz@example.org" and result.assigned is True
    assert result.sheet_error == ""


def test_an_old_endpoint_reply_without_assignment_fields_still_works():
    class Recorder:
        def __call__(self, url, body):
            payload = json.loads(body)
            return {"ok": True, "fileId": "F", "url": "https://x/F", "action": "created",
                    "sha256": payload["sha256"], "notified": 5}
    result = deliver("20260101T000000_X", PDF, transport=Recorder())
    assert result.assignee == "" and result.assigned is False


def test_a_status_payload_carries_only_the_allowed_keys():
    rows = [{"id": "MALAVI-SUB-2026-000011", "received": "2026-10-09T10:44:20+00:00",
             "state": "ready_for_review", "waiting_on": "verdict",
             "waiting_since": "2026-10-14T14:01:00Z", "hold_ends": "", "embargoed": False,
             "names": 1, "revision": 1, "report_url": "https://x/R", "report_at": "2026-10-14",
             "submitter_name": "A Person", "submitter_email": "a@example.org"}]
    people = [{"name": "Luz", "email": "luz@example.org", "role": "curator", "active": True}]
    payload = report_delivery.build_status_payload(rows, people, issued_at=1)
    assert payload["action"] == "status"
    assert payload["rows"] == rows
    assert payload["curators"] == people
    assert payload["issued_at"] == 1


def test_a_status_payload_refuses_reason_text_or_any_other_stray_key():
    """The sheet is shared with every curator; the ledger's free text must not leak onto
    it by a caller adding a field. Refused, never silently dropped."""
    with pytest.raises(DeliveryError, match="keys the sheet may not hold"):
        report_delivery.build_status_payload(
            [{"id": "MALAVI-SUB-2026-000011", "state": "held", "reason_text": "the trace"}])
    with pytest.raises(DeliveryError, match="no submission id"):
        report_delivery.build_status_payload([{"state": "held"}])
    with pytest.raises(DeliveryError, match="keys the sheet may not hold"):
        report_delivery.build_status_payload([], [{"name": "X", "email": "x@y.z",
                                                   "role": "lead", "active": True,
                                                   "aliases": ["a@b.c"]}])


def test_deliver_status_posts_the_signed_payload_and_returns_the_reply(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("s" * 64)
    secret.chmod(0o600)
    monkeypatch.setenv(report_delivery.SECRET_ENV, str(secret))
    monkeypatch.setenv(report_delivery.ENDPOINT_ENV, "https://script.example/exec")
    seen = {}

    def transport(url, body):
        seen["url"] = url
        seen["payload"] = json.loads(body)
        return {"ok": True, "action": "synced", "rows": 1, "created": 1, "curators": 1,
                "synced_at": "2026-10-16T12:00:00Z"}

    reply = report_delivery.deliver_status(
        [{"id": "MALAVI-SUB-2026-000011", "state": "ready_for_review"}],
        [{"name": "Luz", "email": "luz@example.org", "role": "curator", "active": True}],
        transport=transport)
    assert reply["rows"] == 1
    assert seen["payload"]["action"] == "status"
    assert "sig=" in seen["url"]


def test_a_missing_status_sheet_is_a_delivery_error_the_caller_can_recognize(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("s" * 64)
    secret.chmod(0o600)
    monkeypatch.setenv(report_delivery.SECRET_ENV, str(secret))
    monkeypatch.setenv(report_delivery.ENDPOINT_ENV, "https://script.example/exec")
    with pytest.raises(DeliveryError, match="STATUS_SHEET_ID"):
        report_delivery.deliver_status(
            [{"id": "MALAVI-SUB-2026-000011", "state": "held"}],
            transport=lambda url, body: {"ok": False, "error": "STATUS_SHEET_ID is not set"})
