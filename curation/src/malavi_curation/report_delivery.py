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
from typing import Any, Callable, Dict, List, Optional, Sequence

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

# --------------------------------------------------------------------------------------
# The mail mode (guard rails of 2026-10-16, results/GUARDRAILS_PLAN_2026-10-16.md, 1.2)
# --------------------------------------------------------------------------------------
#
# Every email the system sends leaves through the endpoint. This variable says what the
# programs on THIS machine may ask it to send:
#
#   live      post every action; the endpoint sends (subject to its own MAIL_MODE)
#   redirect  post every action with mail_mode "redirect" in the payload; the endpoint
#             then sends everything THIS request causes to its own MAIL_REDIRECT_TO
#             address, whatever its MAIL_MODE property says (a request can only make the
#             mode more restrictive). The address is never in the payload: a leaked
#             secret must not become an open mailer.
#   dry       post nothing that carries mail; raise MailHeld instead, before any network
#
# UNSET MEANS DRY. The fail-safe direction: a development session on BIOMIX, or a hand
# run that forgot the variable, sends nothing. The intake cycle requires the variable to
# be set explicitly for a live run (it refuses otherwise), and the Actions workflow sets
# it. A value that is none of the three is an error, not a quiet dry run, so a typo
# ("lvie") is seen rather than silently held.
MAIL_MODE_ENV = "MALAVI_MAIL_MODE"
MAIL_MODES = ("live", "redirect", "dry")

# The actions whose only job is to send mail. publish_report carries mail only when its
# payload says notify; describe and status never do.
MAIL_ACTIONS = ("confirm_names", "decline_notice", "verdict_notice", "attention_notice")


class DeliveryError(RuntimeError):
    """Delivery could not even be attempted, or the endpoint refused it.

    The message is written for whoever has to fix the configuration, not for a log.
    """


class MailHeld(DeliveryError):
    """The message was not sent because a guard held it: the mail mode here is dry, the
    endpoint's MAIL_MODE is dry or redirect, a cap or the quota was reached. Not a fault
    of the message. The caller records nothing as sent and tries again on a later run."""


class StaleDeployment(DeliveryError):
    """The deployed endpoint script is older than the repository's copy.

    Its own class because the answer is different: nothing is misconfigured and the check
    did run, a redeploy is owed.
    """


