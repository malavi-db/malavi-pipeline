"""The curators' board: where every submission stands, behind a shared password.

What this is
------------
A curator recording a verdict had no way to see whether it counted, what the other
curators had recorded, or what a submission was waiting on; the only place that knew was
the review ledger, on a machine no curator has an account on
(``results/PROTOCOL_REVIEW_2026-10-02.md``, sections 1 and 7). This module turns the
ledger into one small file that the public site serves and a page there decrypts in the
curator's browser.

Why it is on the public site, and what that costs
-------------------------------------------------
The site is static (GitHub Pages) and its repository is public, so a page there cannot be
access-controlled: an unlisted page is a public page. The board is therefore published
**encrypted** -- AES-256-GCM, with the key derived from one shared password by
PBKDF2-HMAC-SHA256 -- and the page asks for the password and decrypts in the browser.
Decided by Vincenzo on 2026-10-02 over a Google Sheet shared per curator, because it needs
no second system; the costs were stated and accepted:

* one password for everybody, so access cannot be withdrawn from one person without
  changing the password for all;
* every published version stays in the public repository's history, readable by anyone
  who ever learns the password it was encrypted with. Changing the password protects
  later versions only.

What is allowed on it, because of that
--------------------------------------
Only what would be tolerable if the password leaked: the opaque submission id, dates, the
state, which curator recorded which verdict (the word, never the reasoning), what the
submission is waiting on, and links to the report in Drive and to the verdict form, both
of which keep their own Google protection. **Never** a submitter's name, institution or
address, never a verdict's reason text, never a correction's text, never a lineage name
beside a state (the public queue's own rule: a reserved name encodes the host of
unpublished work). :func:`board_content` builds exactly that and nothing else, and a test
holds it there.

The password is generated, not chosen. The ciphertext is public for good, so the only
thing between it and a reader is the password's entropy: sixteen characters from
``secrets`` (96 bits) cannot be searched, where a memorable phrase could.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import ledger as ledger_mod
from .config import load_config, repo_root

# Where the encrypted board is written, relative to the repository. One of the files
# publish/push_feeds.sh is allowed to publish.
BOARD_FILE = Path("docs") / "assets" / "data" / "board.json"

# The password file's documented home, beside the report secret. Never inside the tree.
DEFAULT_PASSWORD_FILE = "~/.config/malavi/board_password.txt"
PASSWORD_ENV = "MALAVI_BOARD_PASSWORD_FILE"

# Key derivation. 600,000 rounds of PBKDF2-HMAC-SHA256 is OWASP's 2023 figure for that
# function; it costs a browser well under a second once per visit. The page reads the
# count from the file, so raising it here needs no change there.
KDF_ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12                 # the size AES-GCM is specified for
KEY_BYTES = 32                # AES-256
ENVELOPE_VERSION = 1

# How many finished submissions the board lists under the live ones.
FINISHED_LIMIT = 20

# Verdict words as the form shows them, not as the ledger stores them.
VERDICT_WORDS = {"approve": "accepted", "hold": "flagged", "decline": "rejected"}


class BoardError(RuntimeError):
    """The board could not be built or read. Written for whoever has to fix it."""


# --------------------------------------------------------------------------------------
# The password
# --------------------------------------------------------------------------------------

def password_path() -> Path:
    """Where the board password lives. Refuses a path inside the repository."""
    configured = (os.environ.get(PASSWORD_ENV, "").strip()
                  or str((load_config().get("review") or {}).get("board_password_file")
                         or DEFAULT_PASSWORD_FILE))
    resolved = Path(configured).expanduser().resolve()
    try:
        resolved.relative_to(repo_root().resolve())
    except ValueError:
        return resolved
    raise BoardError(
        f"The board password is at {resolved}, inside the repository. Move it outside "
        f"the tree ({DEFAULT_PASSWORD_FILE} is the documented home, mode 600).")


def load_password() -> Optional[str]:
    """The password, or None when no password file exists (the board is not set up)."""
    path = password_path()
    if not path.is_file():
        return None
    password = path.read_text(encoding="utf-8").strip()
    if not password:
        raise BoardError(f"The board password file at {path} is empty.")
    return password


def create_password_file() -> Path:
    """Generate a password and store it, mode 600. Refuses to replace an existing one.

    Replacing a password is a decision (every curator has to be given the new one), so it
    is done by deleting the file deliberately and running this again, never by accident.
    """
    path = password_path()
    if path.exists():
        raise BoardError(
            f"A board password already exists at {path}. To change it, delete that file "
            f"and run this again; then give every curator the new password.")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Opened with the final mode rather than chmod-ed afterwards, so the password is never
    # on disk readable by anyone else, however briefly.
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_urlsafe(12) + "\n")      # 16 characters, 96 bits
    return path


# --------------------------------------------------------------------------------------
# What the board says
# --------------------------------------------------------------------------------------

def _curator_name(registry: Dict[str, Any], curator_id: str) -> str:
    person = registry.get(curator_id)
    return (getattr(person, "name", "") or "").strip() or curator_id


def _last_event(entry: Any, event: str) -> Optional[Dict[str, Any]]:
    found = [h for h in entry.history if h.get("event") == event and h.get("at")]
    return max(found, key=lambda h: str(h.get("at"))) if found else None


def _verdict_link(config: Dict[str, Any], submission_id: str, revision: int) -> str:
    """The verdict form with the id and revision already filled in, or '' if not set up."""
    review = config.get("review") or {}
    url = str(review.get("verdict_form_url") or "")
    fields = review.get("verdict_form_entries") or {}
    if not url or not fields.get("submission_id"):
        return url
    from urllib.parse import urlencode
    values = {f"entry.{fields['submission_id']}": submission_id}
    if fields.get("revision"):
        values[f"entry.{fields['revision']}"] = str(revision)
    return url + ("&" if "?" in url else "?usp=pp_url&") + urlencode(values)


def _waiting_on(entry: Any, registry: Dict[str, Any], hold_hours: int,
                report_at: str) -> Dict[str, Any]:
    """What a live submission is waiting on, as a code the page puts into words.

    A code and its facts rather than a sentence, so the wording lives in one place (the
    page) and the one thing that depends on the time of reading -- whether the publish
    hold has run out -- is decided when the page is opened, not when the file was built.
    """
    state = entry.state
    if state in ("received", "screening_failed"):
        return {"code": "screening", "since": entry.received_at}

    if state == "awaiting_submitter":
        return {"code": "submitter", "since": entry.awaiting_since or entry.received_at}

    if state == "approved" and entry.approved_at:
        ends = ledger_mod._parse(entry.approved_at) + timedelta(hours=hold_hours)
        return {"code": "hold", "since": entry.approved_at,
                "ends": ends.strftime("%Y-%m-%dT%H:%M:%SZ"), "hours": hold_hours}

    # A correction outranks the flag it came with: it names the next person to act.
    for correction in entry.corrections:
        if correction.applied:
            continue
        if not correction.approved:
            return {"code": "correction_approval", "correction": correction.id,
                    "since": correction.at}
        return {"code": "correction_recording", "correction": correction.id,
                "since": correction.approved_at or correction.at}

    holds = ledger_mod.blocking_holds(entry)
    if holds:
        first = min(holds, key=lambda verdict: str(verdict.at))
        return {"code": "flag", "flag": first.id,
                "by": _curator_name(registry, first.curator),
                "verdict": VERDICT_WORDS.get(first.verdict, first.verdict),
                "since": first.at}

    # Nothing stands on the current revision: it wants a curator. Counted from when the
    # report for this revision went out, because that is when a curator could first act.
    revision_at = next((r.at for r in entry.revisions if r.number == entry.revision), "")
    return {"code": "verdict", "since": report_at or revision_at or entry.received_at}


def board_content(entries: Dict[str, Any], registry: Dict[str, Any],
                  config: Optional[Dict[str, Any]] = None,
                  excluded_ids: Sequence[str] = ()) -> Dict[str, Any]:
    """The board as plain data, before encryption. See the module docstring for the rule
    on what may appear here.

    Carries no time of generation: the file is rewritten only when this content changes,
    so a build that changes nothing leaves the published file byte-for-byte alone.
    """
    config = config if config is not None else load_config()
    hold_hours = int(ledger_mod._review_config(config)["publish_hold_hours"])
    skip = set(excluded_ids)

    live: List[Dict[str, Any]] = []
    finished: List[Dict[str, Any]] = []
    for submission_id, entry in sorted(entries.items()):
        if submission_id in skip:
            continue
        report = _last_event(entry, "report_published")
        report_at = str(report.get("at")) if report else ""

        if entry.state not in ledger_mod.LIVE_STATES:
            ended = max((str(h.get("at")) for h in entry.history if h.get("at")),
                        default=entry.received_at)
            finished.append({"id": submission_id, "state": entry.state,
                             "received": entry.received_at, "ended": ended})
            continue

        verdicts = [
            {"id": verdict.id,
             "curator": _curator_name(registry, verdict.curator),
             "verdict": VERDICT_WORDS.get(verdict.verdict, verdict.verdict),
             "at": verdict.at,
             # A verdict that no longer counts is shown, struck, with why.
             "standing": not verdict.resolved,
             "ended": ("withdrawn" if verdict.retracted_at else
                       "cleared by a lead" if verdict.overridden_by else "")}
            for verdict in ledger_mod.verdicts_for_revision(entry)
        ]
        live.append({
            "id": submission_id,
            "received": entry.received_at,
            "revision": entry.revision,
            "state": entry.state,
            "names": len(entry.reserved_names),
            "embargoed": bool(entry.embargoed),
            "report": ({"sent": report_at, "url": str(report.get("url") or "")}
                       if report else None),
            "verdicts": verdicts,
            "waiting": _waiting_on(entry, registry, hold_hours, report_at),
            "verdict_link": _verdict_link(config, submission_id, entry.revision),
        })

    # Longest wait first: the top row is the one most in need of somebody.
    live.sort(key=lambda row: str(row["waiting"].get("since") or row["received"]))
    finished.sort(key=lambda row: row["ended"], reverse=True)
    return {"version": 1, "hold_hours": hold_hours, "submissions": live,
            "finished": finished[:FINISHED_LIMIT]}


def excluded_submission_ids(inbox: Path, submission_ids: Sequence[str],
                            config: Optional[Dict[str, Any]] = None) -> List[str]:
    """Which of these ids ``submissions.exclude`` hides from the public feeds.

    The same list the queue uses (test submissions made while building the form). It is
    written as directory names, so each id is mapped back to its directory through the id
    map; the board and the queue then agree on what counts as a submission.
    """
    from .submission_id import directory_for
    config = config if config is not None else load_config()
    hidden = set()
    for item in (config.get("submissions") or {}).get("exclude") or []:
        hidden.add(item["id"] if isinstance(item, dict) else item)
    if not hidden:
        return []
    return sorted(submission_id for submission_id in submission_ids
                  if directory_for(inbox, submission_id) in hidden)


# --------------------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------------------

def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations,
                               dklen=KEY_BYTES)


def encrypt(content: Dict[str, Any], password: str, *, salt: Optional[bytes] = None,
            iv: Optional[bytes] = None, iterations: int = KDF_ITERATIONS) -> Dict[str, Any]:
    """Encrypt the board into the envelope the page reads.

    ``salt`` and ``iv`` are random unless given, and they are given only by tests. An IV
    must never be reused with the same key and different content under AES-GCM, which is
    why a fresh salt (hence a fresh key) and a fresh IV are drawn for every write.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    iv = iv if iv is not None else secrets.token_bytes(IV_BYTES)
    plaintext = json.dumps(content, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    sealed = AESGCM(_derive_key(password, salt, iterations)).encrypt(iv, plaintext, None)
    encode = lambda raw: base64.b64encode(raw).decode("ascii")           # noqa: E731
    return {"v": ENVELOPE_VERSION, "kdf": "PBKDF2-HMAC-SHA256", "iterations": iterations,
            "cipher": "AES-256-GCM", "salt": encode(salt), "iv": encode(iv),
            # The ciphertext with its 16-byte authentication tag at the end, which is the
            # layout WebCrypto's AES-GCM expects.
            "ciphertext": encode(sealed)}


def decrypt(envelope: Dict[str, Any], password: str) -> Dict[str, Any]:
    """Open an envelope. Raises BoardError on a wrong password or a damaged file."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        if envelope.get("v") != ENVELOPE_VERSION:
            raise BoardError(f"Unknown board file version {envelope.get('v')!r}.")
        salt = base64.b64decode(envelope["salt"])
        iv = base64.b64decode(envelope["iv"])
        sealed = base64.b64decode(envelope["ciphertext"])
        key = _derive_key(password, salt, int(envelope["iterations"]))
        return json.loads(AESGCM(key).decrypt(iv, sealed, None).decode("utf-8"))
    except InvalidTag as exc:
        raise BoardError("The password does not open this board file.") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise BoardError(f"The board file is damaged ({exc}).") from exc


def write_board(path: Path, content: Dict[str, Any], password: str) -> bool:
    """Write the encrypted board, unless the published one already says the same thing.

    Returns True when the file was written. Encryption is randomized, so two encryptions
    of the same board differ byte for byte; comparing the decrypted content is what stops
    every build from producing a new file, a new commit and a new public push.
    """
    if path.is_file():
        try:
            current = decrypt(json.loads(path.read_text(encoding="utf-8")), password)
            if current == json.loads(json.dumps(content)):
                return False
        except (BoardError, ValueError):
            pass            # unreadable, or encrypted under an old password: rewrite it
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = encrypt(content, password)
    # When the board last CHANGED, outside the ciphertext so the page can show it. Not when
    # it was last checked: a build that finds nothing new leaves the file alone, by design.
    envelope["written"] = ledger_mod.now_utc()
    path.write_text(json.dumps(envelope, indent=1) + "\n", encoding="utf-8")
    return True
