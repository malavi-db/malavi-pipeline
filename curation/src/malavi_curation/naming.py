"""Propose the lineage name a new sequence should take.

MalAvi names a lineage with a five- or six-letter acronym built from its host's scientific
name, followed by a two-digit number: *Turdus migratorius* gives ``TUMIG`` or ``TURMIG``,
and the number is the next one that acronym has not used.

**This is a port, not a new implementation.** The same suggestion is offered on the website
by ``docs/assets/js/sequence-check.mjs``, and the two must not disagree — a submitter told
``TUMIG31`` by the site and a curator told ``TUMIG32`` by a report is a worse outcome than
neither being told anything. The algorithm below follows that file step for step, and the
tests assert the two agree on real release data.

**It proposes; it never assigns.** A suggestion in a report is a starting point for the
curator, who may know the host was misidentified — which is exactly the sort of thing that
changes the acronym and so the whole name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence


@dataclass
class AcronymUsage:
    """What one candidate acronym is already doing in the release."""

    acronym: str
    taken: int                      # how many release lineages use it
    highest: Optional[str]          # the highest-numbered one, or None
    proposal: str                   # the next free name
    claims: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class Suggestion:
    """What to call a new lineage from this host, or why we cannot say."""

    ok: bool
    message: str = ""
    host: str = ""
    options: List[AcronymUsage] = field(default_factory=list)

    @property
    def best(self) -> Optional[AcronymUsage]:
        """The acronym to follow: the one this host's lineages already use."""
        return self.options[0] if self.options else None

    @property
    def proposal(self) -> Optional[str]:
        return self.best.proposal if self.best else None


def _pad(number: int) -> str:
    """Two digits, matching padNumber() in the browser checker."""
    return str(number).rjust(2, "0")


def acronym_usage(acronym: str, names: Iterable[str],
                  claims: Optional[Sequence[Dict[str, str]]] = None) -> AcronymUsage:
    """Which numbers this acronym has used, and what the next free one is.

    ``claims`` are names held by submissions that have been received but are not yet in a
    release. A claimed number is not free: the whole point of the reservation is that a
    second person proposing the same name is told it is taken rather than discovering the
    collision in print.
    """
    pattern = re.compile(rf"^{re.escape(acronym)}(\d+)$")

    numbers = [int(m.group(1)) for m in
               (pattern.match(str(n)) for n in names) if m]

    claimed: List[Dict[str, Any]] = []
    for claim in claims or []:
        match = pattern.match(str(claim.get("name", "")))
        if match:
            claimed.append({"name": claim["name"],
                            "claimed": claim.get("claimed", ""),
                            "number": int(match.group(1))})
    claimed.sort(key=lambda c: c["number"])

    highest_release = max(numbers) if numbers else 0
    highest_claimed = claimed[-1]["number"] if claimed else 0
    nxt = max(highest_release, highest_claimed) + 1

    return AcronymUsage(
        acronym=acronym,
        taken=len(numbers),
        highest=(acronym + _pad(highest_release)) if numbers else None,
        proposal=acronym + _pad(nxt),
        claims=[{"name": c["name"], "claimed": c["claimed"]} for c in claimed],
    )


def suggest_name(host_name: str, names: Iterable[str],
                 claims: Optional[Sequence[Dict[str, str]]] = None) -> Suggestion:
    """The lineage name a sequence from this host should take.

    ``names`` is every lineage name in the pinned release.
    """
    # Keep only things that look like words, which drops "sp.", "cf.", authorities and
    # punctuation — so "Turdus sp." is a genus with no epithet rather than a binomial.
    words = [w for w in re.split(r"[^A-Za-z]+", str(host_name or "")) if len(w) >= 3]

    if len(words) < 2:
        return Suggestion(
            ok=False,
            message=("Give the host's scientific name — genus and species, as in "
                     "Turdus migratorius. The acronym is built from both."))

    genus, epithet = words[0].upper(), words[1].upper()

    # Both widths MalAvi uses, longer first. A duplicate is dropped, which is what happens
    # when the genus is only two letters long.
    acronyms: List[str] = []
    for candidate in (genus[:3] + epithet[:3], genus[:2] + epithet[:3]):
        if candidate not in acronyms:
            acronyms.append(candidate)

    all_names = list(names)
    options = [acronym_usage(a, all_names, claims) for a in acronyms]
    # The acronym this host's lineages already use is the one to follow, so show it first.
    options.sort(key=lambda o: o.taken, reverse=True)

    return Suggestion(
        ok=True,
        host=words[0].capitalize() + " " + epithet.lower(),
        options=options,
    )
