"""Read-only access to MalAvi's private Google Drive, for the fetch jobs.

**Why this exists.** The submission responses sheet used to be shared with "anyone with the
link", which let the fetcher read it over plain HTTPS with no credentials at all — no token
to leak, rotate or expire, and reproducible on any machine. That was a real property and
losing it costs something. It was given up on 2026-08-06 for a plainer reason: the same link
lets anyone who ever sees it read every submitter's address and their unpublished sequences,
and that link lived in a config file, in git history, and in every clone of the repository.

So access is now granted to named identities, and a job that needs to read has to prove who
it is. This module is the smallest thing that does that.

**What it does not do.** It asks for read-only scope and nothing else. A fetch job has no
business writing to Drive, and a credential that cannot write is one that cannot destroy a
submission by accident.

**Where the key lives: not here, and not in the repository.** The path comes from
``$MALAVI_GOOGLE_KEY`` or from ``google.service_account_key`` in config/project.yml, and
both point outside the tree. The check in :func:`key_path` refuses a key inside the
repository outright rather than trusting .gitignore, because the cost of being wrong once is
a private key in a public-adjacent history forever.

**When there is no credential, nothing pretends.** :func:`access_token` returns None and the
caller is expected to stop with an explanatory error, never to carry on and report zero
submissions — which is what "the fetch ran fine and found nothing" looks like from the
outside, and is indistinguishable from a quiet week.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .config import load_config, repo_root

# Read-only, deliberately. Drive's readonly scope covers both downloading an uploaded file
# and exporting a Sheet as CSV, which is everything the fetch jobs do.
SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)

ENV_VAR = "MALAVI_GOOGLE_KEY"


class CredentialError(RuntimeError):
    """No usable credential. The message is written for whoever has to fix it."""


def key_path() -> Optional[Path]:
    """Where the service-account key is, or None if none is configured.

    Order: ``$MALAVI_GOOGLE_KEY`` first so a CI run or a one-off can override without
    editing tracked configuration, then ``google.service_account_key`` in
    config/project.yml.
    """
    from_env = os.environ.get(ENV_VAR, "").strip()
    if from_env:
        return _checked(Path(from_env).expanduser())

    configured = (load_config().get("google") or {}).get("service_account_key")
    if configured:
        return _checked(Path(str(configured)).expanduser())
    return None


def _checked(path: Path) -> Path:
    """Refuse a key stored inside the repository.

    Not a matter of taste. A service-account key is a bearer credential: anyone holding the
    file can read everything it can read, forever, with no password and no second factor.
    Relying on .gitignore to keep it out of history means relying on nobody ever running
    `git add -f`, nobody copying it to a differently-named file, and every future clone
    keeping the same ignore rules. Refusing the location entirely is cheaper than any of
    those assumptions.
    """
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(repo_root().resolve())
    except ValueError:
        return resolved          # outside the repo, which is what we want
    raise CredentialError(
        f"The Google key is at {resolved}, inside the repository. Move it somewhere "
        f"outside the tree (~/.config/malavi/ is the documented home) and point "
        f"${ENV_VAR} or google.service_account_key at it. A key in the tree is one "
        f"`git add -f` away from being permanent.")


def access_token() -> Optional[str]:
    """A short-lived read-only OAuth token, or None if no credential is configured.

    Returning None rather than raising for the "not configured" case lets a caller decide
    how loudly to fail — but every caller must fail, not continue. A fetch that carries on
    without credentials cannot read anything and reports an empty inbox, which looks exactly
    like a quiet week.
    """
    path = key_path()
    if path is None:
        return None
    if not path.is_file():
        raise CredentialError(
            f"No Google key at {path}. Create the service account (see "
            f"docs in curation/GOOGLE_ACCESS.md), download its JSON key, and put it there.")

    try:
        from google.oauth2 import service_account          # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
    except ImportError as exc:
        raise CredentialError(
            "The 'google' extra is not installed. Run:\n"
            "    pip install -e 'curation[google]'\n"
            f"(underlying import error: {exc})") from exc

    credentials = service_account.Credentials.from_service_account_file(
        str(path), scopes=list(SCOPES))
    credentials.refresh(Request())
    return credentials.token


def service_account_email() -> Optional[str]:
    """The identity the key belongs to — what the Drive folders must be shared with.

    Worth surfacing, because "shared with the wrong address" and "not shared at all" produce
    the same 404 and the address is not otherwise visible to whoever is doing the sharing.
    """
    path = key_path()
    if path is None or not path.is_file():
        return None
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("client_email")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def describe() -> str:
    """One line about the credential state, for a job to print before it starts."""
    try:
        path = key_path()
    except CredentialError as exc:
        return f"Google access: MISCONFIGURED — {exc}"
    if path is None:
        return (f"Google access: none configured (set ${ENV_VAR} or "
                f"google.service_account_key)")
    if not path.is_file():
        return f"Google access: key configured at {path}, but the file is not there"
    email = service_account_email()
    return f"Google access: service account {email or 'unknown'} (read-only)"
