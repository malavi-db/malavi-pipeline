"""The curator registry: who may record a verdict, and what they may do.

A verdict arrives from a Google Form that asks Google to verify the responder's email
address. That verification is worth having — it is far stronger than a typed-in name — but
it establishes exactly one fact: the responder controls that mailbox. It says nothing about
whether they are a MalAvi curator, and the form link is unlisted rather than private, so
anyone it is forwarded to can submit through it.

This module is the other half. ``config/curators.yml`` lists who the curators are; a
response whose address is not in that list resolves to nothing and is never acted on.
Authentication comes from Google, authorization comes from here, and keeping the two
separate is what lets curators sign in as themselves instead of sharing a MalAvi login —
which would attribute every verdict in the project's history to a single shared account.

Two roles, and the difference between them is narrow on purpose:

* ``curator`` may approve, hold or decline, and may retract a hold they placed themselves.
* ``lead`` may additionally clear an unresolved hold placed by *someone else*.

If any curator could clear any other curator's hold, dissent would stop outranking
approval and the hold would be decorative. Lead is a list rather than a person so that
handing it over is an edit to a config file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .config import load_config, repo_root

# The roles the registry understands. Anything else in the file is a typo, and a typo in a
# role must fail loudly: silently downgrading an unrecognized role to "curator" would hand
# out a power nobody granted, and silently dropping the curator would throw away verdicts.
ROLES = ("curator", "lead")


def _strict_bool(value: object, curator_id: str) -> bool:
    """Read an ``active:`` flag, refusing anything that is not already a boolean.

    ``bool("false")`` is ``True``, so a quoting slip in the YAML — ``active: "false"`` —
    would silently leave a retired curator empowered to decide what enters MalAvi. This is
    an authorization control, so an ambiguous value is an error rather than a guess.
    """
    if isinstance(value, bool):
        return value
    raise ValueError(
        f"curator {curator_id!r}: active must be true or false, not {value!r} "
        f"(a quoted \"false\" is a non-empty string, which reads as true).")


@dataclass(frozen=True)
class Curator:
    """One person entitled to record verdicts."""

    id: str                  # stable short slug; what verdicts are attributed to
    name: str                # display name, for reports a human reads
    email: str               # primary verified address
    role: str                # "curator" or "lead"
    active: bool             # False retires them without erasing their past verdicts
    aliases: Tuple[str, ...]  # other verified addresses that are still this person

    @property
    def is_lead(self) -> bool:
        """Whether this curator may clear another curator's hold.

        Retired leads are deliberately excluded: ``active`` is what makes the registry a
        usable way to remove someone's authority without deleting the record of what they
        decided while they had it.
        """
        return self.role == "lead" and self.active

    def addresses(self) -> List[str]:
        """Every address that resolves to this curator, normalized."""
        return [normalize_email(self.email)] + [normalize_email(a) for a in self.aliases]


def normalize_email(address: str) -> str:
    """Reduce an address to the form the registry is matched on.

    Case and surrounding whitespace only. Deliberately *not* clever: Gmail's dot-insensitive
    and plus-addressed forms are not collapsed, because doing so would make
    ``a.b@gmail.com`` and ``ab@gmail.com`` the same curator on Gmail and different curators
    everywhere else, and a rule that holds for one provider is a rule that will be wrong for
    the next curator who joins. A curator with two addresses lists both under ``aliases``,
    which is explicit and visible in review.
    """
    return address.strip().lower()


def registry_path() -> Path:
    """Where the registry lives, per ``config/project.yml``."""
    review = load_config().get("review") or {}
    configured = review.get("curator_registry", "config/curators.yml")
    return repo_root() / configured


def _load_raw(path_text: str) -> Dict[str, Curator]:
    """Parse the registry into ``{curator id: Curator}``.

    Deliberately uncached. An earlier version cached on (path, mtime), which is wrong here:
    the repository lives on NFS, whose attribute caching can hand back a stale mtime for
    up to a minute, so a just-retired curator would keep being honored and a just-added one
    would keep being filed as unrecognized. The file is a few hundred bytes and this is an
    authorization control — re-reading it is cheaper than being wrong about who may decide
    what goes into MalAvi.
    """
    path = Path(path_text)
    if not path.is_file():
        raise FileNotFoundError(
            f"Curator registry not found at {path}. Without it no verdict can be "
            f"authorized, and treating an absent registry as 'everyone is a curator' "
            f"would be the worst possible default.")

    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = parsed.get("curators") or []

    curators: Dict[str, Curator] = {}
    seen_addresses: Dict[str, str] = {}   # normalized address -> curator id that claimed it

    for entry in entries:
        curator_id = str(entry.get("id", "")).strip()
        if not curator_id:
            raise ValueError(f"{path}: a curator entry has no id.")
        if curator_id in curators:
            raise ValueError(
                f"{path}: duplicate curator id {curator_id!r}. Ids are what verdicts are "
                f"attributed to, so two people sharing one would merge their decisions.")

        role = str(entry.get("role", "curator")).strip()
        if role not in ROLES:
            raise ValueError(
                f"{path}: curator {curator_id!r} has unknown role {role!r}; "
                f"expected one of {ROLES}.")

        curator = Curator(
            id=curator_id,
            name=str(entry.get("name", curator_id)),
            email=str(entry.get("email", "")),
            role=role,
            active=_strict_bool(entry.get("active", True), curator_id),
            aliases=tuple(str(a) for a in (entry.get("aliases") or [])),
        )
        if not curator.email.strip():
            raise ValueError(f"{path}: curator {curator_id!r} has no email address.")

        # An address claimed by two curators would make attribution ambiguous, and the
        # ambiguity would only surface later, on a verdict that mattered.
        for address in curator.addresses():
            if address in seen_addresses:
                raise ValueError(
                    f"{path}: address {address!r} is listed for both "
                    f"{seen_addresses[address]!r} and {curator_id!r}.")
            seen_addresses[address] = curator_id

        curators[curator_id] = curator

    return curators


def load_registry(path: Optional[Path] = None) -> Dict[str, Curator]:
    """All curators, keyed by id. Includes retired ones; check ``.active`` to filter.

    Returns a fresh dict each call, so a caller that mutates the result cannot corrupt what
    the next caller sees.
    """
    target = Path(path) if path is not None else registry_path()
    return dict(_load_raw(str(target)))


def resolve(address: str, path: Optional[Path] = None) -> Optional[Curator]:
    """The curator who owns a verified email address, or ``None`` if nobody does.

    ``None`` is the important return value: it is what an unrecognized responder produces,
    and every caller must treat it as "record this and flag it", never as "assume they meant
    well". A verdict that cannot be attributed to a named curator is not a verdict.
    """
    wanted = normalize_email(address)
    for curator in load_registry(path).values():
        if wanted in curator.addresses():
            return curator
    return None


def active_curators(path: Optional[Path] = None) -> List[Curator]:
    """Everyone currently serving, in registry order — who a hold notification goes to."""
    return [c for c in load_registry(path).values() if c.active]


def leads(path: Optional[Path] = None) -> List[Curator]:
    """Curators who may clear another curator's hold."""
    return [c for c in load_registry(path).values() if c.is_lead]
