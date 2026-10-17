# @title Short, stable handles a curator can quote for one record
# @purpose Give every record in a submission a short label (R1, R2, ...) that the curator
#          report prints and the verdict form asks for, and resolve that label back to the
#          exact record it names.
# @why A curator correcting one value has to be able to say WHICH row. Reading a workbook
#      row number off a report is error-prone, and a natural key (lineage + host + site)
#      is ambiguous where a submission repeats one.
# @input submission.json (as read by report_html and the correction path)
# @output an in-memory ordered mapping; nothing is written
# @program python
# @critical-var HANDLE_PREFIX
"""The handle a curator quotes to name one record.

**One function, two readers.** The report prints these and the correction path resolves
them, and both call :func:`handles`. If they were generated in two places they could
disagree, and a curator's correction would land on a different row from the one they were
looking at -- silently, because both rows are plausible.

**Ordering is the definition.** ``R1`` is the first record in ``submission["records"]``,
in the order the workbook reader produced them, which is the order the report prints. That
makes the mapping reproducible from ``submission.json`` alone, with nothing stored and
nothing to keep in sync.

**Handles belong to a revision, not to a submission.** Applying a correction bumps the
revision and the report is regenerated; if rows were added or removed the handles move.
That is why a correction records the revision it was made against -- see
``verdicts``/``ledger`` -- and why resolving a handle against a different revision must be
refused rather than guessed at.

**Why not ``V``.** The verdict form already uses ``V1`` for a verdict and ``C1`` for a
correction. Records use ``R`` and share one sequence across host records and vector
records, so a handle is unambiguous wherever it is quoted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

# The letter a record handle starts with. R for record; V and C are taken by the verdict
# form's own identifiers.
HANDLE_PREFIX = "R"

# The two record lists a submission carries, in the order the report prints them. A single
# handle sequence runs across both, so a curator never has to say which table they meant.
RECORD_KINDS = ("records", "vectors")


@dataclass(frozen=True)
class Handle:
    """One record, and the label a curator uses to name it."""

    handle: str            # "R14"
    kind: str              # "records" | "vectors"
    index: int             # position within that list
    workbook_row: Any      # the row number in the submitted workbook, when known
    summary: str           # a short human description, for confirming the right row

    @property
    def table(self) -> str:
        """The store table this record becomes on ingest."""
        return "host_records" if self.kind == "records" else "vector_records"


def _summarize(record: Dict[str, Any], kind: str) -> str:
    """A one-line description, so a curator can confirm the handle names what they meant."""
    lineage = (record.get("lineage_name") or "").strip()
    if kind == "vectors":
        subject = (record.get("vector_species") or "").strip()
    else:
        subject = (record.get("host_species") or "").strip()
    where = (record.get("site") or record.get("country") or "").strip()
    return " / ".join(part for part in (lineage, subject, where) if part)


def handles(submission: Dict[str, Any]) -> List[Handle]:
    """Every record in the submission, with its handle, in the order the report prints."""
    out: List[Handle] = []
    number = 0
    for kind in RECORD_KINDS:
        for index, record in enumerate(submission.get(kind) or []):
            number += 1
            out.append(Handle(
                handle=f"{HANDLE_PREFIX}{number}",
                kind=kind,
                index=index,
                workbook_row=(record.get("source") or {}).get("row"),
                summary=_summarize(record, kind)))
    return out


def by_handle(submission: Dict[str, Any]) -> Dict[str, Handle]:
    """The handles keyed by label, for resolving one."""
    return {entry.handle: entry for entry in handles(submission)}


def normalize(value: Any) -> str:
    """A handle as typed by a curator, in canonical form.

    A form is typed by a person: ``r14``, ``R 14`` and ``R14 `` are all the same handle,
    and refusing them would be pedantry that costs a correction. Anything that is not a
    prefix followed by digits is left alone, so that :func:`resolve` refuses it plainly
    rather than mangling it into something that happens to match.
    """
    text = str(value or "").strip().upper().replace(" ", "")
    return text


def resolve(submission: Dict[str, Any], value: Any) -> Optional[Handle]:
    """The record a handle names, or ``None`` when it names nothing.

    ``None`` rather than an exception, and never a guess: a handle that does not resolve
    is a curator naming a row that is not there -- most likely because the report they
    were reading is not the current revision -- and that has to be reported to a person,
    not approximated.
    """
    return by_handle(submission).get(normalize(value))


def describe(submission: Dict[str, Any], limit: int = 0) -> List[str]:
    """``R1  row 3  TUMIG31 / Turdus migratorius / Newark`` for each record."""
    lines = [f"{entry.handle:5s} row {entry.workbook_row or '?':>4}  {entry.summary}"
             for entry in handles(submission)]
    return lines[:limit] if limit else lines
