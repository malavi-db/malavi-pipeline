"""Render a review-ready report from candidate submission records.

Produces a concise Markdown digest per paper — reference, mined accessions
(noting any inferred from ranges), candidate host species, localities, and review
flags — for a curator (Staffan or Vincenzo) to accept or reject. Validation flags
from the malaviR R checks are slotted in once r/validate_record.R is wired up.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# How many rows of the review queue to print in full. A paper can produce
# thousands of rows and the report is meant to be read, so the queue is capped
# and the remainder is pointed at submission.json, which always holds every row.
_MAX_QUEUE_ROWS = 40

# The order a curator should work the tiers in, with what each one means. Kept
# here (not imported from row_flags) so the report renders a submission that was
# produced by an older run and carries tiers this build does not know about.
_TIER_ORDER = ("review", "new", "confirms", "incomplete")
_TIER_BLURB = {
    "review": "need a curator's judgment",
    "new": "complete and not already in MalAvi — candidate additions",
    "confirms": "correctly read, but MalAvi already holds the association",
    "incomplete": "no lineage or no species — not an association yet",
}


def _fmt_num(n: Any) -> str:
    """Render a count for the report: integers without a trailing '.0', else '?'."""
    if n is None:
        return "?"
    return str(int(n)) if isinstance(n, float) and n.is_integer() else str(n)


def _reference_line(ref: Dict[str, Any]) -> str:
    bits = []
    if ref.get("title"):
        bits.append(f"**{ref['title']}**")
    if ref.get("year"):
        bits.append(f"({ref['year']})")
    if ref.get("doi"):
        bits.append(f"doi:{ref['doi']}")
    if ref.get("pmid"):
        bits.append(f"PMID:{ref['pmid']}")
    return " ".join(bits) if bits else "(reference details not extracted)"


def _row_label(row: Dict[str, Any]) -> str:
    """Identify one row in the review queue as compactly as it can be identified."""
    lineage = row.get("lineage_name") or "?"
    # A record names a bird, a vector row an arthropod; whichever this row has.
    species = row.get("host_species") or row.get("vector_species") or "?"
    where = row.get("country") or row.get("site")
    return f"{lineage} × {species}" + (f" ({where})" if where else "")


def _render_triage(rows: List[Dict[str, Any]], noun: str) -> List[str]:
    """Tier counts and the review queue for one set of rows (records or vectors).

    ``noun`` is the singular name of a row ("record", "vector row"); it is
    pluralized for counts other than one.

    Returns an empty list when the rows carry no tiers at all, so a submission
    produced before row_flags existed renders exactly as it used to.
    """
    def plural(count: int) -> str:
        return noun if count == 1 else noun + "s"

    tiered = [r for r in rows if r.get("tier")]
    if not tiered:
        return []

    counts: Dict[str, int] = {}
    for row in tiered:
        counts[row["tier"]] = counts.get(row["tier"], 0) + 1

    # Tiers this build knows, in working order, then anything unrecognized.
    names = [t for t in _TIER_ORDER if t in counts]
    names += sorted(t for t in counts if t not in _TIER_ORDER)

    lines = [f"- Triage of {len(tiered)} {plural(len(tiered))}:"]
    for name in names:
        blurb = _TIER_BLURB.get(name)
        lines.append(f"  - **{name}**: {counts[name]}"
                     + (f" — {blurb}" if blurb else ""))

    queue = [r for r in tiered if r.get("tier") == "review"]
    if queue:
        lines.append(f"- Review queue ({len(queue)} {plural(len(queue))}):")
        for row in queue[:_MAX_QUEUE_ROWS]:
            lines.append(f"  - {_row_label(row)}")
            for flag in row.get("flags") or []:
                # Only the flags that put the row here are worth the space; the
                # purely informational ones are in submission.json.
                if flag.get("severity") in ("warn", "info"):
                    detail = f" — {flag['detail']}" if flag.get("detail") else ""
                    lines.append(f"    - [{flag.get('severity', '?').upper()}] "
                                 f"{flag.get('message')}{detail}")
        if len(queue) > _MAX_QUEUE_ROWS:
            lines.append(f"  - …and {len(queue) - _MAX_QUEUE_ROWS} more — see "
                         "submission.json for every row.")
    return lines


def render_submission(submission: Dict[str, Any], heading: Optional[str] = None) -> str:
    """Render one submission as a Markdown section."""
    ref = submission.get("reference", {})
    prov = submission.get("provenance", {})
    records = submission.get("records", [])
    vectors = submission.get("vectors", [])
    accessions = submission.get("accessions", [])
    ranges = prov.get("accession_ranges", [])

    def _host_label(r: Dict[str, Any]) -> str:
        """Host species, annotating reported prevalence when present."""
        label = r["host_species"]
        tested, found = r.get("number_tested"), r.get("number_found")
        if tested is not None or found is not None:
            label += f" [{_fmt_num(found)}/{_fmt_num(tested)} infected/tested]"
        return label

    hosts = [_host_label(r) for r in records if r.get("host_species")]
    # With no structured table there are no records, but the host names mined
    # from the text are still the curator's starting point. Show them, mirroring
    # how candidate_countries is handled below, so nothing extracted is hidden
    # just because it could not be turned into a record.
    if not hosts:
        hosts = sorted(prov.get("candidate_hosts", []))
    vec_species = [v["vector_species"] for v in vectors if v.get("vector_species")]
    # Prefer countries attached to records/vectors; fall back to all candidates.
    countries = sorted({r["country"] for r in records if r.get("country")}
                       | {v["country"] for v in vectors if v.get("country")})
    if not countries:
        countries = sorted(prov.get("candidate_countries", []))

    lines: List[str] = [f"## {heading}" if heading else "## Submission"]
    lines.append(f"- Reference: {_reference_line(ref)}")
    lines.append(f"- Accessions ({len(accessions)}): "
                 + (", ".join(accessions) if accessions else "none found"))
    if ranges:
        lines.append(f"- Ranges expanded (interior accessions inferred): {', '.join(ranges)}")
    lines.append(f"- Candidate hosts ({len(hosts)}): "
                 + (", ".join(hosts) if hosts else "none found"))
    if vec_species:
        lines.append(f"- Candidate vectors ({len(vec_species)}): {', '.join(vec_species)}")
    lines.append(f"- Localities: " + (", ".join(countries) if countries else "none found"))

    # Where the curator's attention should go, and why. This is the point of the
    # report for any paper big enough that reading every row is not an option.
    lines.extend(_render_triage(records, "record"))
    lines.extend(_render_triage(vectors, "vector row"))

    flags: List[str] = []
    if prov.get("needs_supplement"):
        flags.append("paper references supplementary data — the host×lineage table "
                     "likely lives there (check the supplement)")
    if prov.get("needs_review", True):
        flags.append("auto-extracted — curator review required")
    # malaviR validation flags (r/validate_record.R, r/host_geo_flag.R).
    val = submission.get("validation")
    if val:
        flags.extend(val if isinstance(val, list) else [str(val)])
    if flags:
        lines.append("- ⚠ Flags:")
        lines.extend(f"  - {f}" for f in flags)

    # Pre-ingest validation gate (gate.py): show pass/fail and any findings.
    gate = submission.get("gate")
    if gate:
        status = "PASSED" if gate.get("passed") else "BLOCKED"
        lines.append(f"- Automated gate: **{status}** "
                     f"({gate.get('n_error', 0)} error, {gate.get('n_warn', 0)} warn)")
        order = {"error": 0, "warn": 1, "info": 2}
        for f in sorted(gate.get("findings", []), key=lambda x: order.get(x.get("severity"), 3)):
            loc = f" ({f['where']})" if f.get("where") else ""
            lines.append(f"  - [{f.get('severity', '?').upper()}] {f.get('check')}: "
                         f"{f.get('message')}{loc}")
    lines.append("")
    return "\n".join(lines)


def render_report(submissions: List[Dict[str, Any]], headings: Optional[List[str]] = None) -> str:
    """Return a Markdown curation report for the given submissions."""
    out = ["# MalAvi curation queue — review report", "",
           f"{len(submissions)} paper(s) staged for review. Nothing has been added to "
           "MalAvi; accept/reject each below.", ""]
    for i, sub in enumerate(submissions):
        heading = headings[i] if headings and i < len(headings) else None
        out.append(render_submission(sub, heading=heading))
    return "\n".join(out)
