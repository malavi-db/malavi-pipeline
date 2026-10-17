"""End-to-end curation pipeline: PDF -> candidate submission -> curator report.

Ties the stages together (pdf_extract -> accession_mine + hosts_geography ->
record_builder) so a folder of PDFs becomes a single review report. The DOI is
mined from the PDF when the caller does not supply one; title/year still default
to the file name (provisional) unless given.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .accession_mine import mine_accessions, mine_doi
from .curator_report import render_report
from .hosts_geography import extract_hosts_geography
from .pdf_extract import extract_pdf
from .record_builder import build_submission


def process_pdf(path: str | Path, reference: Optional[Dict[str, Any]] = None,
                schema_validate: bool = True, run_malavir: bool = False,
                version: str = "latest") -> Dict[str, Any]:
    """Run the full extraction pipeline on one PDF and return a submission dict.

    ``schema_validate`` checks the record against submission.schema.json.
    ``run_malavir`` (off by default) additionally runs the malaviR validation
    layer (host-name reconciliation + improbable host/locality flags) via R.
    """
    path = Path(path)
    reference = reference or {"title": path.stem}

    doc = extract_pdf(path)
    # mining_text() unions reading-order prose, layout-preserved text (unruled
    # column tables / supplements), and ruled tables — the full recall surface.
    surface = doc.mining_text()
    accessions = mine_accessions(surface.upper())
    hostgeo = extract_hosts_geography(surface)
    # Fill the DOI from the PDF when the caller did not supply one (curator confirms).
    if reference.get("doi") is None:
        reference["doi"] = mine_doi(surface)

    submission = build_submission(reference, accessions=accessions, hostgeo=hostgeo,
                                  validate=schema_validate)
    # Automated pre-ingest gate (pure-Python checks + DB-snapshot collision checks).
    from .gate import apply_gate
    # check_online: verify the cited accessions are actually retrievable from
    # INSDC. A well-formed accession can still be one the authors reserved and
    # never released, which no offline check can detect. Degrades to a warning
    # if the network is unavailable; set MALAVI_GATE_OFFLINE=1 to skip it.
    apply_gate(submission, check_online=True)
    if run_malavir:
        from .validate import validate_submission
        validate_submission(submission, version=version)
    return submission


def process_dir(pdf_dir: str | Path, run_malavir: bool = False) -> str:
    """Process every PDF in a directory and return a combined curator report."""
    pdf_dir = Path(pdf_dir)
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    submissions: List[Dict[str, Any]] = []
    headings: List[str] = []
    for pdf in pdfs:
        submissions.append(process_pdf(pdf, run_malavir=run_malavir))
        headings.append(pdf.stem)
    return render_report(submissions, headings=headings)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build a curator review report from PDFs.")
    ap.add_argument("pdf_dir", help="directory of publication PDFs")
    ap.add_argument("--out", help="write the Markdown report here (default: stdout)")
    ap.add_argument("--validate", action="store_true",
                    help="also run the malaviR validation layer (needs Rscript + malaviR)")
    args = ap.parse_args(argv)

    report = process_dir(args.pdf_dir, run_malavir=args.validate)
    if args.out:
        Path(args.out).write_text(report + "\n")
        print(f"Wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
