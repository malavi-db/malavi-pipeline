#!/usr/bin/env python3
# @title Batch intake driver — one downloaded paper folder at a time
# @purpose Feed each per-DOI folder produced by fetch_oa_articles.py through the
#   single-paper intake (PDF + supplements bundled together), producing one
#   review bundle per paper under curation/intake/processed/.
# @why The intake processes ONE paper at a time; the fetcher already groups each
#   paper's files in its own folder, so this driver bridges the two without the
#   curator hand-staging files.
# @input <downloads>/<doi_slug>/ folders (article_pdf + supplement_NN files)
# @output curation/intake/processed/<slug>_<ts>/ bundles + a printed summary
# @program python3
# @program malavi_curation.intake
# @critical-var DOWNLOADS_DIR
"""Batch driver over per-DOI download folders.

For each folder it copies the paper's files into a temporary staging dir (so the
downloads are preserved for re-runs), renames the generic ``article_pdf`` to the
folder name so the bundle slug/title is DOI-traceable, and runs the intake.
Folders with no PDF are skipped (there is no text surface to mine) and reported.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from malavi_curation.intake import process_batch

PDF_SUFFIXES = {".pdf"}
SKIP_NAMES = {"download_report.csv"}


def paper_files(folder: Path):
    """Real input files in a per-DOI folder (ignore dotfiles and the report)."""
    return [f for f in sorted(folder.iterdir())
            if f.is_file() and not f.name.startswith(".")
            and f.name.lower() not in SKIP_NAMES]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--downloads", default="curation/intake/downloads",
                    help="dir of per-DOI folders from fetch_oa_articles.py")
    ap.add_argument("--processed", default="curation/intake/processed",
                    help="where review bundles are written")
    ap.add_argument("--validate", action="store_true",
                    help="also run the malaviR validation layer")
    args = ap.parse_args(argv)

    downloads = Path(args.downloads)
    processed = Path(args.processed)
    processed.mkdir(parents=True, exist_ok=True)
    if not downloads.is_dir():
        print(f"No downloads directory: {downloads}")
        return 1

    folders = sorted(p for p in downloads.iterdir() if p.is_dir())
    print(f"{len(folders)} paper folder(s) under {downloads}/\n")
    processed_n = skipped_n = 0

    for folder in folders:
        files = paper_files(folder)
        pdfs = [f for f in files if f.suffix.lower() in PDF_SUFFIXES]
        if not pdfs:
            print(f"  SKIP  {folder.name}  (no PDF; {len(files)} other file(s))")
            skipped_n += 1
            continue

        # Stage COPIES so the downloads survive; rename article_pdf -> <folder>.pdf
        # so the bundle slug/title is DOI-traceable rather than "article_pdf".
        stage = Path(tempfile.mkdtemp())
        try:
            for f in files:
                dest = stage / (f"{folder.name}.pdf"
                                if f.stem == "article_pdf" and f.suffix.lower() == ".pdf"
                                else f.name)
                shutil.copy2(f, dest)
            bundle = process_batch(stage, processed, run_malavir=args.validate)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        if bundle is None:
            print(f"  EMPTY {folder.name}")
            skipped_n += 1
            continue
        import json
        m = json.loads((bundle / "manifest.json").read_text())
        print(f"  OK    {folder.name}  -> {bundle.name}")
        print(f"        accessions={m['n_accessions']} records={m['n_records']} "
              f"tables={len(m['tables_parsed'])} gate="
              f"{'PASS' if m['gate_passed'] else 'FAIL'}"
              f"(warn {m['gate_n_warn']})")
        processed_n += 1

    print(f"\nDone. processed {processed_n}, skipped {skipped_n}. "
          f"Bundles in {processed}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