@dataclass(frozen=True)
class Delivered:
    """What the endpoint did, as reported by the endpoint itself."""

    submission_id: str
    file_id: str
    url: str
    action: str          # "created" or "updated"
    notified: int        # how many curators were emailed; 0 when notification was off
    assignee: str = ""   # who the endpoint assigned (or found assigned); '' without a sheet
    assigned: bool = False   # True when THIS publish made the assignment
    sheet_error: str = ""    # the endpoint's note when the status sheet could not be written

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
            "the SHARED_SECRET Script Property of the report publisher (Apps Script "
            "project settings; see curation/apps_script/settings.gs), and set "
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
                         selections: Optional[Dict[str, Any]] = None,
                         issued_at: Optional[int] = None) -> Dict[str, Any]:
    """The payload for a name-confirmation email to the submitter.

    Sent through the same endpoint and the same secret as a report, distinguished by
    ``action``. One deployment, one secret, one thing to keep working.

    ``corrections`` maps a name the submitter proposed to the name they were actually
    given. It is the reason this email exists: a submitter who proposed TUMIG06 and was
    granted TUMIG25 must be told, in as many words, because the name they put in their
    paper and in GenBank has to be the granted one.

    ``selections`` is what the submitter chose on the form, so the email can tell them
    what happens to their RECORDS rather than guess. Four keys, all optional:

    ``stage``             their publication-stage answer, quoted back verbatim
    ``sending``           their "what are you sending us?" answer, quoted back verbatim
    ``records_included``  did they say they were sending host and geography records?
    ``records_held``      are those records waiting on publication?

    The two booleans are decided HERE, in Python, by ``form_metadata.records_are_held``
    and ``records_were_included`` -- not in Apps Script. The email text branches on them,
    and a branch that decides whether somebody is told "your records are in the next
    release" or "please send your records" belongs where it can be tested.

    The two strings are quoted verbatim on purpose. "You selected X" is a quotation, and
    normalizing the wording would put words in the submitter's mouth -- which matters
    because the publication-stage question has been asked with two different vocabularies
    ("Published"/"Unpublished" and "Pre-"/"Post-publication") and both are in the data.
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
        "selections": dict(selections or {}),
        "issued_at": int(time.time()) if issued_at is None else int(issued_at),
    }


def deliver_name_confirmation(
        submission_id: str, *, to: str, submitter_name: str, names: Sequence[str],
        corrections: Dict[str, str], reference: str = "",
        selections: Optional[Dict[str, Any]] = None,
        transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None) -> Delivered:
    """Tell a submitter which lineage names are theirs. Returns what the endpoint did."""
    payload = build_notice_payload(
        submission_id, to=to, submitter_name=submitter_name, names=names,
        corrections=corrections, reference=reference, selections=selections)
    reply = _send(payload, transport)
    return Delivered(
        submission_id=submission_id,
        file_id="",
        url="",
        action=str(reply.get("action", "emailed")),
        notified=int(reply.get("notified", 0) or 0),
    )


def build_verdict_notice_payload(submission_id: str, *, subject: str, body: str,
                                 actor_email: str = "", mail_key_: str = "",
                                 audience: str = "curators",
                                 issued_at: Optional[int] = None) -> Dict[str, Any]:
    """The payload for telling the other curators what somebody just recorded.

    The text is composed here rather than in the Apps Script, because the things worth
    saying -- which verdict id, whose objection, what state it left the submission in,
    which link answers it -- are all in the ledger, and the script cannot see the ledger.

    ``actor_email`` is who performed the act; the endpoint drops them from the recipient
    list. It does **not** carry the recipients: the endpoint sends only to its own
    CURATORS property, so a leaked secret cannot be used to mail anyone else.
    """
    if not subject.strip() or not body.strip():
        raise DeliveryError(
            f"{submission_id}: a verdict notice needs both a subject and a body.")
    return {
        "action": "verdict_notice",
        "submission_id": submission_id,
        "subject": subject.strip(),
        "body": body.strip(),
        "actor_email": (actor_email or "").strip(),
        "issued_at": int(time.time()) if issued_at is None else int(issued_at),
        # Who hears it: "curators" (everyone on CURATORS) or "leads" (ATTENTION_TO). The
        # endpoint resolves both from its own properties; the payload never names anyone.
        "audience": audience if audience in ("curators", "leads") else "curators",
        # The identity of this notice for the endpoint's duplicate check (see mail_key).
        # Given by the caller from the RESPONSE it announces (submission, responder,
        # form timestamp), never from the ledger's verdict id.
        "mail_key": mail_key_ or mail_key("verdict_notice", submission_id, subject, body),
    }


def deliver_verdict_notice(
        submission_id: str, *, subject: str, body: str, actor_email: str = "",
        mail_key_: str = "", audience: str = "curators",
        transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None) -> Delivered:
    """Tell the other curators (or the leads) that a verdict was recorded."""
    payload = build_verdict_notice_payload(submission_id, subject=subject, body=body,
                                           actor_email=actor_email, mail_key_=mail_key_,
                                           audience=audience)
    reply = _send(payload, transport)
    return Delivered(submission_id=submission_id, file_id="", url="",
                     action=str(reply.get("action", "emailed")),
                     notified=int(reply.get("notified", 0) or 0))


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
        # One decline per submission and address, ever: a retry after a lost reply must
        # not write to the submitter twice.
        "mail_key": mail_key("decline_notice", submission_id, to.strip().lower()),
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


# The endpoint refuses a longer body. The cycle sends the end of each step's output, not
# all of it; the log on the machine that ran it holds the rest.
MAX_ATTENTION_BODY = 20000


def build_attention_payload(*, subject: str, body: str, mail_key_: str = "",
                            issued_at: Optional[int] = None) -> Dict[str, Any]:
    """The payload telling the lead curators that the intake cycle needs a person.

    Like the verdict notice, it carries no recipients: the endpoint sends only to its own
    ATTENTION_TO list, so a leaked secret cannot be used to mail anyone else. The body is
    program output and can name a submitter, which is why it goes to the leads and not to
    every curator.
    """
    if not subject.strip() or not body.strip():
        raise DeliveryError("An attention notice needs both a subject and a body.")
    text = body.strip()
    if len(text) > MAX_ATTENTION_BODY:
        text = (text[:MAX_ATTENTION_BODY].rstrip()
                + "\n\n[cut here; the log on the machine that ran the cycle has the rest]")
    return {
        "action": "attention_notice",
        "subject": subject.strip(),
        "body": text,
        "issued_at": int(time.time()) if issued_at is None else int(issued_at),
        # The cycle's own attention fingerprint when it has one, so the endpoint and the
        # cycle agree on what "the same notice" is.
        "mail_key": mail_key_ or mail_key("attention_notice", subject, text),
    }


def deliver_attention_notice(
        *, subject: str, body: str, mail_key_: str = "",
        transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None) -> Delivered:
    """Tell the lead curators that an unattended run needs a person."""
    reply = _send(build_attention_payload(subject=subject, body=body, mail_key_=mail_key_),
                  transport)
    notified = int(reply.get("notified", 0) or 0)
    if notified == 0 and str(reply.get("action", "")) != "already_sent":
        # The caller remembers a sent notice and does not repeat it for days, so a notice
        # that reached nobody must not count as sent. The current endpoint refuses an
        # empty ATTENTION_TO itself; this covers an older deployment that reports 0.
        raise DeliveryError("the endpoint emailed nobody (notified: 0): its ATTENTION_TO "
                            "Script Property is probably empty; run showSettings in the "
                            "report publisher project.")
    return Delivered(submission_id="", file_id="", url="",
                     action=str(reply.get("action", "emailed")), notified=notified)


# --------------------------------------------------------------------------------------
# Which code is deployed?
# --------------------------------------------------------------------------------------

# Every action a program in this repository sends. The endpoint script declares the same
# list (ACTIONS in publish_report.gs) and a test holds the two together, so an action added
# on one side and not the other fails here rather than in front of a submitter.
ACTIONS_SENT = ("publish_report", "confirm_names", "decline_notice", "verdict_notice",
                "attention_notice", "describe", "status")


@dataclass(frozen=True)
class Deployment:
    """What the live endpoint said about itself, beside what the repository expects."""

    code_version: str            # CODE_VERSION of the deployed script
    actions: tuple               # the actions the deployed script answers
    curators: int                # how many addresses its live CURATORS property holds
    expected_version: str        # CODE_VERSION in this repository's publish_report.gs
    attention_to: int = -1       # how many lead addresses ATTENTION_TO holds; -1 = not stated
                                 # (a deployment older than 2026-10-14 does not report it)
    mail_mode: str = ""          # the endpoint's MAIL_MODE (live/redirect/dry); '' = not
                                 # stated (a deployment older than the mail guard)
    held_today: int = -1         # messages the endpoint held today (UTC); -1 = not stated

    @property
    def missing_actions(self) -> tuple:
        """Actions this repository sends that the deployed script does not answer."""
        return tuple(action for action in ACTIONS_SENT if action not in self.actions)

    @property
    def current(self) -> bool:
        return self.code_version == self.expected_version and not self.missing_actions


def script_path() -> Path:
    """The repository's copy of the endpoint script."""
    return repo_root() / "curation" / "apps_script" / "publish_report.gs"


