"""Curator self-intake: process one paper (PDF + supplementary files) at a time.

Workflow:
  1. Drop ONE paper's files into ``curation/intake/processing/`` — its PDF plus any
     supplementary tables (.xlsx/.csv/.tsv/.docx). Keeping one paper per batch means
     a paper's files stay grouped and are never confused with another's.
  2. Run this module. It extracts accessions + hosts from the PDF, structured
     lineage×host×locality rows from the supplementary tables, builds a
     schema-valid submission, optionally runs malaviR validation, and writes a
     curator report.
  3. Everything (original inputs + outputs + manifest) is bundled and MOVED to
     ``curation/intake/processed/<slug>_<timestamp>/`` for storage and revisiting.

Run:
    python -m malavi_curation.intake [--validate]
    python -m malavi_curation.intake --processing DIR --processed DIR
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .accession_mine import mine_accessions, mine_doi
from .config import repo_root
from .curator_report import render_report
from .host_names import canonicalize_rows
from .hosts_geography import extract_hosts_geography
from .lineage_resolve import LineageResolver, resolve_rows
from .source_scope import assess as assess_source_scope, classify_rows as classify_scope
from .pdf_extract import extract_pdf
from .record_builder import build_submission
from .row_flags import flag_rows
from .table_extract import extract_pdf_tables, extract_table_file

PDF_SUFFIXES = {".pdf"}
TABLE_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".tab", ".docx"}


def _intake_dirs(processing: Optional[Path], processed: Optional[Path]):
    base = repo_root() / "curation" / "intake"
    return (processing or base / "processing", processed or base / "processed")


def _slug(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip()
    return "_".join(keep.split()) or "paper"


def _batch_files(processing: Path) -> List[Path]:
    """All real input files dropped in processing/ (ignore dotfiles/README)."""
    return sorted(
        f for f in processing.iterdir()
        if f.is_file() and not f.name.startswith(".") and f.name.lower() != "readme.md"
    )


def process_batch(processing: Path, processed: Path,
                  run_malavir: bool = False, version: str = "latest") -> Optional[Path]:
    """Process the single paper currently in ``processing/``; return the bundle dir.

    Returns None if the processing folder is empty.
    """
    files = _batch_files(processing)
    if not files:
        return None

    pdfs = [f for f in files if f.suffix.lower() in PDF_SUFFIXES]
    tables = [f for f in files if f.suffix.lower() in TABLE_SUFFIXES]
    other = [f for f in files if f not in pdfs and f not in tables]

    # The reference/title comes from the PDF if present, else the first file.
    primary = pdfs[0] if pdfs else files[0]
    slug = _slug(primary.stem)
    reference = {"title": primary.stem}

    # --- Extract from the PDF (text surface + printed tables) -----------------
    text_surface = ""
    structured: List[Dict[str, Any]] = []
    structured_vectors: List[Dict[str, Any]] = []
    table_accessions: List[str] = []
    table_reports: List[Dict[str, Any]] = []
    for pdf in pdfs:
        doc = extract_pdf(pdf)
        # mining_text() includes layout-preserved text so unruled column tables in
        # the main PDF *and* in PDF supplements (host x lineage, prevalence) are seen.
        text_surface += doc.mining_text() + "\n"
        # A paper without a supplementary spreadsheet still prints its host x
        # lineage table; carve it back out and parse it like any other table.
        pdf_tables = extract_pdf_tables(doc)
        structured.extend(pdf_tables.rows)
        structured_vectors.extend(pdf_tables.vectors)
        table_accessions.extend(pdf_tables.accessions)
        table_reports.append({"file": pdf.name, "rows": len(pdf_tables.rows),
                              "vector_rows": len(pdf_tables.vectors),
                              "columns_detected": pdf_tables.columns_detected,
                              "source": "printed table in PDF"})

    # --- Extract structured rows from supplementary tables --------------------
    for tbl in tables:
        try:
            ex = extract_table_file(tbl)
        except Exception as exc:  # unsupported/corrupt file -> note and continue
            table_reports.append({"file": tbl.name, "error": str(exc)})
            continue
        structured.extend(ex.rows)
        structured_vectors.extend(ex.vectors)
        table_accessions.extend(ex.accessions)
        text_surface += ex.text + "\n"
        table_reports.append({"file": tbl.name, "rows": len(ex.rows),
                              "vector_rows": len(ex.vectors),
                              "columns_detected": ex.columns_detected})

    # --- Resolve study-local lineage names from the sequences ------------------
    # A supplement that prints its own haplotype codes ("T001") alongside the raw
    # cytb sequence is stating which MalAvi lineage each row is; the sequence is
    # the join key its identifier is not. Rows whose printed name MalAvi already
    # knows are left alone. See lineage_resolve.
    resolution_tally: Dict[str, int] = {}
    resolver = LineageResolver.for_pinned_release()
    if resolver is not None:
        for rows in (structured, structured_vectors):
            for verdict, count in resolve_rows(rows, resolver).items():
                resolution_tally[verdict] = resolution_tally.get(verdict, 0) + count

    # --- Decide which rows are this paper's own data ---------------------------
    # A table in a paper is not always the paper's own results: Harl et al 2026's
    # Additional file 2 lists 88 previously published mitochondrial genomes, and
    # Fecchio et al 2023b's Table S1 pools six earlier studies. Those rows are
    # read correctly but belong to somebody else's reference in MalAvi. Only the
    # paper's own statements about provenance are used, and every row is kept --
    # the label is advice for the curator, never a filter. See source_scope.
    scope = assess_source_scope(text_surface)
    scope_tally = classify_scope(structured, scope)
    classify_scope(structured_vectors, scope)

    # --- Put host and vector names into MalAvi's namespace --------------------
    # Papers use current taxonomy; MalAvi files a bird under one name. Astur
    # gentilis is ACCIPITER GENTILIS here, Crithagra flaviventris is SERINUS
    # FLAVIVENTRIS. Only documented genus revisions are applied, and the
    # document's own wording is kept. See host_names.
    host_name_tally = canonicalize_rows(structured, "host_species")
    vector_name_tally = canonicalize_rows(structured_vectors, "vector_species",
                                         kind="vectors")

    # --- Flag and tier every row for the curator ------------------------------
    # This runs last, because it reads the results of everything above: a row's
    # tier depends on whether its lineage resolved, whether its host name was
    # canonicalized into one MalAvi holds, and what the paper said about the
    # row's provenance. Flags are additive and no row is ever dropped -- the
    # point is to tell the curator which rows need their attention, since a
    # paper can produce thousands (Fecchio et al 2023b: 2,683). See row_flags.
    triage = flag_rows(structured)
    flag_rows(structured_vectors, kind="vectors")

    accessions = mine_accessions(text_surface.upper())
    # Accessions read from a table's accession column, unioned with the mined
    # ones. A column value is a stronger signal than a text match, and some are
    # only ever present in a table (Perrin et al 2026's vector sheet).
    accessions.nucleotide = sorted(
        set(accessions.nucleotide) | {a.upper() for a in table_accessions})
    hostgeo = extract_hosts_geography(text_surface)
    # Capture the paper's DOI from the (original-case) text so it lands in the
    # submission's reference. The curator confirms it; needs_review stays true.
    reference["doi"] = mine_doi(text_surface)

    # A DOI is enough to get the real citation. Without this the reference is
    # whatever the filename happened to be, which is how a hooded-vulture paper
    # ended up titled "s11686-026-01301-5". Crossref is the registration
    # authority for the DOI, so this is a lookup of the publisher's own metadata,
    # not an inference. Any failure leaves the mined values untouched.
    if reference.get("doi"):
        from .reference_lookup import enrich_reference
        enrich_reference(reference)

    submission = build_submission(
        reference, accessions=accessions, hostgeo=hostgeo,
        structured_records=structured or None,
        structured_vectors=structured_vectors or None, validate=True,
    )
    # Automated pre-ingest gate runs on every paper before the curator sees it.
    # check_online: verify the mined accessions are actually retrievable from
    # INSDC. A well-formed accession can still be one the authors reserved and
    # never released, which no offline check can detect. Degrades to a warning if
    # the network is unavailable; set MALAVI_GATE_OFFLINE=1 to skip it.
    from .gate import apply_gate
    apply_gate(submission, check_online=True)
    if run_malavir:
        from .validate import validate_submission
        validate_submission(submission, version=version)

    report = render_report([submission], headings=[primary.stem])

    # --- Bundle: write outputs, MOVE inputs into processed/<slug>_<ts>/ --------
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = processed / f"{slug}_{ts}"
    (bundle / "inputs").mkdir(parents=True, exist_ok=True)

    (bundle / "report.md").write_text(report + "\n")
    (bundle / "submission.json").write_text(json.dumps(submission, indent=2) + "\n")
    manifest = {
        "slug": slug,
        "processed_at": ts,
        "reference_title": reference["title"],
        "inputs": {"pdfs": [p.name for p in pdfs],
                   "tables": [t.name for t in tables],
                   "other": [o.name for o in other]},
        "tables_parsed": table_reports,
        # Verdict counts from sequence-based lineage resolution. "resolved" rows
        # carry a MalAvi name derived from the sequence rather than read off the
        # page; "novel" rows may be new lineages needing a name.
        "lineage_resolution": resolution_tally,
        # How many host/vector names needed translating into MalAvi's namespace,
        # by rule (see host_names.RULES).
        "host_names": host_name_tally,
        "vector_names": vector_name_tally,
        # Which rows are this paper's own data and which it reprinted, plus the
        # sentences the paper used to say so. A "reprinted" count above zero
        # means the curator must not file those rows under this reference.
        "source_scope": {"rows": scope_tally, **scope.as_dict()},
        # How many rows landed in each triage tier, and which flags fired. The
        # "review" count is the size of the curator's actual queue; "confirms"
        # counts rows read correctly that MalAvi already holds under an earlier
        # reference, which are not errors. See row_flags.
        "triage": triage,
        "n_accessions": len(submission["accessions"]),
        "n_records": len(submission["records"]),
        "n_vectors": len(submission.get("vectors", [])),
        "gate_passed": submission.get("gate", {}).get("passed"),
        "gate_n_error": submission.get("gate", {}).get("n_error"),
        "gate_n_warn": submission.get("gate", {}).get("n_warn"),
        "malavir_validated": run_malavir,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    for f in files:
        shutil.move(str(f), str(bundle / "inputs" / f.name))

    return bundle


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Process one paper from the intake folder.")
    ap.add_argument("--processing", type=Path, help="override processing/ dir")
    ap.add_argument("--processed", type=Path, help="override processed/ dir")
    ap.add_argument("--validate", action="store_true",
                    help="also run the malaviR validation layer (needs Rscript + malaviR)")
    args = ap.parse_args(argv)

    processing, processed = _intake_dirs(args.processing, args.processed)
    if not processing.is_dir():
        print(f"No processing directory: {processing}")
        return 1

    bundle = process_batch(processing, processed, run_malavir=args.validate)
    if bundle is None:
        print(f"Nothing to process — {processing} is empty. Drop a paper's PDF + "
              "supplementary files there first.")
        return 0
    print(f"Processed -> {bundle}")
    print(f"  report:     {bundle / 'report.md'}")
    print(f"  submission: {bundle / 'submission.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
