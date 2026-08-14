# @title Correct records MalAvi already holds, and keep the record of why
# @purpose Select rows in the record store by record id, by site, or by an exact column
#          value, set a field on all of them as one decision, and append that decision to
#          a committed correction log.
# @why Data faults in the published database (a longitude with the wrong sign, a
#      misspelled vector method) have to be fixable without hand-editing 18,000-row CSVs,
#      and every fix has to leave a record of who made it and why.
# @input data/records/*.csv
# @output data/records/*.csv (rewritten)
# @output data/corrections.csv (appended)
# @program python
# @critical-var CORRECTION_LOG
# @critical-var SELECTORS
"""Corrections to records MalAvi already holds.

**This is not the submission-correction path.** ``apply_corrections.py`` handles a curator
fixing a *submitter's* rows before they are ingested, and it records the fix as a revision
over a workbook that is never edited. This module is the other case: a fault in data MalAvi
has already published, where the store itself is the record and correcting it is the whole
point.

**A published edition is never edited.** The store is the *next* edition; a release ZIP
that has shipped is immutable. A correction made here appears in the next release and in
its edition report, which is what makes the change visible to somebody working from the
edition before it.

**One decision, however many rows.** The 54 host records at Mata Seca State Park share one
wrong longitude, and they are one mistake, not 54. Selecting by site and setting the field
once records a single decision with a single reason, and the log says how many rows it
touched. Row-by-row correction of a shared value invites 54 chances to type it differently.

**Nothing is guessed.** A selector either matches rows or it does not; a value is either
given or it is not. There is no inference here, no fuzzy matching and no "did you mean" --
the operator names exactly what to change and exactly what to change it to, and the
program's whole job is to apply that consistently and write down what it did.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .release_store import TABLES, record_id

# The committed log of every correction made to the store. Tracked in git, beside the
# store it describes: `data/records/` says what MalAvi holds and this says why any of it
# changed. The store's own `_source`/`_added` columns are deliberately left alone -- they
# record where a row came from and when it first appeared, which a later correction does
# not alter.
CORRECTION_LOG = "corrections.csv"

CORRECTION_LOG_COLUMNS: Tuple[str, ...] = (
    "CORRECTION_ID", "APPLIED_ON", "ACTOR", "TABLE", "SELECTOR", "COLUMN",
    "OLD_VALUE", "NEW_VALUE", "ROWS_CHANGED", "REASON",
)

# The ways a correction may name the rows it applies to. Each is exact: no pattern
# matching, no case folding, no partial matches. An operator who cannot name the rows
# precisely should not be changing them in bulk.
SELECTORS = ("record", "site", "where")


@dataclass
class Correction:
    """One decision: change one column on the rows a selector names."""

    table: str
    column: str
    new_value: str
    reason: str
    actor: str = "maintainer"
    selector_kind: str = "record"       # one of SELECTORS
    selector_value: str = ""
    selector_column: str = ""           # only for `where`

    def describe_selector(self) -> str:
        if self.selector_kind == "where":
            return f"where {self.selector_column}={self.selector_value!r}"
        if self.selector_kind == "site":
            return f"site {self.selector_value!r}"
        return f"record {self.selector_value}"


@dataclass
class Change:
    """One row that a correction would change, and how."""

    table: str
    record: str
    column: str
    was: str
    now: str
    context: Dict[str, str] = field(default_factory=dict)


def _text(value: Any) -> str:
    return (value or "").strip() if isinstance(value, str) else ("" if value is None
                                                                 else str(value).strip())


def matcher(correction: Correction) -> Callable[[Dict[str, Any]], bool]:
    """The predicate a row must satisfy to be corrected.

    Exact comparison throughout. ``site`` matches ``SITE_NAME`` exactly rather than by
    substring, because "Mata Seca" would also select "Mata Seca II" and an operator
    correcting a coordinate would not notice until the release shipped.

    **Both sides are stripped, not just the stored one.** They used to be asymmetric --
    :func:`_text` on the row, the raw string from argv as the target -- which made this
    program unable to select any row whose fault *was* surrounding whitespace. The store
    holds 22 accessions with non-breaking spaces from an Excel paste; ``_text`` reduced
    ``'\\xa0 KF717063'`` to ``'KF717063'``, which matched neither the damaged value an
    operator copied out of the CSV nor anything else they could type. Python's ``strip()``
    removes U+00A0, so the needle and the haystack were being measured differently.
    Selecting by the clean value is also what an operator would try first.
    """
    if correction.selector_kind == "record":
        target = _text(correction.selector_value)
        return lambda row: record_id(row) == target
    if correction.selector_kind == "site":
        target = _text(correction.selector_value)
        return lambda row: _text(row.get("SITE_NAME")) == target
    if correction.selector_kind == "where":
        column = correction.selector_column
        target = _text(correction.selector_value)
        return lambda row: _text(row.get(column)) == target
    raise ValueError(f"unknown selector {correction.selector_kind!r}; "
                     f"expected one of {SELECTORS}")


def plan(store: Dict[str, List[Dict[str, Any]]], correction: Correction) -> List[Change]:
    """Every row this correction would change, with the value it holds now.

    Rows that already carry the new value are **not** included: a correction is a change,
    and reporting a no-op as a change would inflate the count in the log and in the
    edition report. Re-running a correction that has already been applied therefore
    reports nothing to do, which is the honest answer.

    **"Already carries it" is judged on the raw stored string**, not on a stripped copy.
    What gets written to the CSV is the raw value, so a row holding ``'\\xa0 KF717063'``
    genuinely does not carry ``'KF717063'`` and correcting it genuinely changes the
    published file. Comparing stripped values called that a no-op and skipped it, which --
    together with the asymmetric selector in :func:`matcher` -- made whitespace damage the
    one fault class this program could neither find nor fix.
    """
    spec = TABLES.get(correction.table)
    if spec is None:
        raise ValueError(f"unknown table {correction.table!r}; "
                         f"expected one of {', '.join(sorted(TABLES))}")
    if correction.column not in spec.columns:
        raise ValueError(
            f"{correction.table} has no column {correction.column!r}. Its columns are: "
            f"{', '.join(spec.columns)}")
    if correction.selector_kind == "site" and "SITE_NAME" not in spec.columns:
        raise ValueError(f"{correction.table} has no SITE_NAME column, so it cannot be "
                         f"selected by site")
    if correction.selector_kind == "where" and correction.selector_column not in spec.columns:
        raise ValueError(f"{correction.table} has no column "
                         f"{correction.selector_column!r} to select on")

    keep = matcher(correction)
    changes: List[Change] = []
    for row in store.get(correction.table, []):
        if not keep(row):
            continue
        # Raw, because the raw string is what is written back to the CSV. See the
        # docstring: stripping here made whitespace damage look like a no-op.
        raw = row.get(correction.column)
        was = raw if isinstance(raw, str) else _text(raw)
        if was == correction.new_value:
            continue
        changes.append(Change(
            table=correction.table, record=record_id(row), column=correction.column,
            was=was, now=correction.new_value,
            # Enough of the row to recognize it in a listing without printing all twenty
            # columns. These are the fields an operator checks to confirm they selected
            # what they meant to.
            context={key: _text(row.get(key))
                     for key in ("LINEAGE_NAME", "SPECIES_NAME", "VECTOR_SPECIES",
                                 "SITE_NAME", "COUNTRY_NAME", "REFERENCE_NAME")
                     if _text(row.get(key))}))
    return changes


def apply(store: Dict[str, List[Dict[str, Any]]],
          correction: Correction) -> List[Change]:
    """Apply the correction to the store in memory, returning what changed."""
    changes = plan(store, correction)
    changed_ids = {change.record for change in changes}
    for row in store.get(correction.table, []):
        if record_id(row) in changed_ids:
            row[correction.column] = correction.new_value
    return changes


# ---------------------------------------------------------------------------
# The committed log
# ---------------------------------------------------------------------------

def log_path(root: Path) -> Path:
    return Path(root) / "data" / CORRECTION_LOG


def read_log(path: Path) -> List[Dict[str, str]]:
    path = Path(path)
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def next_correction_id(existing: Sequence[Dict[str, str]]) -> str:
    """The next id, derived from the highest in use rather than from the count.

    Counting rows mints a duplicate the moment one is ever removed -- the same defect
    that was fixed for submission correction ids in 6a66ec5.
    """
    highest = 0
    for row in existing:
        match = re.fullmatch(r"COR-(\d+)", _text(row.get("CORRECTION_ID")))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"COR-{highest + 1:06d}"


def append_log(path: Path, correction: Correction, changes: Sequence[Change],
               applied_on: Optional[str] = None) -> Dict[str, str]:
    """Record one applied correction. Returns the row written.

    ``OLD_VALUE`` holds the single value replaced when every corrected row held the same
    one, and ``(various)`` when they did not -- a bulk correction over rows that disagreed
    is a thing the log should say plainly rather than pick a representative for.
    """
    path = Path(path)
    existing = read_log(path)
    was_values = {change.was for change in changes}
    row = {
        "CORRECTION_ID": next_correction_id(existing),
        "APPLIED_ON": applied_on or date.today().isoformat(),
        "ACTOR": correction.actor,
        "TABLE": correction.table,
        "SELECTOR": correction.describe_selector(),
        "COLUMN": correction.column,
        "OLD_VALUE": was_values.pop() if len(was_values) == 1 else "(various)",
        "NEW_VALUE": correction.new_value,
        "ROWS_CHANGED": str(len(changes)),
        "REASON": correction.reason,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.is_file()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CORRECTION_LOG_COLUMNS))
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    return row