def repository_code_version() -> str:
    """CODE_VERSION as written in the repository's publish_report.gs.

    Read from the file rather than repeated here, so there is one place to change it.
    """
    import re
    found = re.search(r"^var CODE_VERSION = '([^']+)';", script_path().read_text(
        encoding="utf-8"), flags=re.MULTILINE)
    if not found:
        raise DeliveryError(f"CODE_VERSION is not declared in {script_path()}.")
    return found.group(1)


def describe_live(transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None,
                  issued_at: Optional[int] = None) -> Deployment:
    """Ask the deployed endpoint which code it is running. Sends no mail, writes nothing.

    The request is signed like every other, so it also proves the secret here matches the
    one in the Apps Script project.

    A deployment older than the ``describe`` action does not know it. Its router sends any
    action it does not recognize to the PDF branch, which refuses a request with no
    filename ("unusable filename") before writing or emailing anything. That refusal is
    therefore the signature of an old deployment, and it is reported as one rather than as
    a failure of this check.
    """
    payload = {"action": "describe",
               "issued_at": int(time.time()) if issued_at is None else int(issued_at)}
    try:
        reply = _send(payload, transport)
    except DeliveryError as exc:
        if "unusable filename" in str(exc):
            raise StaleDeployment(
                "The deployed script does not know the 'describe' action, so it is older "
                f"than the repository's copy ({repository_code_version()}). Redeploy "
                "curation/apps_script/publish_report.gs (RUNBOOK section 1c), then run "
                "this check again.") from exc
        raise
    return Deployment(code_version=str(reply.get("code_version", "")),
                      actions=tuple(str(a) for a in (reply.get("actions") or [])),
                      curators=int(reply.get("curators", 0) or 0),
                      expected_version=repository_code_version(),
                      attention_to=(int(reply["attention_to"])
                                    if reply.get("attention_to") is not None else -1),
                      mail_mode=str(reply.get("mail_mode") or ""),
                      held_today=(int(reply["held_today"])
                                  if reply.get("held_today") is not None else -1))


