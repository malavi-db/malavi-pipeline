"""One named, versioned check interface over a submission, whichever path it arrived by.

The checking in this project was real but scattered: the pre-ingest gate, the row flags,
the malaviR validators and the template screen each had their own result shape, their own
severity vocabulary, and their own way of saying nothing happened. A curator could not
tell, from any of it, what had actually been checked.

This module does not re-implement any of that logic. It wraps the four producers in one
registry so that every check reports the same four things:

* **what it asserts**, in a sentence a biologist can read;
* **whether it ran** -- and if not, why not;
* **what it found**, with the sheet and row a curator should look at;
* **how much it looked at**, so "0 findings" can be told from "nothing to check".

Two design rules carry most of the weight.

**Outcome and severity are different axes.** Severity is a property of the check ("a
lineage name collision is blocking"); outcome is what happened this time (it ran, it was
skipped, it broke). Conflating them is how a check that never ran comes to look like a
check that passed.

**An implementation failure is never a skip and never a pass.** A crashed R process, a
malformed R response or an exception inside a check means validation is *incomplete*, and
the run says so at the top rather than presenting a partial result as a clean bill of
health.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__


class Outcome(str, Enum):
    """What happened when a check ran."""

    PASS = "pass"          # it ran and found nothing
    FINDING = "finding"    # it ran and found something
    SKIP = "skip"          # it could not apply here -- always with a reason
    ERROR = "error"        # the check itself malfunctioned


class Severity(str, Enum):
    """How much a finding from this check matters."""

    BLOCKING = "blocking"  # must be resolved before anything is ingested
    WARNING = "warning"    # a curator must look
    INFO = "info"          # a decision to make, not evidence of a problem


# Severity words used by the wrapped producers, mapped onto this module's vocabulary.
_SEVERITY_FROM_LEGACY = {
    "error": Severity.BLOCKING,
    "warn": Severity.WARNING,
    "warning": Severity.WARNING,
    "info": Severity.INFO,
}


@dataclass(frozen=True)
class Check:
    """What a check is, independent of any particular run.

    ``id`` is permanent once it has been emitted. Titles and explanatory prose may
    change freely; a change in what a check *means* needs a new id, because ids are
    what a curator's past decisions and any future adjudication record will point at.
    """

    id: str
    title: str
    asserts: str           # one sentence, curator-facing
    scope: str             # "submission" | "record" | "vector" | "sequence" | "name"
    severity: Severity


@dataclass(frozen=True)
class Finding:
    """One thing a check found, and where to look at it."""

    subject: str                              # display label, e.g. "TUMIG19 x Parus major"
    message: str
    severity: Severity = Severity.WARNING
    source: Optional[Dict[str, Any]] = None   # {sheet, row, file} where available
    evidence: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "subject": self.subject,
            "message": self.message,
            "severity": self.severity.value,
        }
        if self.source:
            out["source"] = self.source
        if self.evidence:
            out["evidence"] = self.evidence
        return out


@dataclass(frozen=True)
class CheckResult:
    """The result of running one check once.

    ``evaluated`` and ``passed`` are what let a report distinguish "checked 47 rows and
    they were fine" from "there were no rows to check", which read identically if only
    findings are reported.
    """

    check_id: str
    outcome: Outcome
    evaluated: int = 0
    passed: int = 0
    findings: Tuple[Finding, ...] = ()
    skip_reason: Optional[str] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "check_id": self.check_id,
            "outcome": self.outcome.value,
            "evaluated": self.evaluated,
            "passed": self.passed,
            "findings": [f.as_dict() for f in self.findings],
        }
        if self.skip_reason:
            out["skip_reason"] = self.skip_reason
        if self.error:
            out["error"] = self.error
        return out


# ---------------------------------------------------------------------------------------
# The registry
#
# Every id here is permanent. The gate, row-flag and screen ids deliberately match the
# names those modules already use, so that a finding can be traced back to the code that
# raised it without a translation table.
# ---------------------------------------------------------------------------------------

def _check(id_, title, asserts, scope, severity):
    return Check(id=id_, title=title, asserts=asserts, scope=scope, severity=severity)


CHECKS: Dict[str, Check] = {c.id: c for c in [
    # -- from gate.py -------------------------------------------------------------------
    _check("prevalence_sanity", "Found is not more than tested",
           "The number infected is never greater than the number screened.",
           "record", Severity.BLOCKING),
    _check("accession_format", "Accessions are well formed",
           "Every accession looks like an NCBI nucleotide accession.",
           "submission", Severity.WARNING),
    _check("accession_collision", "Accessions are not already curated",
           "No submitted accession is already in MalAvi under another lineage.",
           "submission", Severity.INFO),
    _check("lineage_known", "Lineage names are checked against the release",
           "Each lineage named is either already in MalAvi or arrives as a new one.",
           "name", Severity.INFO),
    _check("vector_sanity", "Vector genera are recognized",
           "Each vector's genus is one MalAvi already records vectors from.",
           "vector", Severity.WARNING),

    # -- from row_flags.py --------------------------------------------------------------
    # One check per flag code. The descriptions come from row_flags.FLAGS at import time
    # so the two can never disagree about what a flag means; see _row_flag_checks.

    # -- from check_template.py's screen ------------------------------------------------
    # A warning, not a blocker: the report offers a free name and an approval adopts it,
    # so the submission goes through with the correction rather than stopping. What still
    # blocks is `sequence_is_known_lineage` -- a sequence MalAvi already holds is not a new
    # lineage under any name, and no suggestion can fix that.
    _check("name_malformed", "Proposed names follow the naming convention",
           "Each proposed name is its host's acronym followed by a number.",
           "name", Severity.WARNING),
    _check("name_already_in_malavi", "Proposed names are free",
           "No proposed new lineage name is already a MalAvi lineage.",
           "name", Severity.WARNING),
    # Separate from the check above because the answer a curator gives is different. A
    # released name is settled and the submitter renames. A name claimed by a submission
    # still in the queue is a question of who arrived first, and the earlier submitter may
    # not have been granted it yet either.
    _check("name_claimed_by_another_submission", "Proposed names are not already claimed",
           "No proposed new lineage name has been claimed by an earlier submission that "
           "is still under review.",
           "name", Severity.WARNING),
    _check("sequence_is_known_lineage", "New sequences are actually new",
           "No sequence offered as new is identical to a lineage MalAvi already names.",
           "sequence", Severity.BLOCKING),
    _check("sequence_needs_reframing", "Sequences are in the MalAvi reading frame",
           "Each sequence starts on a codon boundary of the 479 bp cytochrome b window.",
           "sequence", Severity.WARNING),
    _check("sequence_stop_codon", "Sequences translate without stops",
           "No sequence carries a stop codon in the reference frame (genetic code 4).",
           "sequence", Severity.WARNING),
    _check("sequence_unplaceable", "Sequences register against the alignment",
           "Each sequence can be placed in the MalAvi reference alignment at all.",
           "sequence", Severity.BLOCKING),
    _check("accession_malformed", "Declared accessions are well formed",
           "Each accession on the NewLineages sheet has a valid accession shape.",
           "name", Severity.WARNING),
    _check("lineage_without_sequence", "Every new lineage has a sequence",
           "Each lineage declared new is accompanied by the sequence that defines it.",
           "name", Severity.BLOCKING),
    _check("sequence_without_declaration", "Every sequence has a declared lineage",
           "Each sequence supplied belongs to a lineage declared on NewLineages.",
           "sequence", Severity.WARNING),
    _check("record_without_country", "Records name a country",
           "Each host record says where it was found.",
           "record", Severity.WARNING),
    _check("record_without_prevalence", "Records report prevalence",
           "Each host record reports how many birds were screened and infected.",
           "record", Severity.INFO),
    _check("lineage_without_host_record", "New lineages have a host record",
           "Each newly declared lineage appears in at least one host record.",
           "name", Severity.WARNING),
    _check("reference_missing", "The submission names its reference",
           "The Reference sheet carries the study the data come from. An unpublished "
           "study names itself '<Authors> unpubl'.",
           "submission", Severity.BLOCKING),
    _check("reference_unpubl_malformed", "Unpublished references follow the convention",
           "A reference held before publication is named '<Authors> unpubl', so that "
           "every unpublished study in MalAvi can be found the same way.",
           "submission", Severity.WARNING),

    # -- from the malaviR validators (validate_record.R) --------------------------------
    _check("host_name_resolves", "Host names resolve to avian taxonomy",
           "Each host binomial reconciles to a current eBird/clootl species.",
           "record", Severity.WARNING),
    _check("host_geography_plausible", "Host and locality are plausible for the lineage",
           "Each lineage x host x locality has precedent in MalAvi's own records.",
           "record", Severity.INFO),
    _check("lineage_previously_recorded", "This lineage has been seen in this host and place",
           "MalAvi already records each lineage in this host species and country. "
           "Novelty here describes SAMPLING, not biology: it means no prior record, "
           "never that a detection is impossible, and is never on its own a reason to "
           "discard one.",
           "record", Severity.INFO),
    _check("sequence_qc", "Sequences pass malaviR's plausibility screen",
           "Each sequence looks like a typical MalAvi cytochrome b barcode: no stop "
           "codons, no changes at invariant sites, no chimera-like pattern.",
           "sequence", Severity.WARNING),

    # -- from this module ---------------------------------------------------------------
    _check("values_normalized", "Values were read as submitted",
           "No submitted value had to be reinterpreted on the way in.",
           "submission", Severity.INFO),
    _check("headers_intact", "The workbook's column headers were all present",
           "No column had to be identified by its position instead of its label.",
           "submission", Severity.WARNING),
]}


def _register_row_flag_checks() -> None:
    """Add one registry entry per row-flag code, described by row_flags itself.

    Importing the descriptions rather than restating them is what stops the registry
    and the flag definitions from drifting into saying different things about the same
    code -- the exact failure this module exists to end.
    """
    from .row_flags import FLAGS

    for code, meta in FLAGS.items():
        if code in CHECKS:
            continue
        severity = _SEVERITY_FROM_LEGACY.get(meta.get("severity", "info"), Severity.INFO)
        scope = "vector" if code.startswith("vector") else "record"
        # row_flags writes its descriptions as a clause ("the row carries no lineage
        # name"), so they are turned into the assertion the check makes.
        CHECKS[code] = Check(
            id=code,
            title=code.replace("_", " "),
            asserts=f"No row where {meta['description']}.",
            scope=scope,
            severity=severity,
        )


_register_row_flag_checks()


# ---------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckRun:
    """Every check result for one submission, plus the provenance to reproduce it."""

    results: Tuple[CheckResult, ...]
    provenance: Dict[str, Any]

    @property
    def incomplete(self) -> bool:
        """Did any check fail to execute? If so, this run is not a clean verdict."""
        return any(r.outcome is Outcome.ERROR for r in self.results)

    @property
    def blocking(self) -> Tuple[Finding, ...]:
        """Findings from blocking-severity checks: the things that stop an ingest."""
        out: List[Finding] = []
        for result in self.results:
            check = CHECKS.get(result.check_id)
            if check and check.severity is Severity.BLOCKING:
                out.extend(result.findings)
        return tuple(out)

    def counts(self) -> Dict[str, int]:
        counts = {outcome.value: 0 for outcome in Outcome}
        for result in self.results:
            counts[result.outcome.value] += 1
        counts["findings"] = sum(len(r.findings) for r in self.results)
        return counts

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provenance": self.provenance,
            "counts": self.counts(),
            "incomplete": self.incomplete,
            "results": [r.as_dict() for r in self.results],
        }


def _code_version() -> Optional[str]:
    """The commit this code is at, when it can be determined cheaply.

    A curator asked why the same submission gave a different answer six months apart
    needs to be able to point at two versions of the software, not just two dates.
    """
    try:
        from .config import repo_root

        proc = subprocess.run(
            ["git", "-C", str(repo_root()), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.stdout.strip() or None if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, Exception):  # noqa: BLE001
        return None


def _release() -> Optional[str]:
    try:
        from .config import load_config

        return load_config()["malaviR"]["release"]
    except Exception:                                        # noqa: BLE001
        return None


def _row_label(row: Dict[str, Any], kind: str) -> str:
    """Identify one row as compactly as it can be identified."""
    lineage = row.get("lineage_name") or "?"
    species = (row.get("host_species") if kind == "records"
               else row.get("vector_species")) or "?"
    where = row.get("country") or row.get("site")
    return f"{lineage} x {species}" + (f" ({where})" if where else "")


def _result(check_id: str, findings: Sequence[Finding], evaluated: int) -> CheckResult:
    """Build a PASS or FINDING result from what a check turned up."""
    findings = tuple(findings)
    return CheckResult(
        check_id=check_id,
        outcome=Outcome.FINDING if findings else Outcome.PASS,
        evaluated=evaluated,
        passed=max(evaluated - len(findings), 0),
        findings=findings,
    )


def _gate_checks(submission: Dict[str, Any], check_online: bool) -> List[CheckResult]:
    """The pre-ingest gate's findings, as check results.

    ``run_gate`` already returns findings without mutating the submission, so this is a
    translation rather than a re-run of anything. Its one pseudo-finding -- "DB snapshot
    unavailable" -- becomes a SKIP on the two checks that depend on the snapshot, which
    is what it always meant: those checks did not run.
    """
    from .gate import GateFinding, load_snapshot, run_gate

    snapshot_missing = load_snapshot() is None
    try:
        raw: List[GateFinding] = run_gate(submission, check_online=check_online)
    except Exception as exc:                                 # noqa: BLE001
        # One broken gate check must not cost the curator every other gate check, but
        # it must be loud: this is an execution error, not a skip.
        return [CheckResult(check_id="prevalence_sanity", outcome=Outcome.ERROR,
                            error=f"the pre-ingest gate raised {type(exc).__name__}: {exc}")]

    by_check: Dict[str, List[Finding]] = {}
    for finding in raw:
        if finding.check == "snapshot":
            continue                                          # handled as a skip below
        by_check.setdefault(finding.check, []).append(Finding(
            subject=finding.where or "submission",
            message=finding.message,
            severity=_SEVERITY_FROM_LEGACY.get(finding.severity, Severity.INFO),
        ))

    records = submission.get("records", [])
    vectors = submission.get("vectors", [])
    accessions = submission.get("accessions", [])
    evaluated = {
        "prevalence_sanity": len(records),
        "accession_format": len(accessions),
        "accession_collision": len(accessions),
        "lineage_known": len({r.get("lineage_name") for r in records
                              if r.get("lineage_name")}),
        "vector_sanity": len(vectors),
    }

    results: List[CheckResult] = []
    for check_id, count in evaluated.items():
        if snapshot_missing and check_id in ("accession_collision", "lineage_known"):
            results.append(CheckResult(
                check_id=check_id, outcome=Outcome.SKIP, evaluated=count,
                skip_reason="the malaviR database snapshot has not been generated "
                            "(run curation/r/gate_reference.R)"))
            continue
        if check_id == "accession_collision" and not check_online:
            # Only the INSDC availability half needs the network; the collision check
            # itself is snapshot-backed, so it still runs. Nothing to skip here.
            pass
        results.append(_result(check_id, by_check.get(check_id, []), count))
    return results


def _row_flag_checks(submission: Dict[str, Any]) -> List[CheckResult]:
    """Row-level flags, as one result per flag code.

    ``flag_rows`` writes ``flags`` and ``tier`` onto each row, which is its documented
    purpose and what the triage tiers are built from, so it is left to do that. What
    this adds is the reverse view: per flag code, which rows raised it and where they
    are in the workbook.
    """
    from .row_flags import FLAGS, INDEX_BACKED_FLAGS, flag_rows

    results: List[CheckResult] = []
    index_available = True
    for kind in ("records", "vectors"):
        rows = submission.get(kind, [])
        if not rows:
            continue
        try:
            summary = flag_rows(rows, kind=kind)
            if not (summary or {}).get("release_index_available", True):
                index_available = False
        except FileNotFoundError as exc:
            for code in FLAGS:
                results.append(CheckResult(
                    check_id=code, outcome=Outcome.SKIP, evaluated=len(rows),
                    skip_reason=f"the pinned release tables are not on disk: {exc}"))
            return results
        except Exception as exc:                             # noqa: BLE001
            results.append(CheckResult(
                check_id="lineage_known", outcome=Outcome.ERROR,
                error=f"row flagging raised {type(exc).__name__}: {exc}"))
            return results

    by_code: Dict[str, List[Finding]] = {}
    evaluated_for: Dict[str, int] = {}
    for kind in ("records", "vectors"):
        rows = submission.get(kind, [])
        for row in rows:
            for flag in row.get("flags") or []:
                code = flag.get("code")
                if not code:
                    continue
                evaluated_for.setdefault(code, 0)
                by_code.setdefault(code, []).append(Finding(
                    subject=_row_label(row, kind),
                    message=flag.get("message", ""),
                    severity=_SEVERITY_FROM_LEGACY.get(flag.get("severity"),
                                                       Severity.INFO),
                    source=row.get("source"),
                    evidence={"detail": flag["detail"]} if flag.get("detail") else {},
                ))

    total_rows = len(submission.get("records", [])) + len(submission.get("vectors", []))
    for code in FLAGS:
        if total_rows == 0:
            continue
        # An index-backed flag with no index behind it is unanswered, not clean.
        if not index_available and code in INDEX_BACKED_FLAGS:
            results.append(CheckResult(
                check_id=code, outcome=Outcome.SKIP, evaluated=total_rows,
                skip_reason="the pinned release tables are not in this checkout, so "
                            "nothing could be compared against MalAvi"))
            continue
        results.append(_result(code, by_code.get(code, []), total_rows))
    return results


def _screen_checks(screen: Optional[Any]) -> List[CheckResult]:
    """The template screen's issues, as check results.

    ``screen`` is what ``check_template.screen`` returned (one report, or the list of
    them written to ``screen.json``). The screen stays the authority on sequences: it
    is the tested code that decides whether a sequence is already a named lineage, and
    that judgment must not exist twice.
    """
    if not screen:
        # Not an empty list. run_checks is a public API and the PDF/table-extraction path
        # calls it with no screen at all, which used to make every screen check -- four of
        # them blocking, including "is this sequence already a named lineage" -- simply
        # absent from checks.json, with run.incomplete still False. Absent reads as
        # "nothing to say"; skipped says nobody looked.
        return [CheckResult(
            check_id=check_id, outcome=Outcome.SKIP, evaluated=0,
            skip_reason="this submission did not arrive as a workbook, so the template "
                        "screen did not run")
            for check_id in sorted(_SCREEN_CHECK_IDS)]
    reports = screen if isinstance(screen, list) else [screen]

    by_code: Dict[str, List[Finding]] = {}
    sequences = 0
    names = 0
    uncoded: List[Finding] = []
    # Checks the screen could not perform. A skipped check is not a passed one, and the
    # name-collision check is the one this matters most for: reporting it as passed when
    # the snapshot was missing told a curator a name was free when nobody had looked.
    skipped: Dict[str, str] = {}

    for report in reports:
        for entry in report.get("skipped", []):
            skipped.setdefault(entry["code"], entry.get("reason", "not stated"))
        sequences += len(report.get("sequences", []))
        names += len(report.get("lineages", {}))
        for issue in report.get("issues", []):
            finding = Finding(
                # The screen states what an issue is about; fall back to the workbook
                # only for the few issues that really are about the file as a whole
                # (a missing Reference sheet, say).
                subject=issue.get("subject") or report.get("workbook", "the workbook"),
                message=issue.get("message", ""),
                severity=_SEVERITY_FROM_LEGACY.get(issue.get("severity"), Severity.INFO),
                source={"file": report["workbook"]} if report.get("workbook") else None,
            )
            code = issue.get("code")
            if code and code in CHECKS:
                by_code.setdefault(code, []).append(finding)
            else:
                # An issue the screen raised that this registry has no id for. Reported
                # rather than dropped: an unrecognized finding is still a finding, and
                # silently discarding it would be the worst possible failure here.
                uncoded.append(finding)

    scope_size = {"sequence": sequences, "name": names}
    results: List[CheckResult] = []
    for check_id, check in CHECKS.items():
        if check_id in skipped:
            results.append(CheckResult(
                check_id=check_id, outcome=Outcome.SKIP,
                evaluated=scope_size.get(check.scope, 1),
                skip_reason=skipped[check_id]))
            continue
        # Membership of _SCREEN_CHECK_IDS is tested first: the three screen checks whose
        # scope is neither "sequence" nor "name" (reference_missing,
        # record_without_country, record_without_prevalence) were otherwise dropped by
        # the scope guard and never reported as passed, contradicting the comment on
        # _SCREEN_CHECK_IDS.
        if check_id not in by_code and check_id not in _SCREEN_CHECK_IDS:
            if check.scope not in ("sequence", "name"):
                continue
        results.append(_result(check_id, by_code.get(check_id, []),
                               scope_size.get(check.scope, 1)))

    if uncoded:
        results.append(CheckResult(
            check_id="screen_uncoded", outcome=Outcome.FINDING,
            evaluated=len(uncoded), findings=tuple(uncoded)))
    return results


# The checks that depend on R. Named in one place so that adding one cannot leave
# it missing from the skip and error paths, which is precisely when its absence
# would matter most.
_R_CHECK_IDS = ("host_name_resolves", "host_geography_plausible",
                "lineage_previously_recorded", "sequence_qc")


# The checks the template screen is responsible for. Listed explicitly so that a screen
# which raised no issues still reports those checks as PASSED rather than as absent --
# "we looked and it was fine" is information a curator needs.
_SCREEN_CHECK_IDS = frozenset({
    "name_already_in_malavi", "name_claimed_by_another_submission",
    "sequence_is_known_lineage", "sequence_needs_reframing",
    "sequence_stop_codon", "sequence_unplaceable", "accession_malformed",
    "lineage_without_sequence", "sequence_without_declaration", "record_without_country",
    "record_without_prevalence", "lineage_without_host_record", "reference_missing",
    "reference_unpubl_malformed",
})


def _r_checks(submission: Dict[str, Any], version: str) -> List[CheckResult]:
    """Host-name reconciliation and host/geography plausibility, from malaviR."""
    from .validate import run_validation

    records = submission.get("records", [])
    sequences = submission.get("sequences", [])
    outcome = run_validation(submission, version=version)

    if outcome.outcome == "skip":
        return [CheckResult(check_id=check_id, outcome=Outcome.SKIP,
                            evaluated=len(records), skip_reason=outcome.reason)
                for check_id in _R_CHECK_IDS]
    if outcome.outcome == "error":
        return [CheckResult(check_id=check_id, outcome=Outcome.ERROR,
                            evaluated=len(records), error=outcome.reason)
                for check_id in _R_CHECK_IDS]

    result = outcome.result or {}
    source_for = {
        (r.get("host_species") or ""): r.get("source") for r in records
    }

    taxonomy = [Finding(subject=entry.get("host_species") or "?",
                        message=entry.get("reason") or "did not resolve",
                        severity=Severity.WARNING,
                        source=source_for.get(entry.get("host_species") or ""))
                for entry in result.get("host_taxonomy", []) if entry.get("flagged")]

    geography = [Finding(
        subject=f"{entry.get('lineage_name')} x {entry.get('host_species')}",
        message=f"[{entry.get('type')}] {entry.get('reason')}",
        severity=_SEVERITY_FROM_LEGACY.get(entry.get("severity"), Severity.INFO),
        source=source_for.get(entry.get("host_species") or ""),
    ) for entry in result.get("record_flags", [])]

    # Host and biogeographic plausibility. Everything but "previously_recorded" is
    # reported, and reported as INFO: these describe where people have looked, so a
    # new host family for a well-studied generalist is unremarkable while a new host
    # order for a single-study lineage is worth a hard look. The check tells the
    # curator which it is; it does not decide.
    plausibility: List[Finding] = []
    novelty_errors: List[str] = []
    for entry in result.get("plausibility", []) or []:
        if entry.get("error"):
            novelty_errors.append(str(entry["error"]))
            continue
        call = entry.get("call")
        if call in (None, "previously_recorded"):
            continue
        # How well known the lineage is, which is what tells a curator whether novelty
        # here is unremarkable or worth a hard look. Omitted for a lineage MalAvi does
        # not hold at all, where the counts are undefined and printing "NA studies"
        # would look like a fault rather than an absence.
        studies = entry.get("n_studies")
        countries = entry.get("n_countries")
        context = ""
        if call != "lineage_not_in_malavi" and studies is not None:
            context = (f" (MalAvi knows this lineage from {studies} study/studies "
                       f"in {countries} country/countries)")
        plausibility.append(Finding(
            subject=f"{entry.get('lineage')} x {entry.get('host')}",
            message=f"{call}"
                    + (f" — {entry['flags']}" if entry.get("flags") else "")
                    + context,
            severity=Severity.INFO,
            source=source_for.get(entry.get("host") or ""),
            evidence={key: entry.get(key) for key in
                      ("n_studies", "n_host_records", "n_countries", "host_recorded",
                       "host_family_recorded", "country_recorded")},
        ))

    # Sequence QC. A call of "known_lineage" here is not a finding: the screen already
    # reports an identical sequence as a blocking issue, and saying it twice in two
    # vocabularies is how a report becomes something a curator skims.
    sequence_findings: List[Finding] = []
    qc_errors: List[str] = []
    source_by_lineage = {s.get("lineage_name"): s.get("source") for s in sequences}
    for entry in result.get("sequence_qc", []) or []:
        if entry.get("error"):
            qc_errors.append(str(entry["error"]))
            continue
        call = entry.get("call")
        if call in ("known_lineage", "plausible_new_lineage"):
            continue
        # malaviR's own message leads, when there is one. It is written precisely to
        # stop a curator over-reading the flag list: a sequence pasted outside the
        # 479 bp frame produces stop codons, "never observed" bases and a chimera-like
        # pattern all at once, and every one of those is a consequence of the padding
        # rather than independent evidence of anything. Putting the flags first would
        # present one fixable problem as nine alarming ones.
        explanation = entry.get("message")
        detail = f" — {entry['flags']}" if entry.get("flags") else ""
        message = (f"{explanation} (verdict: {call}; underlying flags:{detail or ' none'})"
                   if explanation else f"{call}{detail}")
        sequence_findings.append(Finding(
            subject=str(entry.get("lineage_name") or "a sequence"),
            message=message,
            severity=Severity.WARNING,
            source=source_by_lineage.get(entry.get("lineage_name")),
            evidence={key: entry.get(key) for key in
                      ("score", "nearest_lineage", "nearest_distance", "n_mutations",
                       "n_nonsynonymous", "n_stop_codons")},
        ))

    results = [
        _result("host_name_resolves", taxonomy, len(result.get("host_taxonomy", []))),
        _result("host_geography_plausible", geography, len(records)),
    ]
    # A sub-check that fell over inside R is an error on that check alone, so the
    # others still reach the curator -- but it is an error, not a quiet omission.
    results.append(
        CheckResult(check_id="lineage_previously_recorded", outcome=Outcome.ERROR,
                    evaluated=len(records), error="; ".join(novelty_errors))
        if novelty_errors
        else _result("lineage_previously_recorded", plausibility, len(records)))
    results.append(
        CheckResult(check_id="sequence_qc", outcome=Outcome.ERROR,
                    evaluated=len(sequences), error="; ".join(qc_errors))
        if qc_errors
        else _result("sequence_qc", sequence_findings, len(sequences)))
    return results


def _adapter_checks(submission: Dict[str, Any]) -> List[CheckResult]:
    """What the adapter had to decide about the submitter's file.

    Both of these are INFO-ish by nature but they belong in the report, because they are
    the only place a curator learns that the system reinterpreted something.
    """
    provenance = submission.get("provenance", {}) or {}

    changes = provenance.get("normalizations", []) or []
    normalized = _result("values_normalized", [
        Finding(subject=str(change.get("field", "value")),
                message=f"read {change.get('submitted')!r} as "
                        f"{change.get('normalized')!r}",
                severity=Severity.INFO,
                source=change.get("source"))
        for change in changes
    ], len(changes))

    repairs = provenance.get("header_repairs", []) or []
    headers = _result("headers_intact", [
        Finding(subject="column header", message=note, severity=Severity.WARNING)
        for note in repairs
    ], len(repairs))

    return [normalized, headers]


def run_checks(
    submission: Dict[str, Any],
    screen: Optional[Any] = None,
    check_online: bool = False,
    malavir_version: str = "latest",
    run_r: bool = True,
) -> CheckRun:
    """Run every registered check over one submission and return the results.

    Args:
        submission: a submission conforming to ``schemas/submission.schema.json``.
        screen: the template screen's report, when the submission came from a workbook.
        check_online: allow the checks that need the network (INSDC accession lookup).
        malavir_version: the malaviR release the R validators should read.
        run_r: set False to skip the R validators deliberately (they are then reported
            as skipped for that reason, never as passed).

    Never raises for a failing check: a check that breaks is reported as an ERROR so the
    rest of the run still reaches the curator.
    """
    results: List[CheckResult] = []
    results.extend(_gate_checks(submission, check_online))
    results.extend(_row_flag_checks(submission))
    results.extend(_screen_checks(screen))
    results.extend(_adapter_checks(submission))

    if run_r:
        results.extend(_r_checks(submission, malavir_version))
    else:
        results.extend(
            CheckResult(check_id=check_id, outcome=Outcome.SKIP,
                        skip_reason="the R validators were not requested for this run")
            for check_id in _R_CHECK_IDS)

    # Blocking checks first, then warnings, then info; within a severity, findings and
    # errors before passes. A curator reads from the top and should meet the things that
    # need them there.
    severity_order = {Severity.BLOCKING: 0, Severity.WARNING: 1, Severity.INFO: 2}
    outcome_order = {Outcome.ERROR: 0, Outcome.FINDING: 1, Outcome.SKIP: 2, Outcome.PASS: 3}

    def sort_key(result: CheckResult):
        check = CHECKS.get(result.check_id)
        return (outcome_order[result.outcome],
                severity_order.get(check.severity, 3) if check else 3,
                result.check_id)

    results.sort(key=sort_key)

    provenance = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema_version": submission.get("schema_version"),
        "release": _release(),
        "tool_version": __version__,
        "code_version": _code_version(),
        "online": bool(check_online) and os.environ.get("MALAVI_GATE_OFFLINE") != "1",
        "submission_source": (submission.get("provenance") or {}).get("source"),
    }
    return CheckRun(results=tuple(results), provenance=provenance)


# ---------------------------------------------------------------------------------------
# Console rendering
# ---------------------------------------------------------------------------------------

_MARK = {
    Outcome.PASS: "✔",       # heavy check mark
    Outcome.FINDING: "✖",    # heavy multiplication x
    Outcome.SKIP: "-",
    Outcome.ERROR: "⚠",      # warning sign
}


def render_console(run: CheckRun, heading: Optional[str] = None,
                   redact: bool = False, max_subjects: int = 4) -> str:
    """Render a check run the way a test suite reports itself.

    ``redact`` prints check ids and counts but no subjects. Use it anywhere the output
    is captured somewhere more people can read than should see the submission itself --
    GitHub Actions logs above all, which every repository collaborator can read while
    the submission is still unpublished.
    """
    lines: List[str] = []
    if heading:
        lines.append(heading)

    if run.incomplete:
        lines.append("  ** validation is INCOMPLETE: a check failed to execute **")

    for result in run.results:
        check = CHECKS.get(result.check_id)
        mark = _MARK[result.outcome]
        label = f"  {mark} {result.check_id:<28}"

        if result.outcome is Outcome.SKIP:
            lines.append(f"{label} skipped: {result.skip_reason}")
            continue
        if result.outcome is Outcome.ERROR:
            # An error message can carry a subprocess's stderr, and R will happily echo
            # the host names and localities it was handed. Under redaction the failure
            # is still announced -- it must be, it means validation is incomplete --
            # but its text stays in the report rather than going into a log.
            detail = ("details in the report" if redact
                      else result.error or "no detail recorded")
            lines.append(f"{label} ERROR: {detail}")
            continue
        if result.outcome is Outcome.PASS:
            lines.append(f"{label} {result.evaluated} evaluated")
            continue

        summary = f"{len(result.findings)}"
        if redact:
            lines.append(f"{label} {summary} finding(s)")
            continue

        subjects = ", ".join(f.subject for f in result.findings[:max_subjects])
        if len(result.findings) > max_subjects:
            subjects += f", and {len(result.findings) - max_subjects} more"
        lines.append(f"{label} {summary}  {subjects}")
        # The first finding's own words, so the line above is not the only explanation.
        if result.findings and check:
            lines.append(f"      {result.findings[0].message}")

    counts = run.counts()
    lines.append(f"  {counts['finding']} with findings · {counts['skip']} skipped "
                 f"· {counts['error']} errored · {counts['pass']} passed")
    return "\n".join(lines)
