#!/usr/bin/env python3
# @title Check the lineage-history table's accessions against GenBank
# @purpose For every lineage_history row whose SOURCE is a GenBank accession, fetch the
#          record and compare it with the sequence the store holds for that lineage
#          (NEW_SEQUENCE), reporting comparable positions and mismatches.
# @why A history row saying "sequence updated from KJ488658" is a public claim; this is
#      how it was verified before the seed shipped (2026-09-25: 59 of 60 exact; the
#      60th, MILVUS01, was a note on the wrong row and was excluded).
# @input data/records/lineage_history.csv
# @input data/records/lineages.csv
# @input NCBI nucleotide (Entrez efetch, FASTA)
# @output stdout
# @program python3
# @program biopython
# @critical-var MIN_COMPARABLE
"""Usage: python3 curation/audit_lineage_history_genbank.py [--email you@example.org]

Runs with the system python3 (biopython), like the watcher; the curation .venv has no
biopython. Needs outbound internet (compute nodes have it). Nothing is written.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path

from Bio import Align, Entrez, SeqIO

ROOT = Path(__file__).resolve().parents[1]
ACCESSION = re.compile(r"^[A-Z]{1,2}\d{5,6}(\.\d)?$")
MIN_COMPARABLE = 300      # fewer aligned A/C/G/T positions than this is not a verdict


def compare(stored: str, genbank: str, aligner: Align.PairwiseAligner):
    aln = aligner.align(stored, genbank)[0]
    comparable = mismatches = 0
    for (s0, s1), (g0, g1) in zip(*aln.aligned):
        for k in range(s1 - s0):
            a, b = stored[s0 + k], genbank[g0 + k]
            if a in "ACGT" and b in "ACGT":
                comparable += 1
                mismatches += a != b
    return comparable, mismatches


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="vaellis@udel.edu", help="Identifies the caller to NCBI.")
    args = parser.parse_args(argv)
    Entrez.email = args.email
    Entrez.tool = "malavi_rebuild_history_check"

    with open(ROOT / "data" / "records" / "lineage_history.csv", encoding="utf-8", newline="") as h:
        rows = [r for r in csv.DictReader(h) if ACCESSION.match(r["SOURCE"].strip())]
    with open(ROOT / "data" / "records" / "lineages.csv", encoding="utf-8", newline="") as h:
        store = {r["LINEAGE_NAME"]: r for r in csv.DictReader(h)}
    accessions = sorted({r["SOURCE"].strip() for r in rows})
    print(f"{len(rows)} history rows name an accession; {len(accessions)} distinct")

    records = {}
    for i in range(0, len(accessions), 50):
        chunk = accessions[i:i + 50]
        handle = Entrez.efetch(db="nucleotide", id=",".join(chunk), rettype="fasta", retmode="text")
        for rec in SeqIO.parse(handle, "fasta"):
            records[rec.id.split(".")[0]] = str(rec.seq).upper()
        time.sleep(0.4)
    print(f"fetched {len(records)} records from GenBank")

    aligner = Align.PairwiseAligner(mode="local", match_score=1, mismatch_score=-1,
                                    open_gap_score=-2, extend_gap_score=-1)
    problems = []
    for r in rows:
        name, acc = r["LINEAGE_NAME"], r["SOURCE"].strip()
        stored = (store.get(name, {}).get("SEQUENCE") or "").upper().replace("-", "")
        genbank = records.get(acc.split(".")[0])
        if genbank is None:
            problems.append(f"{name} | {acc} | not found at GenBank"); continue
        if r["EVENT"] == "sequence_updated" and stored != r["NEW_SEQUENCE"].upper().replace("-", ""):
            problems.append(f"{name} | {acc} | the store's sequence is not the row's NEW_SEQUENCE")
        comparable, mismatches = compare(stored, genbank, aligner)
        verdict = ("too little overlap" if comparable < MIN_COMPARABLE else
                   "identical" if mismatches == 0 else f"{mismatches} mismatches")
        print(f"  {name:12s} {acc:12s} {comparable:4d} compared  {verdict}")
        if verdict != "identical":
            problems.append(f"{name} | {acc} | {comparable} compared, {verdict}")
    print(f"\n{len(rows) - len(problems)} rows agree with GenBank; {len(problems)} need a look")
    for p in problems:
        print("  ", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