def describe_deployment(deployment: Deployment, registry_curators: Optional[int] = None
                        ) -> str:
    """The lines a ``--check --live`` prints, for a person deciding whether to redeploy."""
    lines = [f"Deployed script: version {deployment.code_version or 'not stated'} "
             f"(the repository's is {deployment.expected_version})",
             f"Actions it answers: {', '.join(deployment.actions) or 'none stated'}",
             f"Curators it emails: {deployment.curators}"
             + ("" if registry_curators is None
                else f" (the registry has {registry_curators} active)"),
             "Lead curators it alerts when the intake cycle needs a person: "
             + ("not stated by this deployment" if deployment.attention_to < 0
                else str(deployment.attention_to))]
    if deployment.curators == 0:
        lines.append("WARNING: CURATORS is empty, so no report would be announced. Set the "
                     "CURATORS Script Property (showSettings in the project).")
    if deployment.attention_to == 0:
        lines.append("WARNING: ATTENTION_TO is empty, so an unattended intake cycle could "
                     "not tell anyone it needs a person. Set the ATTENTION_TO Script "
                     "Property.")
    if deployment.mail_mode:
        lines.append(f"Mail mode of the endpoint (MAIL_MODE): {deployment.mail_mode}"
                     + (f"; {deployment.held_today} message(s) held today"
                        if deployment.held_today > 0 else ""))
        if deployment.mail_mode != "live":
            lines.append("WARNING: the endpoint is not in live mail mode, so nothing it is "
                         "asked to send reaches the curators or submitters until MAIL_MODE "
                         "is set to live (Script Properties).")
    else:
        lines.append("The deployment predates the mail guard (no MAIL_MODE reported); paste "
                     "the current bundle and set MAIL_MODE (RUNBOOK section 3h).")
    if deployment.missing_actions:
        lines.append("MISSING: the deployed script does not answer "
                     + ", ".join(deployment.missing_actions) + ".")
    if registry_curators is not None and deployment.curators != registry_curators:
        lines.append("The live CURATORS property and config/curators.yml disagree on how many "
                     "curators there are. That is expected only for someone who has agreed "
                     "to help and has not been told yet (RUNBOOK section 3ca).")
    lines.append("The deployment is current." if deployment.current
                 else "REDEPLOY NEEDED: set the Script Properties if not yet done, paste "
                      "publish_report.gs and settings.gs, run showSettings (READY), then "
                      "Deploy > Manage deployments > New version (RUNBOOK section 1c).")
    return "\n".join(lines)


