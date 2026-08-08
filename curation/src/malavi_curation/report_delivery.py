"""Send a rendered curator report to Drive, so that a curator can actually read it.

**The gap this closes.** ``check_template.py`` renders ``report.pdf`` into the intake tree
on BIOMIX. The intake tree is gitignored, on a machine no curator has an account on. The
submission email has been promising "the report with the check results follows separately"
since the day it was written, and nothing has ever made that true. This is the thing that
makes it true.

Why not a service account, which was the plan
---------------------------------------------
The design of 2026-08-07 called for a second service account, ``malavi-publisher@``, that
would upload the PDF to a Drive folder. It was probed before being built, and it cannot
work::

    HTTP 403  storageQuotaExceeded
    "Service Accounts do not have storage quota. Leverage shared drives, or use
     OAuth delegation instead."

A file created by a service account is *owned* by that service account, whatever folder it
is placed in, and a service account has no Drive storage of its own. Shared Drives would
solve it and require Google Workspace; ``malaviadmin@gmail.com`` is a consumer account.
Domain-wide delegation likewise needs a Workspace domain. So no key on this machine can
create a file in MalAvi's Drive, and no amount of sharing changes that.

What happens instead
--------------------
An Apps Script web app (``curation/apps_script/publish_report.gs``) runs *as*
``malaviadmin@gmail.com``. This module POSTs the PDF to it; the script writes the file, so
the file is owned by the human account whose storage it is. The same request also triggers
the curator email, which is a bonus the service-account design did not have: it would have
needed a polling trigger to notice a new file, so a report could sit unannounced for up to
an hour.

The cost, stated plainly
------------------------
The web app must be deployed "anyone can access", because BIOMIX cannot hold a Google user
credential. So the endpoint is public and its only protection is the shared secret in this
module. That is a real downgrade from a service-account key, and it is why:

* every request is signed with HMAC-SHA256 over the exact body bytes, and the script
  rejects an unsigned or wrongly-signed request before it looks at anything else;
* the signature covers a timestamp, and the script rejects anything older than
  ``MAX_AGE_SECONDS``, so a captured request cannot be replayed tomorrow;
* the secret lives outside the repository, in a file this module refuses to read from
  inside the tree — the same rule, and for the same reason, as
  :mod:`malavi_curation.google_auth`;
* the endpoint can only ever do one thing: write a PDF into one folder. It cannot read a
  submission, cannot list the folder, and cannot touch anything else in Drive.

What an attacker who obtains the secret can do is put a PDF in the reports folder and cause
an email to curators. That is unpleasant and recoverable. It is not access to submitter
data, which is the thing worth protecting.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from .config import load_config, repo_root

# The endpoint refuses anything older than this. Ten minutes is generous for a POST from
# BIOMIX and short enough that a captured request is not a lasting credential. It must
# match MAX_AGE_SECONDS in publish_report.gs; the test suite does not check the .gs, so
# changing one means changing the other by hand.
MAX_AGE_SECONDS = 600

# Apps Script's own limit on a POST body is about 50 MB, and base64 inflates by a third.
# Curator reports run to a few hundred KB; anything approaching this ceiling means
# something has gone wrong upstream, so refuse it here with a clear message rather than
# letting Google return an opaque failure.
MAX_PDF_BYTES = 8 * 1024 * 1024

ENDPOINT_ENV = "MALAVI_REPORT_ENDPOINT"
SECRET_ENV = "MALAVI_REPORT_SECRET_FILE"


class DeliveryError(RuntimeError):
    """Delivery could not even be attempted, or the endpoint refused it.

    The message is written for whoever has to fix the configuration, not for a log.
    """


@dataclass(frozen=True)
class Delivered:
    """What the endpoint did, as reported by the endpoint itself."""

    submission_id: str
    file_id: str
    url: str
    action: str          # "created" or "updated"
    notified: int        # how many curators were emailed; 0 when notification was off

    @property
    def created(self) -> bool:
        return self.action == "created"


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def endpoint_url() -> Optional[str]:
    """The deployed web app URL, or None if it has not been configured yet.

    Environment first so a test deployment can be exercised without editing tracked
    configuration, then ``google.report_endpoint`` in config/project.yml.
    """
    from_env = os.environ.get(ENDPOINT_ENV, "").strip()
    if from_env:
        return from_env
    configured = (load_config().get("google") or {}).get("report_endpoint")
    configured = str(configured).strip() if configured else ""
    return configured or None


def secret_path() -> Optional[Path]:
    """Where the shared secret lives, or None if not configured."""
    from_env = os.environ.get(SECRET_ENV, "").strip()
    if from_env:
        return _checked(Path(from_env).expanduser())
    configured = (load_config().get("google") or {}).get("report_secret_file")
    if configured:
        return _checked(Path(str(configured)).expanduser())
    return None


def _checked(path: Path) -> Path:
    """Refuse a secret stored inside the repository.

    Identical in spirit to :func:`malavi_curation.google_auth._checked`, and deliberately
    duplicated rather than shared: the two credentials have different lifecycles and a
    future change to one should not silently change the other. A shared secret in the tree
    is one ``git add -f`` from being permanent, and unlike a service-account key it cannot
    be revoked from a console — it has to be rotated here and in the Apps Script project
    at the same time.
    """
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(repo_root().resolve())
    except ValueError:
        return resolved
    raise DeliveryError(
        f"The report secret is at {resolved}, inside the repository. Move it outside the "
        f"tree (~/.config/malavi/ is the documented home, mode 600) and point "
        f"${SECRET_ENV} or google.report_secret_file at it.")


def load_secret() -> bytes:
    """The shared secret as bytes, or a DeliveryError explaining what to do.

    Whitespace is stripped for the same reason the publishing token is: a secret pasted on
    Windows or copied out of a browser carries a trailing newline or carriage return that
    would silently change every signature, and the resulting 401 says nothing useful.
    """
    path = secret_path()
    if path is None:
        raise DeliveryError(
            "No report secret configured. Generate one (`openssl rand -hex 32`), save it "
            "at ~/.config/malavi/report_secret.txt with mode 600, put the same value in "
            "SHARED_SECRET in curation/apps_script/publish_report.gs, and set "
            "google.report_secret_file in config/project.yml.")
    if not path.is_file():
        raise DeliveryError(f"No report secret at {path}. See curation/GOOGLE_ACCESS.md.")
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise DeliveryError(f"The report secret at {path} is empty.")
    return secret.encode("utf-8")


def describe() -> str:
    """One line about whether delivery is configured, for a job to print before it starts."""
    try:
        secret = secret_path()
    except DeliveryError as exc:
        return f"Report delivery: MISCONFIGURED — {exc}"
    url = endpoint_url()
    if not url:
        return "Report delivery: no endpoint configured (google.report_endpoint)"
    if secret is None:
        return f"Report delivery: endpoint set, but no secret configured"
    if not secret.is_file():
        return f"Report delivery: endpoint set, secret configured at {secret} but absent"
    return f"Report delivery: configured ({url.split('/exec')[0][-24:]}…/exec)"


# --------------------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------------------

def build_notice_payload(submission_id: str, *, to: str, submitter_name: str,
                         names: Sequence[str], corrections: Dict[str, str],
                         reference: str = "",
                         issued_at: Optional[int] = None) -> Dict[str, Any]:
    """The payload for a name-confirmation email to the submitter.

    Sent through the same endpoint and the same secret as a report, distinguished by
    ``action``. One deployment, one secret, one thing to keep working.

    ``corrections`` maps a name the submitter proposed to the name they were actually
    given. It is the reason this email exists: a submitter who proposed TUMIG06 and was
    granted TUMIG25 must be told, in as many words, because the name they put in their
    paper and in GenBank has to be the granted one.
    """
    if not to or "@" not in to:
        raise DeliveryError(
            f"{submission_id}: no usable submitter address ({to!r}). Refusing to send.")
    if not names:
        raise DeliveryError(
            f"{submission_id}: no agreed names, so there is nothing to confirm. This is "
            f"probably a records-only submission; it should not reach this program.")
    return {
        "action": "confirm_names",
        "submission_id": submission_id,
        "to": to,
        "submitter_name": submitter_name or "",
        "names": list(names),
        "corrections": dict(corrections or {}),
        "reference": reference or "",
        "issued_at": int(time.time()) if issued_at is None else int(issued_at),
    }


def deliver_name_confirmation(
        submission_id: str, *, to: str, submitter_name: str, names: Sequence[str],
        corrections: Dict[str, str], reference: str = "",
        transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None) -> Delivered:
    """Tell a submitter which lineage names are theirs. Returns what the endpoint did."""
    payload = build_notice_payload(
        submission_id, to=to, submitter_name=submitter_name, names=names,
        corrections=corrections, reference=reference)
    reply = _send(payload, transport)
    return Delivered(
        submission_id=submission_id,
        file_id="",
        url="",
        action=str(reply.get("action", "emailed")),
        notified=int(reply.get("notified", 0) or 0),
    )


def build_decline_payload(submission_id: str, *, to: str, submitter_name: str,
                          reference: str = "",
                          issued_at: Optional[int] = None) -> Dict[str, Any]:
    """The payload for telling a submitter their submission was not accepted.

    Carries no reason, deliberately. A curator's written reasoning quotes the submission
    and is often a judgment about the data; it belongs in a reply from a person who can
    answer the follow-up question, not in an automatic message that can only make an
    assertion and stop. What this message does is make sure nobody is left waiting
    indefinitely, and point them at a human.
    """
    if not to or "@" not in to:
        raise DeliveryError(
            f"{submission_id}: no usable submitter address ({to!r}). Refusing to send.")
    return {
        "action": "decline_notice",
        "submission_id": submission_id,
        "to": to,
        "submitter_name": submitter_name or "",
        "reference": reference or "",
        "issued_at": int(time.time()) if issued_at is None else int(issued_at),
    }


def deliver_decline_notice(
        submission_id: str, *, to: str, submitter_name: str, reference: str = "",
        transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None) -> Delivered:
    """Tell a submitter their submission was not accepted, and to talk to a person."""
    payload = build_decline_payload(submission_id, to=to, submitter_name=submitter_name,
                                    reference=reference)
    reply = _send(payload, transport)
    return Delivered(submission_id=submission_id, file_id="", url="",
                     action=str(reply.get("action", "emailed")),
                     notified=int(reply.get("notified", 0) or 0))


def _send(payload: Dict[str, Any],
          transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None
          ) -> Dict[str, Any]:
    """Sign a payload and post it. Shared by both kinds of message."""
    url = endpoint_url()
    if not url:
        raise DeliveryError(
            "No report endpoint configured. Deploy curation/apps_script/publish_report.gs "
            "as a web app and set google.report_endpoint in config/project.yml to its "
            "/exec URL.")
    secret = load_secret()

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = sign(body, secret)
    signed_url = f"{url}{'&' if '?' in url else '?'}sig={signature}"

    reply = (transport or _post)(signed_url, body)
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise DeliveryError(
            f"The endpoint refused the request: "
            f"{(reply or {}).get('error', 'no reason given')}. "
            f"If this says 'bad signature', the secret here and the one in the Apps Script "
            f"project have drifted apart; if it says 'stale', this machine's clock is off "
            f"by more than {MAX_AGE_SECONDS // 60} minutes.")
    return reply


def build_payload(submission_id: str, pdf: bytes, *, notify: bool = True,
                  issued_at: Optional[int] = None) -> Dict[str, Any]:
    """The exact object that gets signed and posted.

    Separated from sending so a test can assert on it, and so a signature can be recomputed
    from a captured payload when debugging a rejection.

    The filename is derived from the submission id and nothing else. That is what makes
    re-publishing idempotent: the endpoint looks the name up in the folder and *updates*
    the file it finds, keeping the file id, so a link already emailed to a curator still
    resolves after a report is corrected and resent.
    """
    if not submission_id or "/" in submission_id or "\\" in submission_id:
        raise DeliveryError(f"Refusing an unusable submission id: {submission_id!r}")
    if not pdf:
        raise DeliveryError("Refusing to send an empty PDF.")
    if len(pdf) > MAX_PDF_BYTES:
        raise DeliveryError(
            f"The report is {len(pdf) / 1e6:.1f} MB, over the {MAX_PDF_BYTES / 1e6:.0f} MB "
            f"limit. A curator report is normally a few hundred KB — check what went into "
            f"it before raising the limit.")
    if not pdf.startswith(b"%PDF"):
        raise DeliveryError(
            "That file does not begin with %PDF. Refusing to publish it: a truncated or "
            "half-rendered report reaching a curator is worse than no report.")
    return {
        "action": "publish_report",
        "submission_id": submission_id,
        "filename": f"{submission_id}_report.pdf",
        "sha256": hashlib.sha256(pdf).hexdigest(),
        "issued_at": int(time.time()) if issued_at is None else int(issued_at),
        "notify": bool(notify),
        "pdf_b64": base64.b64encode(pdf).decode("ascii"),
    }


def sign(body: bytes, secret: bytes) -> str:
    """HMAC-SHA256 of the exact body bytes, hex.

    The signature covers the body and nothing else, and travels in the query string,
    because Apps Script does not expose request headers to ``doPost``. Signing the
    serialized body rather than a field-by-field canonical string means the two sides
    cannot disagree about canonicalization — the script hashes precisely the string it
    received.
    """
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def deliver(submission_id: str, pdf: bytes, *, notify: bool = True,
            transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None,
            timeout: int = 120) -> Delivered:
    """Publish one report. Returns what the endpoint says it did.

    ``transport`` exists so the whole path can be tested without a network or a deployed
    script: it is handed the URL and the body, and returns the decoded JSON reply.
    """
    payload = build_payload(submission_id, pdf, notify=notify)
    reply = _send(payload, transport)

    # Prove the endpoint received what we sent. Apps Script silently truncating a large
    # body would otherwise produce a valid-looking PDF of the wrong length in Drive.
    if reply.get("sha256") and reply["sha256"] != payload["sha256"]:
        raise DeliveryError(
            "The endpoint stored a file whose checksum does not match what was sent. "
            "Treat the file in Drive as corrupt and do not send its link to anyone.")

    return Delivered(
        submission_id=submission_id,
        file_id=str(reply.get("fileId", "")),
        url=str(reply.get("url", "")),
        action=str(reply.get("action", "unknown")),
        notified=int(reply.get("notified", 0) or 0),
    )


def _post(url: str, body: bytes) -> Dict[str, Any]:
    """The real transport. Kept tiny so the testable part above is the interesting part."""
    try:
        import requests  # imported here so the module loads without it for --check
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise DeliveryError(f"The 'requests' library is not installed: {exc}") from exc

    # Apps Script answers a POST with a 302 to a googleusercontent.com URL that carries the
    # actual body; requests follows it by default, which is what we want.
    response = requests.post(url, data=body,
                             headers={"Content-Type": "application/json"},
                             timeout=120)
    if response.status_code != 200:
        raise DeliveryError(
            f"The endpoint returned HTTP {response.status_code}. If this is 401 or 403, "
            f"the web app is probably not deployed with access set to 'Anyone'; "
            f"re-deploy it and use the new /exec URL.")
    try:
        return response.json()
    except ValueError:
        # A deployment that has never been authorized returns an HTML sign-in page, which
        # is the single most common way this fails and is unrecognizable from a stack trace.
        raise DeliveryError(
            "The endpoint returned HTML rather than JSON, which usually means the web app "
            "has not been authorized or is not deployed to 'Anyone'. Open the /exec URL in "
            "a browser: if you are asked to sign in, that is the problem.")
