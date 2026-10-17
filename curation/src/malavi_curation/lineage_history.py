# @title Record what changed about a lineage name, and make the store agree
# @purpose Define the four lineage-history events (sequence_updated, renamed,
#          merged_into, removed), build the history row each writes, and apply the
#          event to every store table that carries the name, so the history table and
#          the data it describes cannot disagree.
# @why The curators voted on 2026-09-24 to complete partial lineages under their old
#      names and to merge or drop trivial synonyms, on one condition: a PUBLIC table
#      of what changed, because MalAvi names are cited in hundreds of papers and a
#      rename without a trail breaks every downstream join. This module is that
#      condition made mechanical: no event without its row, no row without its event.
# @input data/records/*.csv
# @output data/records/lineage_history.csv (plus the tables the event changes)
# @program python
# @critical-var EVENTS
"""Lineage history: the events, the row, and the store change that goes with each.

The four events, and what each does to the store when applied:

``sequence_updated``
    The lineage keeps its name; its stored SEQUENCE (and SEQ_LENGTH, and GENBANK_ACC if
    given) are replaced. Rule 1 of the 2026-09-24 policy: a complete barcode that matches
    a partial lineage over every position both cover is that lineage, completed.

``renamed``
    Every row carrying the old LINEAGE_NAME, in every table, now carries the new one, and
    the old name is written to alt_names as a synonym so the papers that used it still
    resolve. The new name must be free.

``merged_into``
    The old lineage's rows are re-pointed to an existing lineage (host records, vector
    records, morphospecies links, alternative names), its own lineages row is dropped, and
    the old name becomes an alt_name of the target. Rule 2: a synonym that adds nothing
    is folded into the more complete lineage.

``removed``
    The lineage and every row that cites it are dropped. Rule 2's other half, for a
    lineage that should not be in MalAvi at all (TURGUL01 was the example named).

Every event writes one row to ``lineage_history`` with the old and new name and sequence,
so the row alone is enough to map an old analysis forward. ``LINEAGE_NAME`` on that row
is the name the event is filed under: the surviving name, or for a removal the name that
was removed.

Nothing here writes to disk; :mod:`record_lineage_event` (the CLI) reads the store, calls
:func:`plan` to see what would change, and :func:`apply` only under ``--apply``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import normalize
from .release_store import SEED, TABLES

EVENTS = ("sequence_updated", "renamed", "merged_into", "removed")

# Tables whose rows cite a lineage by name and follow it through a rename or a merge.
# lineages itself is handled explicitly by each event.
CITING_TABLES = ("host_records", "vector_records", "alt_names", "morpho_species",
                 "genomic_resources")


class HistoryError(ValueError):
    """The event cannot be applied as asked; the message says why."""


@dataclass(frozen=True)
class Event:
    event: str
    lineage: str                          # the name the event is about (the old name)
    new_name: str = ""                    # renamed / merged_into: the name after
    new_sequence: str = ""                # sequence_updated: the replacement sequence
    accession: str = ""                   # sequence_updated: the accession it came from
    date: str = ""                        # ISO date of the decision
    reason: str = ""
    source: str = ""                      # submission id, accession, person, "curator decision"
    release: str = ""                     # the release the change first appears in


@dataclass
class Plan:
    """What applying an event would do, in words and in counts."""
    event: Event
    history_row: Dict[str, str]
    changes: List[str] = field(default_factory=list)      # one line per table touched
    counts: Dict[str, int] = field(default_factory=dict)  # table -> rows changed/dropped


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _name(value: Any) -> str:
    return normalize.lineage_name(_text(value)) or ""


def _lineage_row(store: Dict[str, List[Dict[str, Any]]], name: str) -> Optional[Dict[str, Any]]:
    for row in store.get("lineages", []):
        if _name(row.get("LINEAGE_NAME")) == name:
            return row
    return None


def _real_length(sequence: str) -> int:
    """Bases that are not a gap: what SEQ_LENGTH records."""
    return sum(1 for ch in sequence if ch not in "-.~ ")


def history_row(ev: Event, old_sequence: str, new_sequence: str) -> Dict[str, str]:
    """The lineage_history row an event writes. Columns exactly as the table declares."""
    if ev.event not in EVENTS:
        raise HistoryError(f"unknown event {ev.event!r}; one of {', '.join(EVENTS)}")
    old = _name(ev.lineage)
    new = _name(ev.new_name)
    filed_under = old if ev.event in ("sequence_updated", "removed") else new
    return {
        "LINEAGE_NAME": filed_under,
        "EVENT": ev.event,
        "OLD_NAME": old,
        "NEW_NAME": "" if ev.event == "removed" else (new or old),
        "OLD_SEQUENCE": old_sequence,
        "NEW_SEQUENCE": "" if ev.event == "removed" else new_sequence,
        "DATE": _text(ev.date),
        "REASON": _text(ev.reason),
        "SOURCE": _text(ev.source),
        "RELEASE": _text(ev.release),
    }


def plan(store: Dict[str, List[Dict[str, Any]]], ev: Event) -> Plan:
    """Check the event against the store and say what applying it would change.

    Raises :class:`HistoryError` when the event cannot be applied: an unknown lineage, a
    target name that does not exist (merge) or already exists (rename), a sequence update
    with no sequence.
    """
    old = _name(ev.lineage)
    if not old:
        raise HistoryError("the event names no lineage")
    row = _lineage_row(store, old)
    if row is None:
        raise HistoryError(f"{old} is not a lineage in the store")
    old_sequence = _text(row.get("SEQUENCE")).upper()
    new = _name(ev.new_name)

    if ev.event == "sequence_updated":
        new_sequence = _text(ev.new_sequence).upper()
        if not new_sequence:
            raise HistoryError(f"sequence_updated for {old} needs the new sequence")
        if new_sequence == old_sequence:
            raise HistoryError(f"{old}: the new sequence is the one already stored")
        p = Plan(ev, history_row(ev, old_sequence, new_sequence))
        p.changes.append(
            f"lineages: {old} SEQUENCE {_real_length(old_sequence)} -> "
            f"{_real_length(new_sequence)} bases"
            + (f", GENBANK_ACC {_text(row.get('GENBANK_ACC')) or '(none)'} -> {ev.accession}"
               if ev.accession else ""))
        p.counts["lineages"] = 1
        return p

    if ev.event == "renamed":
        if not new:
            raise HistoryError(f"renamed for {old} needs the new name")
        if new == old:
            raise HistoryError(f"{old}: the new name is the old name")
        if _lineage_row(store, new) is not None:
            raise HistoryError(f"{new} is already a lineage; use merged_into to fold "
                               f"{old} into it")
        p = Plan(ev, history_row(ev, old_sequence, old_sequence))
        p.counts["lineages"] = 1
        p.changes.append(f"lineages: {old} -> {new}")
        for table in CITING_TABLES:
            n = sum(1 for r in store.get(table, []) if _name(r.get("LINEAGE_NAME")) == old)
            if n:
                p.counts[table] = n
                p.changes.append(f"{table}: {n} row(s) {old} -> {new}")
        p.changes.append(f"alt_names: + ({new}, {old}) so the old name still resolves")
        return p

    if ev.event == "merged_into":
        if not new:
            raise HistoryError(f"merged_into for {old} needs the target name")
        target = _lineage_row(store, new)
        if target is None:
            raise HistoryError(f"{new} is not a lineage in the store; use renamed if "
                               f"{old} is simply getting a new name")
        if new == old:
            raise HistoryError(f"{old}: cannot merge a lineage into itself")
        p = Plan(ev, history_row(ev, old_sequence, _text(target.get("SEQUENCE")).upper()))
        p.counts["lineages"] = 1
        p.changes.append(f"lineages: drop {old} (kept: {new})")
        for table in CITING_TABLES:
            n = sum(1 for r in store.get(table, []) if _name(r.get("LINEAGE_NAME")) == old)
            if n:
                p.counts[table] = n
                p.changes.append(f"{table}: {n} row(s) {old} -> {new}")
        p.changes.append(f"alt_names: + ({new}, {old}) so the old name still resolves")
        return p

    if ev.event == "removed":
        p = Plan(ev, history_row(ev, old_sequence, ""))
        p.counts["lineages"] = 1
        p.changes.append(f"lineages: drop {old}")
        for table in CITING_TABLES:
            n = sum(1 for r in store.get(table, []) if _name(r.get("LINEAGE_NAME")) == old)
            if n:
                p.counts[table] = n
                p.changes.append(f"{table}: drop {n} row(s)")
        return p

    raise HistoryError(f"unknown event {ev.event!r}")


def apply(store: Dict[str, List[Dict[str, Any]]], ev: Event,
          source_tag: str = "curator") -> Plan:
    """Apply the event to ``store`` in place and append its history row. Returns the plan.

    ``source_tag`` is the ``_source`` provenance written on the history row (a submission
    id when an ingest files the event, otherwise who decided).
    """
    p = plan(store, ev)
    old = _name(ev.lineage)
    new = _name(ev.new_name)

    if ev.event == "sequence_updated":
        row = _lineage_row(store, old)
        row["SEQUENCE"] = p.history_row["NEW_SEQUENCE"]
        row["SEQ_LENGTH"] = str(_real_length(p.history_row["NEW_SEQUENCE"]))
        if ev.accession:
            row["GENBANK_ACC"] = ev.accession

    elif ev.event in ("renamed", "merged_into"):
        if ev.event == "renamed":
            _lineage_row(store, old)["LINEAGE_NAME"] = new
        else:
            store["lineages"] = [r for r in store.get("lineages", [])
                                 if _name(r.get("LINEAGE_NAME")) != old]
        for table in CITING_TABLES:
            for r in store.get(table, []):
                if _name(r.get("LINEAGE_NAME")) == old:
                    r["LINEAGE_NAME"] = new
        # The old name as a synonym of the new: the same trail ALT_NAME has always kept.
        synonym = {c: "" for c in TABLES["alt_names"].columns}
        synonym.update({"LINEAGE_NAME": new, "ALT_NAME": old,
                        "REFERENCE_NAME": ev.source or "curator decision",
                        "_source": source_tag, "_added": ev.release})
        already = any(_name(r.get("LINEAGE_NAME")) == new and _name(r.get("ALT_NAME")) == old
                      for r in store.get("alt_names", []))
        if not already:
            store.setdefault("alt_names", []).append(synonym)

    elif ev.event == "removed":
        store["lineages"] = [r for r in store.get("lineages", [])
                             if _name(r.get("LINEAGE_NAME")) != old]
        for table in CITING_TABLES:
            store[table] = [r for r in store.get(table, [])
                            if _name(r.get("LINEAGE_NAME")) != old]

    history = dict(p.history_row)
    history.update({"_source": source_tag, "_added": ev.release})
    store.setdefault("lineage_history", []).append(history)
    return p


def current_name(history: Sequence[Dict[str, Any]], name: str) -> Tuple[str, List[str]]:
    """Follow renames and merges from ``name`` to the name in force now.

    Returns ``(current, trail)``; ``current`` is ``""`` when the trail ends in a removal.
    A cycle (which the events above cannot create, but a hand-edited table could) stops
    the walk rather than looping.
    """
    forward: Dict[str, Tuple[str, str]] = {}
    for row in history:
        if _text(row.get("EVENT")) in ("renamed", "merged_into", "removed"):
            forward[_name(row.get("OLD_NAME"))] = (_text(row.get("EVENT")), _name(row.get("NEW_NAME")))
    trail: List[str] = []
    here = _name(name)
    seen = set()
    while here in forward and here not in seen:
        seen.add(here)
        event, nxt = forward[here]
        trail.append(f"{here} {event} {nxt}".strip())
        if event == "removed":
            return "", trail
        here = nxt
    return here, trail