def mail_mode() -> str:
    """This machine's mail mode: live, redirect or dry. Unset is dry (see MAIL_MODE_ENV)."""
    raw = os.environ.get(MAIL_MODE_ENV, "").strip().lower()
    if not raw:
        return "dry"
    if raw not in MAIL_MODES:
        raise DeliveryError(
            f"{MAIL_MODE_ENV}={raw!r} is not one of {', '.join(MAIL_MODES)}. Nothing sent.")
    return raw


def describe_mail_mode() -> str:
    """One line for a program's banner."""
    try:
        mode = mail_mode()
    except DeliveryError as exc:
        return f"Mail mode: INVALID ({exc})"
    if mode == "dry" and not os.environ.get(MAIL_MODE_ENV, "").strip():
        return (f"Mail mode: dry ({MAIL_MODE_ENV} is not set; nothing that carries mail "
                f"is sent)")
    return f"Mail mode: {mode}"


def carries_mail(payload: Dict[str, Any]) -> bool:
    """Whether posting this payload could make the endpoint send an email."""
    action = str(payload.get("action") or "publish_report")
    if action in MAIL_ACTIONS:
        return True
    return action == "publish_report" and bool(payload.get("notify"))


def held_messages(reply: Dict[str, Any]) -> List[Dict[str, str]]:
    """The endpoint's list of messages it held, each ``{to, reason}`` (empty if none,
    or for a deployment older than the mail guard, which reports no such list)."""
    held = reply.get("held") or []
    out = []
    for item in held:
        if isinstance(item, dict):
            out.append({"to": str(item.get("to", "")), "reason": str(item.get("reason", ""))})
    return out


def _check_held(payload: Dict[str, Any], reply: Dict[str, Any]) -> None:
    """Raise MailHeld when the endpoint held a message that this payload needed sent.

    A message held as a DUPLICATE is not an error: the endpoint already sent that exact
    message once, so the caller may record it as sent (the endpoint reports the action
    as ``already_sent`` in that case). Any other reason -- the endpoint's MAIL_MODE is dry
    or redirect, a cap, the quota, a refused address -- means nothing reached the person
    the payload was for, and the caller must not record otherwise.
    """
    if not carries_mail(payload):
        return
    held = held_messages(reply)
    real = [item for item in held if item["reason"] != "duplicate"]
    if real:
        reasons = sorted({item["reason"] for item in real})
        raise MailHeld(
            f"the endpoint held {len(real)} message(s) ({', '.join(reasons)}); nothing is "
            f"recorded as sent. Mode reported by the endpoint: "
            f"{reply.get('mail_mode', 'not stated')}.")


def _send(payload: Dict[str, Any],
          transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None
          ) -> Dict[str, Any]:
    """Sign a payload and post it. Shared by both kinds of message.

    The mail guard sits here, in the one function every action goes through: in dry mode
    a payload that carries mail is refused before any network; in redirect mode it is
    marked so the endpoint redirects it; and a reply that reports held messages is turned
    into MailHeld so no caller records a send that did not happen.
    """
    mode = mail_mode()
    if carries_mail(payload):
        if mode == "dry":
            raise MailHeld(
                f"{MAIL_MODE_ENV} is {'not set' if not os.environ.get(MAIL_MODE_ENV) else 'dry'}"
                f", so the {payload.get('action', 'publish_report')} message was not sent. "
                f"Set {MAIL_MODE_ENV}=live to send it, or redirect to receive it yourself.")
        if mode == "redirect":
            payload = dict(payload)
            payload["mail_mode"] = "redirect"
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
    _check_held(payload, reply)
    return reply


