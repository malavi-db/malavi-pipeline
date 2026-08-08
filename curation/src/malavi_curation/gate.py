"""Pre-ingest validation gate: automated checks on a candidate submission.

This is the automated half of the database integrity gate (see
``results/METHODS_draft.md`` → "Database integrity & pre-ingest validation gate").
**Nothing is ever ingested automatically**; the gate's job is to surface every
problem (and every "this looks new — confirm it") to the curator before a write.

Two kinds of checks:
  * pure-Python, always available — prevalence sanity, accession format, vector
    genus sanity;
  * snapshot-backed (``data/db_snapshot.json``, produced by
    ``curation/r/gate_reference.R`` from malaviR) — is this lineage name already
    taken? is this accession already curated (a re-report, not a new deposit)?

Findings have a severity: ``error`` (block until resolved), ``warn`` (curator must
look), ``info`` (FYI, e.g. "this is a genuinely new lineage"). The gate never
mutates database state; it only annotates the submission.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import load_config
from .hosts_geography import load_gazetteer

_DATA_DIR = Path(__file__).resolve().parent / "data"
_SNAPSHOT_PATH = _DATA_DIR / "db_snapshot.json"

# A well-formed nucleotide accession (matches the miner's nucleotide pattern).
_ACCESSION_RE = re.compile(r"^[A-Z]{1,2}[0-9]{5,6}$")


@dataclass
class GateFinding:
    """One automated-check result on a submission."""

    check: str                       # which check produced it
    severity: str                    # "error" | "warn" | "info"
    message: str                     # curator-facing description
    where: Optional[str] = None      # locus, e.g. "records[3]" / accession value


@lru_cache(maxsize=1)
def load_snapshot() -> Optional[Dict[str, Any]]:
    """Load the malaviR DB snapshot, or None if it has not been generated."""
    if not _SNAPSHOT_PATH.is_file():
        return None
    with open(_SNAPSHOT_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _norm_acc(a: str) -> str:
    """Upper-case and drop a version suffix for comparison."""
    return re.sub(r"\.[0-9]+$", "", a.strip().upper())


def _check_prevalence(records: List[Dict[str, Any]]) -> List[GateFinding]:
    """number_found <= number_tested; both non-negative; warn on a lone numerator."""
    out: List[GateFinding] = []
    for i, r in enumerate(records):
        tested, found = r.get("number_tested"), r.get("number_found")
        loc = f"records[{i}]"
        for name, val in (("number_tested", tested), ("number_found", found)):
            if val is not None and val < 0:
                out.append(GateFinding("prevalence_sanity", "error",
                                       f"{name} is negative ({val}).", loc))
        if tested is not None and found is not None and found > tested:
            out.append(GateFinding("prevalence_sanity", "error",
                                   f"number_found ({found}) exceeds number_tested ({tested}).", loc))
        elif found is not None and tested is None:
            out.append(GateFinding("prevalence_sanity", "warn",
                                   "number_found given without number_tested — prevalence "
                                   "cannot be computed; confirm the denominator.", loc))
    return out


def _check_accession_format(accessions: List[str]) -> List[GateFinding]:
    """Flag accessions that are not well-formed nucleotide accessions."""
    out: List[GateFinding] = []
    for a in accessions:
        if not _ACCESSION_RE.match(_norm_acc(a)):
            out.append(GateFinding("accession_format", "warn",
                                   f"'{a}' is not a standard nucleotide accession "
                                   "(letters+digits); confirm it.", a))
    return out


def _resolve_accessions_insdc(accessions: List[str],
                              timeout: float = 20.0) -> Optional[set]:
    """Return the subset of ``accessions`` that are publicly retrievable from INSDC.

    Returns ``None`` if the lookup could not be performed at all (no network, API
    error), which the caller must distinguish from "nothing resolved".

    Uses one NCBI esummary call per chunk rather than one per accession: esummary
    accepts accessions as ids, returns a ``caption`` for each one it finds, and
    names the rest in an ``error`` string. Anything absent from the returned
    captions is not public.

    GenBank, ENA and DDBJ mirror each other daily, so a record missing here is
    missing from INSDC as a whole; querying one member is sufficient.
    """
    if not accessions:
        return set()

    import urllib.error
    import urllib.parse
    import urllib.request

    try:
        email = load_config().get("project", {}).get("curator_email", "")
    except Exception:
        email = ""

    found: set = set()
    # NCBI asks for <=3 requests/second unencrypted and caps ids per request;
    # 200 per chunk keeps well inside both.
    chunk_size = 200
    for start in range(0, len(accessions), chunk_size):
        chunk = [_norm_acc(a) for a in accessions[start:start + chunk_size]]
        params = {
            "db": "nuccore",
            "id": ",".join(chunk),
            "retmode": "json",
            "tool": "malavi_rebuild_gate",
        }
        if email:
            params["email"] = email
        url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
               + urllib.parse.urlencode(params))
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
        result = payload.get("result", {})
        for uid in result.get("uids", []):
            caption = (result.get(uid) or {}).get("caption")
            if caption:
                found.add(_norm_acc(caption))
        if start + chunk_size < len(accessions):
            time.sleep(0.4)
    return found


def _check_accession_resolves(accessions: List[str]) -> List[GateFinding]:
    """Flag cited accessions that are not publicly retrievable from INSDC.

    Format validation cannot catch this: an accession can be perfectly
    well-formed, pass every prefix rule, and still not exist publicly, because
    submitters reserve accessions at submission time and choose when to release
    them. A paper can therefore cite accessions that nobody can download -- seen
    in the wild with a 2026 paper citing PX312166-PX312169, which returned no
    hits from GenBank, ENA or DDBJ months after publication.

    Severity is ``warn``, never ``error``. Submitting to MalAvi *before* the
    sequences go public is explicitly part of the intended workflow (stage one is
    "before you publish"), so an unreleased accession is often entirely correct
    at that point. The curator decides whether to chase it.
    """
    out: List[GateFinding] = []
    resolved = _resolve_accessions_insdc(accessions)

    if resolved is None:
        out.append(GateFinding(
            "accession_resolves", "warn",
            "Could not reach INSDC to verify the cited accessions; they have not "
            "been checked for public availability.",
        ))
        return out

    missing = [a for a in accessions if _norm_acc(a) not in resolved]
    for a in missing:
        out.append(GateFinding(
            "accession_resolves", "warn",
            f"accession {a} is not publicly retrievable from INSDC (checked "
            f"{date.today().isoformat()}). It may be reserved but not yet "
            "released; if the paper is already published, ask the authors to "
            "release it.", a))
    if accessions and not missing:
        out.append(GateFinding(
            "accession_resolves", "info",
            f"all {len(accessions)} cited accessions are publicly retrievable "
            f"from INSDC (checked {date.today().isoformat()}).",
        ))
    return out


def _check_unlinked_records(records: List[Dict[str, Any]]) -> List[GateFinding]:
    """Flag "records" that carry no lineage, because they are not records yet.

    Prose mining yields host species *mentions*, not host-lineage associations. A
    row with a host but no lineage cannot enter the database: it says a species
    appeared somewhere in the paper, which is also true of every species in a
    comparison table or phylogeny. Observed on the Gambia hooded-vulture paper,
    where 15 such rows were produced and none was one of the study's own records.

    Reported once, as a warning, rather than once per row: the rows themselves are
    a lead for the curator, not an error in the submission.
    """
    unlinked = [r for r in records if not (r.get("lineage_name") or "").strip()]
    if not unlinked:
        return []
    return [GateFinding(
        "records_unlinked", "warn",
        f"{len(unlinked)} of {len(records)} record(s) have no lineage_name. These "
        "are candidate host mentions harvested from the text, not curated "
        "host-lineage records, and must be confirmed against the paper's own "
        "tables before ingestion.")]


def _check_host_binomials(records: List[Dict[str, Any]],
                          known_binomials: set) -> List[GateFinding]:
    """Flag host binomials that are not known MalAvi hosts.

    Two very different things land here, and both need a curator:
      * genuinely new host species, which are legitimate and interesting;
      * extraction damage, e.g. "Aegypius monatypical" -- a real binomial
        (*Aegypius monachus*) welded to the next word by a line break.

    Nothing is dropped. A new host species must not be silently discarded just
    because MalAvi has not seen it, so this only ever raises a flag.
    """
    if not known_binomials:
        return []
    out: List[GateFinding] = []
    seen = set()
    for i, r in enumerate(records):
        sp = (r.get("host_species") or "").strip()
        if not sp or sp in seen:
            continue
        seen.add(sp)
        if sp not in known_binomials:
            out.append(GateFinding(
                "host_binomial", "warn",
                f"host species '{sp}' is not a known MalAvi host binomial. Confirm "
                "it is a real species and not text-extraction damage (a hyphenated "
                "or line-broken name).", f"records[{i}]"))
    return out


def _check_new_countries(records: List[Dict[str, Any]],
                         known_countries: set) -> List[GateFinding]:
    """Note records from a country MalAvi has never recorded.

    Informational, not a problem: a first national record is one of the more
    interesting things a submission can carry, and it deserves a curator's eye
    rather than passing unremarked. Silent until it happens.
    """
    if not known_countries:
        return []
    out: List[GateFinding] = []
    for country in sorted({(r.get("country") or "").strip() for r in records} - {""}):
        if country not in known_countries:
            out.append(GateFinding(
                "new_country", "info",
                f"'{country}' has no records in MalAvi: this would be a first for "
                "the country. Confirm the spelling matches the gazetteer.",
                country))
    return out


def _check_vector_genera(vectors: List[Dict[str, Any]],
                         vector_genera: set) -> List[GateFinding]:
    """A vector species' genus should be a known arthropod-vector genus."""
    out: List[GateFinding] = []
    for i, v in enumerate(vectors):
        sp = (v.get("vector_species") or "").strip()
        if not sp:
            continue
        genus = sp.split()[0]
        if vector_genera and genus not in vector_genera:
            out.append(GateFinding("vector_sanity", "warn",
                                   f"vector genus '{genus}' is not a known MalAvi vector "
                                   "genus — confirm it is an arthropod vector.", f"vectors[{i}]"))
    return out


