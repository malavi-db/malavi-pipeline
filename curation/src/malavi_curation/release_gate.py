"""Refuse to build a release carrying records no curator approved.

**The invariant this module exists for.** MalAvi's whole review apparatus -- the verdict
form, the curator registry, the 24-hour publish hold, dissent outranking approval -- is
worth exactly nothing if a release can be built without consulting any of it. Until this
module existed, it could: ``build_release`` read the record store and wrote a ZIP, and
never opened the review ledger. ``ledger.releasable()`` had no caller outside its tests.

**How a row is checked.** Every row in the store carries ``_source``: ``seed`` for the
rows imported from the last externally-produced release, or the id of the submission that
brought it. That column is what makes the check possible at all -- without it a release is
an undifferentiated pile of rows and "did somebody approve this?" has no answer.

* ``seed`` rows pass. They predate this project's review process and there is no curator
  decision to find; pretending otherwise would be a fiction. They are MalAvi as Staffan
  released it.
* A row naming a submission passes only if the ledger holds that submission, it is
  ``approved`` (or already ``released``), nothing stands against it, and it is not
  embargoed.
* A row naming nothing, or naming something the ledger has never heard of, fails. A
  release is a publication; a row nobody can account for is one nobody agreed to publish.

**What this module deliberately does not decide.** It does not re-implement the publish
hold, approvability, or the state machine. Those live in :mod:`ledger` and are re-checked
at the write by :func:`ledger.transition`, which is the only thing that can move a
submission to ``released``. This module's job is to ask the ledger, and to ask *before* a
ZIP exists rather than after -- see :func:`plan_release_transitions`, which rehearses every
transition on throwaway copies so a release that could not be recorded is never built in
the first place.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import ledger
from .release_store import SEED

# Sources that need no submission behind them. Kept as a set rather than a bare comparison
# so that adding another exempt provenance later is one edit in one place, and so that the
# reason it is exempt has somewhere to be written down.
EXEMPT_SOURCES = frozenset({SEED})

# What a row with no provenance at all is called in a report. Blank is not the same fault
# as naming an unknown submission -- one is a row that lost its provenance somewhere in the
# pipeline, the other is a row claiming a submission that does not exist -- and a curator
# reading the refusal needs to be able to tell them apart.
NO_SOURCE = "(blank)"


@dataclass(frozen=True)
class Violation:
    """One reason a release may not be built, and how much of the store it covers."""

    source: str
    reason: str
    rows: int
    tables: Tuple[str, ...] = ()

    def describe(self) -> str:
        where = ", ".join(self.tables)
        return f"{self.source}: {self.reason} ({self.rows} row(s) in {where})"


@dataclass
class GateResult:
    """The verdict on a proposed release build."""

    violations: List[Violation] = field(default_factory=list)
    # Submissions whose rows are in the store and which the ledger says may be published.
    # These are the entries a successful build marks released.
    publishing: List[str] = field(default_factory=list)
    # Rows accounted for by an exempt source, reported so the operator can see that a
    # release is seed-only rather than assuming the check found nothing to look at.
    exempt_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations


def sources_in_store(store: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """Every distinct ``_source`` in the store, with its row count and tables.

    Rows are counted rather than merely listed because "3 rows from an unknown submission"
    and "18,000 rows from an unknown submission" are the same fault and very different
    situations, and the operator reading a refusal is entitled to know which they have.
    """
    found: Dict[str, Dict[str, Any]] = {}
    for table_name, rows in sorted(store.items()):
        for row in rows:
            source = str(row.get("_source") or "").strip() or NO_SOURCE
            record = found.setdefault(source, {"rows": 0, "tables": set()})
            record["rows"] += 1
            record["tables"].add(table_name)
    return found


# The four things the gate can conclude about one provenance value. Named constants
# rather than bare strings so that a caller reading the verdict cannot misspell one and
# silently fall through to "not refused".
EXEMPT = "exempt"            # no submission needed behind it -- the seed
PUBLISHING = "publishing"    # approved, and this release would publish it
RELEASED = "released"        # published by an earlier release; nothing left to mark
REFUSED = "refused"          # may not be in a release, for the reason returned


def admissibility(source: str,
                  entries: Optional[Dict[str, ledger.Entry]]) -> Tuple[str, str]:
    """May rows carrying this ``_source`` be in a release? Returns ``(verdict, reason)``.

    **This is the whole rule, in one place, on purpose.** :func:`check` asks it of every
    provenance value it finds in the store, and ``ingest_submissions`` asks it of every
    submission before writing a single row. Two copies of the rule would drift, and the
    direction they would drift in is the damaging one: an ingest that is more permissive
    than the gate writes rows into the store that then block every subsequent release
    until somebody works out which submission is at fault and removes them by hand.

    ``entries`` may be ``None`` when no ledger exists. That is not automatically a failure:
    a store that is still entirely seed rows has nothing for a ledger to say. It becomes a
    failure the moment a row claims a submission, because then the release is making a
    claim about a review that cannot be produced.
    """
    if source in EXEMPT_SOURCES:
        return EXEMPT, ""

    if source == NO_SOURCE:
        return REFUSED, ("rows carry no _source, so nothing can be shown to have approved "
                         "them. Stamp provenance before building")

    if entries is None:
        return REFUSED, ("rows claim a submission but there is no review ledger to check "
                         "them against")

    entry = entries.get(source)
    if entry is None:
        return REFUSED, "the review ledger has no such submission"

    if entry.embargoed:
        return REFUSED, ("approved but embargoed: the submitter's own work is not public "
                         "yet, and a release would scoop them with their own data")

    if entry.state == "released":
        # Already published by an earlier release. Its rows stay in the store and stay in
        # every later release; there is nothing left to mark.
        return RELEASED, ""

    if entry.state != "approved":
        return REFUSED, f"state is {entry.state!r}, not 'approved'"

    approvable, why_not = ledger.is_approvable(entry)
    if not approvable:
        return REFUSED, why_not

    return PUBLISHING, ""


def check(store: Dict[str, List[Dict[str, Any]]],
          entries: Optional[Dict[str, ledger.Entry]]) -> GateResult:
    """Whether every row in the store is one somebody agreed to publish.

    The rule itself lives in :func:`admissibility`; this counts the rows each verdict
    covers, which is what the operator reading a refusal actually needs.
    """
    result = GateResult()

    for source, record in sorted(sources_in_store(store).items()):
        rows = record["rows"]
        tables = tuple(sorted(record["tables"]))
        verdict, reason = admissibility(source, entries)

        if verdict == EXEMPT:
            result.exempt_rows += rows
        elif verdict == PUBLISHING:
            result.publishing.append(source)
        elif verdict == REFUSED:
            result.violations.append(Violation(
                source=source, reason=reason, rows=rows, tables=tables))
        # RELEASED: nothing to count and nothing to refuse.

    result.publishing.sort()
    return result


def plan_release_transitions(entries: Dict[str, ledger.Entry],
                             publishing: Sequence[str],
                             actor: str,
                             at: Optional[str] = None,
                             config: Optional[Dict[str, Any]] = None,
                             reason: str = "") -> List[str]:
    """Rehearse marking each submission released, and report what would refuse.

    **Why rehearse rather than just try.** :func:`ledger.transition` enforces rules
    :func:`check` deliberately does not copy -- most importantly that the 24-hour publish
    hold has elapsed. Discovering that *after* writing a ZIP would leave a release on disk
    that the ledger refuses to record, which is the worst of both: the data is published
    and the record says it is not.

    So every transition is attempted here on deep copies first. Nothing observable changes;
    the caller either learns the whole set is recordable, or gets the reasons and builds
    nothing. The rules are not duplicated -- the real function is asked, on a copy.

    **``reason`` must be whatever the real write will pass.** It was originally omitted
    here, on the reasoning that a rehearsal only needs the entry's state. That was wrong:
    ``transition`` validates ``reason`` against a closed vocabulary, so rehearsing with
    ``""`` -- which is always valid -- passed while the real call, which passed prose,
    refused. The rehearsal reported a release recordable and the write then failed after
    the ZIP was on disk, which is the exact failure this function exists to prevent. A
    rehearsal that does not use the real arguments is not a rehearsal.
    """
    refusals: List[str] = []
    for submission_id in publishing:
        entry = entries.get(submission_id)
        if entry is None:                        # pragma: no cover - check() precedes this
            refusals.append(f"{submission_id}: no longer in the ledger")
            continue
        try:
            ledger.transition(copy.deepcopy(entry), "released", actor,
                              at=at, reason=reason, config=config)
        except ledger.LedgerError as exc:
            refusals.append(str(exc))
    return refusals


def retract_command(source: str,
                    entries: Optional[Dict[str, ledger.Entry]],
                    release: Optional[str] = None) -> Optional[str]:
    """The ``ingest_submissions.py --retract`` invocation that clears this violation.

    ``None`` unless the rows belong to a submission the ledger knows and which has since
    left ``approved`` -- withdrawn, declined, held, back in review. That is the one
    situation with a mechanical answer: the rows were ingested on the strength of an
    approval that no longer stands, and taking them back out is exactly what --retract
    exists for (see ``ingest_submissions.retract_submissions``). Until this was printed,
    the refusal said what was wrong and RUNBOOK row 12b was the only place that said what
    to do about it.

    No command is offered for the other refusals on purpose. An embargoed submission's
    rows are waiting for a paper, and the answer is ``lift_embargo.py`` when it appears;
    a standing hold is a curator's dissent still to be resolved; an unknown or blank
    source is a provenance fault to be understood, not rows to be deleted. Suggesting a
    retraction there would steer the operator toward destroying rows a pending decision
    may still publish.

    ``--release`` is required by ingest_submissions even on the retract path, so the
    release being built is threaded through; without one a placeholder is shown.
    """
    entry = (entries or {}).get(source)
    if entry is None or entry.state in ("approved", "released"):
        return None
    return (f".venv/bin/python curation/ingest_submissions.py --release "
            f"{release or '<YYYY-MM-DD>'} --retract {source} --apply")


def describe(result: GateResult, refusals: Iterable[str] = (),
             entries: Optional[Dict[str, ledger.Entry]] = None,
             release: Optional[str] = None) -> List[str]:
    """The refusal as lines a person reads before deciding what to do about it.

    Given ``entries``, a violation whose remedy is a retraction is followed by the exact
    command -- see :func:`retract_command` for which ones those are, and why only those.
    """
    lines: List[str] = []
    for violation in result.violations:
        lines.append(f"  {violation.describe()}")
        command = retract_command(violation.source, entries, release)
        if command:
            lines.append(f"      its rows were ingested under an approval that no longer "
                         f"stands; take them out of the store with:")
            lines.append(f"      {command}")
    for refusal in refusals:
        lines.append(f"  {refusal}")
    return lines