def build_payload(submission_id: str, pdf: bytes, *, notify: bool = True,
                  issued_at: Optional[int] = None,
                  submitter: Optional[Dict[str, Any]] = None,
                  assign: Optional[bool] = None) -> Dict[str, Any]:
    """The exact object that gets signed and posted.

    Separated from sending so a test can assert on it, and so a signature can be recomputed
    from a captured payload when debugging a rejection.

    The filename is derived from the submission id and nothing else. That is what makes
    re-publishing idempotent: the endpoint looks the name up in the folder and *updates*
    the file it finds, keeping the file id, so a link already emailed to a curator still
    resolves after a report is corrected and resent.

    ``submitter`` (2026-10-16), when given, adds ``submitter_name``, ``submitter_email``,
    ``received_at`` and ``revision`` so the endpoint can put them on the status sheet; the
    verdict trigger and the inbox triage reach the submitter through that row. ``assign``
    tells the endpoint whether a FIRST report may be assigned to a curator in rotation;
    omitted means the endpoint's default (yes). Neither key is present when not given, so
    a payload built the old way is byte-identical to before.
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
    digest = hashlib.sha256(pdf).hexdigest()
    payload: Dict[str, Any] = {
        "action": "publish_report",
        "submission_id": submission_id,
        "filename": f"{submission_id}_report.pdf",
        "sha256": digest,
        "issued_at": int(time.time()) if issued_at is None else int(issued_at),
        "notify": bool(notify),
        "pdf_b64": base64.b64encode(pdf).decode("ascii"),
        # The identity of the report notice, for the endpoint's duplicate check: the same
        # bytes posted twice (a retry after a lost reply) are one message; a corrected
        # report is a different PDF and therefore a new message. Never derived from
        # issued_at, which changes on every attempt.
        "mail_key": mail_key("report_notice", submission_id, digest),
    }
    if submitter:
        for key in ("submitter_name", "submitter_email", "received_at", "revision"):
            if submitter.get(key) not in (None, ""):
                payload[key] = submitter[key]
    if assign is not None:
        payload["assign"] = bool(assign)
    return payload


def mail_key(*parts: str) -> str:
    """A stable identity for one machine-originated message, from what makes two
    requests the same message. The endpoint appends the recipient and remembers the key
    for MAIL_DEDUPE_DAYS, so the same message is never sent to the same person twice
    (review of 2026-10-16, finding 1: a body hash cannot do this, because a re-applied
    verdict gets a new ledger id that appears in the text)."""
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:32]


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
            timeout: int = 120, submitter: Optional[Dict[str, Any]] = None,
            assign: Optional[bool] = None,
            announced_before: Optional[bool] = None) -> Delivered:
    """Publish one report. Returns what the endpoint says it did.

    ``transport`` exists so the whole path can be tested without a network or a deployed
    script: it is handed the URL and the body, and returns the decoded JSON reply.
    """
    payload = build_payload(submission_id, pdf, notify=notify, submitter=submitter,
                            assign=assign)
    if announced_before is not None:
        # Whether a notice for this submission ever reached the curators (the ledger
        # knows; Drive does not): the endpoint words the notice "updated" only then.
        payload["announced_before"] = bool(announced_before)
    reply = _send(payload, transport)

    # Prove the endpoint received what we sent. Apps Script silently truncating a large
    # body would otherwise produce a valid-looking PDF of the wrong length in Drive.
    if reply.get("sha256") and reply["sha256"] != payload["sha256"]:
        raise DeliveryError(
            "The endpoint stored a file whose checksum does not match what was sent. "
            "Treat the file in Drive as corrupt and do not send its link to anyone.")

    notified = int(reply.get("notified", 0) or 0)
    if notify and notified == 0 and str(reply.get("notice", "")) != "already_sent":
        # The caller records a publish as announced and never sends it again, so a report
        # that reached nobody must not come back as a success. The current endpoint
        # refuses before writing when its CURATORS property is empty; this catches an
        # older deployment that writes the file and reports "notified: 0" instead. (An
        # endpoint with the mail guard reports notice "already_sent" when it declined to
        # send the same notice twice; that is a success, and the file is in place.)
        raise DeliveryError(
            f"{submission_id}: the endpoint wrote the report but emailed nobody "
            f"(notified: 0). Its CURATORS Script Property is probably empty: run "
            f"showSettings in the report publisher project. Nothing was recorded here; "
            f"the report will be sent again next time.")

    return Delivered(
        submission_id=submission_id,
        file_id=str(reply.get("fileId", "")),
        url=str(reply.get("url", "")),
        action=str(reply.get("action", "unknown")),
        notified=notified,
        assignee=str(reply.get("assignee", "") or ""),
        assigned=bool(reply.get("assigned", False)),
        sheet_error=str(reply.get("sheet_error", "") or ""),
    )


# --------------------------------------------------------------------------------------
# The status sheet (2026-10-16): the ledger's view, pushed after each intake cycle
# --------------------------------------------------------------------------------------

# A status row carries only these keys. Enforced here so a caller cannot slip reason text
# or a correction's wording into the sheet: the sheet is shared with every curator, and
# the ledger's free text stays in the ledger.
STATUS_ROW_KEYS = ("id", "received", "submitter_name", "submitter_email", "revision",
                   "state", "waiting_on", "waiting_since", "hold_ends", "embargoed",
                   "names", "report_url", "report_at")
STATUS_CURATOR_KEYS = ("name", "email", "role", "active")
MAX_STATUS_ROWS = 500


def build_status_payload(rows: Sequence[Dict[str, Any]],
                         curators_list: Sequence[Dict[str, Any]] = (),
                         issued_at: Optional[int] = None) -> Dict[str, Any]:
    """The ``status`` action's body: one row per submission, plus the curator registry.

    Keys outside STATUS_ROW_KEYS / STATUS_CURATOR_KEYS are refused, not dropped: a caller
    that tries to send more than the sheet may hold should fail loudly here rather than
    have the field silently vanish and reappear in a later version.
    """
    if len(rows) > MAX_STATUS_ROWS:
        raise DeliveryError(f"Refusing a status payload of {len(rows)} rows (limit "
                            f"{MAX_STATUS_ROWS}); something is wrong with the selection.")
    clean_rows = []
    for row in rows:
        extra = set(row) - set(STATUS_ROW_KEYS)
        if extra:
            raise DeliveryError(f"A status row carries keys the sheet may not hold: "
                                f"{sorted(extra)}. Only {STATUS_ROW_KEYS} are allowed.")
        if not row.get("id"):
            raise DeliveryError("A status row has no submission id.")
        clean_rows.append({key: row[key] for key in STATUS_ROW_KEYS if key in row})
    clean_curators = []
    for person in curators_list:
        extra = set(person) - set(STATUS_CURATOR_KEYS)
        if extra:
            raise DeliveryError(f"A curator row carries keys the sheet may not hold: "
                                f"{sorted(extra)}.")
        clean_curators.append({key: person.get(key, "") for key in STATUS_CURATOR_KEYS})
    return {
        "action": "status",
        "issued_at": int(time.time()) if issued_at is None else int(issued_at),
        "rows": clean_rows,
        "curators": clean_curators,
    }


def deliver_status(rows: Sequence[Dict[str, Any]],
                   curators_list: Sequence[Dict[str, Any]] = (),
                   transport: Optional[Callable[[str, bytes], Dict[str, Any]]] = None
                   ) -> Dict[str, Any]:
    """Push the ledger's view to the status sheet. Returns the endpoint's reply.

    The endpoint answers ``ok: false`` with a sentence when STATUS_SHEET_ID is not set in
    the Apps Script project; that is raised as a DeliveryError like any other refusal, and
    the caller decides whether a missing sheet is worth stopping for (the intake cycle
    does not: the sheet is optional).
    """
    return _send(build_status_payload(rows, curators_list), transport)


def _post(url: str, body: bytes) -> Dict[str, Any]:
    """The real transport. Kept tiny so the testable part above is the interesting part."""
    try:
        import requests  # imported here so the module loads without it for --check
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise DeliveryError(f"The 'requests' library is not installed: {exc}") from exc

    # Apps Script answers a POST with a 302 to a googleusercontent.com URL that carries the
    # actual body; requests follows it by default, which is what we want.
    try:
        response = requests.post(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 timeout=120)
    except requests.RequestException as exc:
        # A timeout or a connection failure is a delivery error like any other, with a
        # sentence instead of a traceback (review 2026-10-16, finding 13).
        raise DeliveryError(f"The endpoint could not be reached: {exc}") from exc
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
