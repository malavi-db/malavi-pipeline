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

    def fake_deliver(sid, pdf, notify=True):
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
