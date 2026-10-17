"""Bridge to the malaviR validation layer (curation/r/validate_record.R).

Sends a submission's candidate records to R, which reconciles host names
(``malaviR::match_taxonomy``) and runs the improbable host/locality check
(``host_geo_flag``), then folds the returned flags back into the submission as a
human-readable ``validation`` list that ``curator_report`` renders.

R is invoked as a subprocess so the Python pipeline stays pure-Python; if Rscript
or malaviR is unavailable the submission is returned unchanged (with a note) so the
pipeline never hard-fails on a machine without R.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import repo_root

_VALIDATE_R = "curation/r/validate_record.R"


def _records_payload(submission: Dict[str, Any], version: str) -> Dict[str, Any]:
    """Build the JSON input validate_record.R expects from a submission.

    Sequences are sent when the submission has them -- a filled template does, a PDF
    extraction generally does not, because it mines accessions rather than sequences.
    Only the cleaned form is sent: R screens against the reference alignment, and the
    as-pasted form exists so the *report* can point at a position in the text the
    submitter actually typed, which is a job for the Python side.
    """
    records = []
    for r in submission.get("records", []):
        records.append({
            "host_species": r.get("host_species"),
            "lineage_name": r.get("lineage_name"),
            "country": r.get("country"),
        })
    sequences = [
        {"lineage_name": s.get("lineage_name"), "sequence_clean": s.get("sequence_clean")}
        for s in submission.get("sequences", []) if s.get("sequence_clean")
    ]
    return {"version": version, "records": records, "sequences": sequences}


def _flags_to_strings(result: Dict[str, Any]) -> List[str]:
    """Render the R validation result as curator-facing flag lines."""
    out: List[str] = []
    for h in result.get("host_taxonomy", []):
        if h.get("flagged"):
            out.append(f"host name '{h['host_species']}': {h.get('reason') or 'unresolved'}")
    for f in result.get("record_flags", []):
        sev = (f.get("severity") or "info").upper()
        out.append(f"{sev}: {f.get('lineage_name')} × {f.get('host_species')} "
                   f"[{f.get('type')}] — {f.get('reason')}")
    return out


@dataclass(frozen=True)
class RValidation:
    """The outcome of one call out to R, with skip and failure kept apart.

    The distinction is the whole point. "R is not installed here" and "R ran and
    crashed" both used to arrive as a line of text in the same list, which meant a
    reader could not tell an unrun check from a broken one -- and an unrun check that
    looks like a quiet success is how a validation gate stops being a gate.

    ``outcome`` is one of:

    * ``"ok"``    -- R ran and returned a result (which may itself contain flags)
    * ``"skip"``  -- the check could not apply here; ``reason`` says why
    * ``"error"`` -- R was available but the call failed; ``reason`` says how
    """

    outcome: str
    reason: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


def run_validation(
    submission: Dict[str, Any],
    version: str = "latest",
    rscript: Optional[str] = None,
) -> RValidation:
    """Run the malaviR validators over a submission's records. Does not mutate.

    Every failure mode is reported rather than raised, because a curation run must
    still deliver the checks that did work.
    """
    rscript = rscript or shutil.which("Rscript")
    script = repo_root() / _VALIDATE_R
    if not rscript:
        return RValidation("skip", "Rscript is not on PATH")
    if not script.is_file():
        return RValidation("skip", f"{_VALIDATE_R} is not present")

    payload = _records_payload(submission, version)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        tmp = fh.name
    try:
        proc = subprocess.run(
            [rscript, str(script), tmp],
            capture_output=True, text=True, timeout=600,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return RValidation("error", f"could not run Rscript: {exc}")
    finally:
        Path(tmp).unlink(missing_ok=True)

    if proc.returncode != 0:
        return RValidation(
            "error", f"R exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        return RValidation("ok", result=json.loads(proc.stdout))
    except json.JSONDecodeError:
        return RValidation("error", "R returned output that is not JSON")


def validate_submission(
    submission: Dict[str, Any],
    version: str = "latest",
    rscript: Optional[str] = None,
) -> Dict[str, Any]:
    """Run malaviR validation on a submission; attach a ``validation`` list.

    Returns the same submission dict (mutated) for convenience. On any failure to
    reach R/malaviR, attaches a single explanatory note instead of raising.

    Kept as the pipeline's existing entry point. New code should prefer
    :func:`run_validation`, which reports a skip and a failure as different things.
    """
    outcome = run_validation(submission, version=version, rscript=rscript)

    if outcome.outcome == "skip":
        submission.setdefault("validation", []).append(
            "validation skipped — Rscript or validate_record.R unavailable")
        return submission
    if outcome.outcome == "error":
        submission.setdefault("validation", []).append(
            f"validation error: {outcome.reason}")
        return submission

    result = outcome.result or {}
    flags = _flags_to_strings(result)
    submission.setdefault("validation", []).extend(
        flags or ["malaviR validation: no host-name or host/locality flags raised"])
    submission["validation_detail"] = result
    return submission