def _check_against_snapshot(submission: Dict[str, Any],
                            snapshot: Dict[str, Any]) -> List[GateFinding]:
    """Lineage-name novelty and accession re-report checks against current MalAvi."""
    out: List[GateFinding] = []
    known_lineages = set(snapshot.get("lineages", []))
    acc_to_lineage = snapshot.get("accession_to_lineage", {})

    # Lineage names referenced by records/vectors: known vs new.
    rows = submission.get("records", []) + submission.get("vectors", [])
    for i, r in enumerate(rows):
        lin = (r.get("lineage_name") or "").strip()
        if lin and lin not in known_lineages:
            out.append(GateFinding("lineage_known", "info",
                                   f"lineage '{lin}' is not in MalAvi — if genuinely new it "
                                   "must arrive with a sequence + accession.", f"row[{i}]"))

    # Accessions already curated -> this is a re-report, not a new deposit.
    for a in submission.get("accessions", []):
        existing = acc_to_lineage.get(_norm_acc(a))
        if existing:
            out.append(GateFinding("accession_collision", "info",
                                   f"accession {a} is already in MalAvi under lineage "
                                   f"'{existing}' (re-report, not a new sequence).", a))
    return out


def run_gate(submission: Dict[str, Any],
             snapshot: Optional[Dict[str, Any]] = None,
             check_online: bool = False) -> List[GateFinding]:
    """Run every automated check and return the findings (does not mutate input).

    ``check_online`` adds the INSDC accession-availability check. It defaults to
    False so that every other check stays pure, offline and reproducible: the
    same submission always yields the same findings. The pipeline turns it on for
    real curation runs. Setting ``MALAVI_GATE_OFFLINE=1`` forces it off even when
    requested, for CI or air-gapped work.
    """
    findings: List[GateFinding] = []
    records = submission.get("records", [])
    vectors = submission.get("vectors", [])
    accessions = submission.get("accessions", [])

    findings += _check_prevalence(records)
    findings += _check_accession_format(accessions)

    if check_online and os.environ.get("MALAVI_GATE_OFFLINE") != "1":
        findings += _check_accession_resolves(accessions)

    findings += _check_unlinked_records(records)

    try:
        gaz = load_gazetteer()
    except FileNotFoundError:
        gaz = {}
    findings += _check_vector_genera(vectors, set(gaz.get("vector_genera", [])))
    findings += _check_host_binomials(records, set(gaz.get("binomials", [])))
    findings += _check_new_countries(records, set(gaz.get("countries", [])))

    snap = snapshot if snapshot is not None else load_snapshot()
    if snap is not None:
        findings += _check_against_snapshot(submission, snap)
    else:
        findings.append(GateFinding("snapshot", "warn",
                                   "DB snapshot unavailable (run curation/r/gate_reference.R) "
                                   "— lineage/accession collision checks skipped."))
    return findings


def apply_gate(submission: Dict[str, Any],
               snapshot: Optional[Dict[str, Any]] = None,
               check_online: bool = False) -> Dict[str, Any]:
    """Run the gate and attach results to ``submission['gate']``; return submission.

    Adds a ``passed`` flag (True iff no ``error`` findings). A False ``passed``
    means the submission must not be ingested until the errors are resolved.
    """
    findings = run_gate(submission, snapshot=snapshot, check_online=check_online)
    n_error = sum(1 for f in findings if f.severity == "error")
    n_warn = sum(1 for f in findings if f.severity == "warn")
    submission["gate"] = {
        "passed": n_error == 0,
        "n_error": n_error,
        "n_warn": n_warn,
        "findings": [asdict(f) for f in findings],
    }
    return submission
