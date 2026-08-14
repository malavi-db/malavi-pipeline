"""Which submissions are being held back, and which study each of them is for.

An embargo is a promise to one submitter: hold my *records* until my study is out. Review
carries on around it — the submission is screened, curators decide, the lineage names are
reserved and confirmed — and only publication waits.

**Why this module exists at all.** Everything else in the pipeline identifies a submission's
study through the record store's ``_source`` column, which says which submission brought a
row. That is unavailable for exactly the submissions this module is about:
``release_gate.admissibility`` refuses an embargoed submission, so ``ingest_submissions``
never writes its rows, so no provenance exists to read. Until 2026-08-13 that was a
deadlock — ``publish_reference`` was the only thing that lifted an embargo and it looked for
its submissions in the store, the one place an embargoed submission's rows are guaranteed
not to be.

So the question "which study is this submission for" is answered from the **submitted
workbook**, which exists from the moment the submission arrives and does not depend on
anything having been ingested, approved or published.

Two callers: ``lift_embargo.py``, which does it on purpose, and ``publish_reference.py``,
which does it as a consequence of the study appearing in print.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import store_ingest
from .submission_id import directory_for, is_opaque


def resolve_directory(inbox: Path, submission_id: str) -> Optional[Path]:
    """The submission's directory on disk, or ``None`` if nothing maps to the id.

    The ledger is keyed by the minted opaque id while the directory is named after the
    submitter, so an opaque id goes through the reverse lookup. A directory name passed
    directly is honored too, which is what a maintainer working from the filesystem has.
    """
    name = directory_for(inbox, submission_id) if is_opaque(submission_id) else submission_id
    if not name:
        return None
    path = Path(inbox) / name
    return path if path.is_dir() else None


def studies_cited(inbox: Path, submission_id: str) -> Tuple[List[str], str]:
    """Which studies a submission cites. Returns ``(names, complaint)``.

    A submission whose workbook cannot be found or read is **reported, not raised**. It is
    still a real embargoed submission that a maintainer may want to act on by id, and
    dropping it from a listing because its spreadsheet is unusual would hide the very thing
    the listing exists to surface — a submitter's records being held with nobody noticing.
    """
    directory = resolve_directory(inbox, submission_id)
    if directory is None:
        return [], "no directory is mapped to this id in submission_ids.json"
    try:
        workbooks = store_ingest.template_workbooks(directory)
    except Exception as exc:                                        # noqa: BLE001
        return [], f"the directory could not be read ({exc})"
    if not workbooks:
        return [], "no filled ImportMalavi template in the submission directory"

    names: List[str] = []
    for path in workbooks:
        try:
            names.extend(store_ingest.reference_names_in_workbook(path))
        except Exception as exc:                                    # noqa: BLE001
            return [], f"{path.name} could not be read ({exc})"
    return sorted(set(names)), ""


def embargoed_entries(entries: Dict[str, Any]) -> List[Any]:
    """Every submission currently held back, in id order."""
    return [entries[submission_id] for submission_id in sorted(entries)
            if entries[submission_id].embargoed]


def submissions_for_reference(inbox: Path, entries: Dict[str, Any],
                              reference: str) -> Tuple[List[str], List[str]]:
    """Embargoed submissions citing ``reference``, plus lines about any that are unclear.

    Matched on the study name **exactly as typed, stripped** — the same comparison
    ``publish_reference`` makes against ``REFERENCE_NAME``, which is a join key and is not
    normalized anywhere else either. A near-miss spelling therefore finds nothing and says
    so, which is the right outcome: lifting an embargo on a study that merely looks similar
    would publish somebody's unpublished data.
    """
    wanted = (reference or "").strip()
    chosen: List[str] = []
    notes: List[str] = []
    for entry in embargoed_entries(entries):
        names, complaint = studies_cited(inbox, entry.submission_id)
        if complaint:
            notes.append(f"{entry.submission_id}: cannot tell which study this is — "
                         f"{complaint}. Act on it by id if it is this one.")
            continue
        if wanted and wanted in names:
            chosen.append(entry.submission_id)
    return chosen, notes


def describe(inbox: Path, entries: Dict[str, Any]) -> List[str]:
    """The listing: every held submission, the state it is in, and what it cites."""
    held = embargoed_entries(entries)
    if not held:
        return ["No submission is under embargo."]

    lines = [f"{len(held)} submission(s) under embargo:"]
    for entry in held:
        names, complaint = studies_cited(inbox, entry.submission_id)
        study = ", ".join(names) if names else f"study unknown — {complaint}"
        lines.append(f"  {entry.submission_id}  state={entry.state}  "
                     f"names={', '.join(sorted(entry.reserved_names)) or 'none'}")
        lines.append(f"      cites: {study}")
    return lines
