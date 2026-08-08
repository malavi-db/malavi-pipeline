#!/usr/bin/env python3
# @title Backward lineage gap-finder (GenBank cytb vs MalAvi)
# @purpose Find haemosporidian cytochrome-b sequences in GenBank that match NO
#   existing MalAvi lineage (>=1 bp different) -- i.e. lineages/papers that slipped
#   past MalAvi curation -- and map those accessions to their source publications.
# @why Keyword literature scans cannot tell "not in MalAvi because it has no new
#   lineage" from "a genuinely missed lineage". Sequence novelty can: a cytb
#   sequence with no 100%-identity MalAvi hit is a lineage MalAvi does not have.
# @input MalAvi lineage BLAST db (built from extract_malavi_lineages.R + makeblastdb)
# @input NCBI nucleotide (Entrez esearch/efetch) haemosporidian cytb records
# @output TSV of novel-candidate accessions + their publications
# @program python
# @program blastn
# @program Bio.Entrez
# @critical-var GENERA
# @critical-var KNOWN_MIN_IDENTITY
# @critical-var KNOWN_MIN_ALN_LEN
# @critical-flag blastn -perc_identity
"""Backward lineage gap-finder.

Pipeline: esearch GenBank for haemosporidian cytb records in a date window ->
efetch FASTA -> blastn against the MalAvi lineage db -> classify each query as
KNOWN (has a 100%-identity MalAvi hit over a real overlap) or NOVEL (best hit is
<100%, so it differs from every MalAvi lineage) -> for novel accessions, pull the
GenBank reference (title/journal/pubmed) so the source paper can be curated.

Fetched FASTA and GenBank records are cached so re-runs do not re-download.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from Bio import Entrez, SeqIO

# The three avian haemosporidian genera MalAvi curates. Mammalian Plasmodium is
# excluded by species below so human/rodent malaria cytb does not flood the search.
GENERA = ["Haemoproteus", "Plasmodium", "Leucocytozoon"]

# A query is "already in MalAvi" if it has a BLAST hit at essentially 100% identity
# over a real overlap. MalAvi's lineage definition is exact match in the barcode
# window, so anything below 100% differs from every known lineage (a novel lineage
# or a sequencing error -- the curator decides which).
KNOWN_MIN_IDENTITY = 100.0   # percent identity that counts a query as a known lineage
KNOWN_MIN_ALN_LEN = 200      # ...over at least this many aligned bp (guards tiny hits)

# Mammalian/reptilian Plasmodium species to exclude from the Plasmodium search so
# the candidate set stays avian-relevant (mirrors the watcher query's NOT clause).
NON_AVIAN_PLASMODIUM = [
    "falciparum", "vivax", "ovale", "malariae", "knowlesi", "berghei",
    "chabaudi", "yoelii", "cynomolgi", "gallinaceum", "reichenowi",
]


def esearch_ids(term: str, mindate: str, maxdate: str, retmax: int) -> List[str]:
    """Return up to ``retmax`` GenBank nucleotide IDs matching ``term`` in a window."""
    handle = Entrez.esearch(
        db="nucleotide", term=term, datetype="pdat",
        mindate=mindate, maxdate=maxdate, retmax=str(retmax),
    )
    rec = Entrez.read(handle)
    handle.close()
    return rec["IdList"]


def _id_digest(ids: List[str]) -> str:
    """Short stable hash of an ID set, used to key content-addressed caches so a
    changed query (e.g. a larger --max-records) never reuses a stale, smaller
    download keyed only on a fixed filename."""
    return hashlib.sha1("\n".join(sorted(ids)).encode()).hexdigest()[:12]


def efetch_fasta(ids: List[str], cache: Path, batch: int = 200) -> Path:
    """Fetch FASTA for ``ids`` (batched, cached) and return the FASTA path.

    The cache is keyed on the exact ID set (hash in the filename): an identical
    re-run reuses the download, but a different/larger ID set fetches fresh.
    """
    cache = cache.with_name(f"{cache.stem}_{_id_digest(ids)}{cache.suffix}")
    if cache.is_file():
        print(f"  using cached FASTA: {cache}", file=sys.stderr)
        return cache
    with cache.open("w") as out:
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            print(f"  efetch FASTA {i + 1}-{i + len(chunk)} of {len(ids)}", file=sys.stderr)
            h = Entrez.efetch(db="nucleotide", id=",".join(chunk),
                              rettype="fasta", retmode="text")
            out.write(h.read())
            h.close()
            time.sleep(0.34)  # NCBI: <=3 requests/second without an API key
    return cache


def run_blast(query_fasta: Path, db: Path, min_identity: float = 90.0) -> Dict[str, dict]:
    """blastn ``query_fasta`` vs ``db``; return best MalAvi hit per query accession.

    Only hits at >= ``min_identity`` are considered (a query with no such hit is
    treated as having no MalAvi match). The best hit is the one with the highest
    identity, then longest alignment.
    """
    cmd = [
        "blastn", "-query", str(query_fasta), "-db", str(db),
        "-perc_identity", str(min_identity), "-max_target_seqs", "5",
        "-outfmt", "6 qseqid sacc pident length qlen",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    best: Dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        qseqid, sacc, pident, length, qlen = line.split("\t")
        pident, length, qlen = float(pident), int(length), int(qlen)
        cur = best.get(qseqid)
        cand = {"lineage": sacc, "pident": pident, "length": length, "qlen": qlen}
        if cur is None or (pident, length) > (cur["pident"], cur["length"]):
            best[qseqid] = cand
    return best


def genbank_reference(ids: List[str], cache_dir: Path, batch: int = 150) -> Dict[str, dict]:
    """For each accession, pull the primary GenBank reference (title/journal/pubmed)."""
    info: Dict[str, dict] = {}
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        # Key the chunk cache on its contents, not its position, so a different
        # novel-accession set does not reuse a stale gb_<index> file.
        cache = cache_dir / f"gb_{_id_digest(chunk)}.gb"
        if not cache.is_file():
            print(f"  efetch GenBank {i + 1}-{i + len(chunk)} of {len(ids)}", file=sys.stderr)
            h = Entrez.efetch(db="nucleotide", id=",".join(chunk),
                              rettype="gb", retmode="text")
            cache.write_text(h.read())
            h.close()
            time.sleep(0.34)
        for rec in SeqIO.parse(str(cache), "genbank"):
            acc = rec.id.split(".")[0]
            refs = rec.annotations.get("references", [])
            # Prefer a reference that is an actual article (has a title), not "Direct Submission".
            ref = next((r for r in refs if r.title and r.title != "Direct Submission"), None)
            ref = ref or (refs[0] if refs else None)
            info[acc] = {
                "title": getattr(ref, "title", "") if ref else "",
                "journal": getattr(ref, "journal", "") if ref else "",
                "pubmed": getattr(ref, "pubmed_id", "") if ref else "",
                "organism": rec.annotations.get("organism", ""),
            }
    return info


def build_term(genus: str) -> str:
    """Entrez term for one genus: cytb, sensible length, mammalian Plasmodium removed."""
    term = (f'{genus}[Organism] AND (cytochrome b[Title] OR cytb[Title] OR cob[Gene] '
            f'OR "cytochrome b"[Gene]) AND 300:1500[SLEN]')
    if genus == "Plasmodium":
        term += "".join(f' NOT {sp}[Organism]' for sp in NON_AVIAN_PLASMODIUM)
    return term


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="MalAvi lineage BLAST db prefix")
    ap.add_argument("--genera", nargs="+", default=GENERA, choices=GENERA)
    ap.add_argument("--mindate", default="2024/01/01", help="pub-date lower bound (YYYY/MM/DD)")
    ap.add_argument("--maxdate", default="2026/12/31", help="pub-date upper bound (YYYY/MM/DD)")
    ap.add_argument("--max-records", type=int, default=400, help="cap per genus (prototype safety)")
    ap.add_argument("--cache-dir", required=True, help="dir for fetched FASTA/GenBank")
    ap.add_argument("--out", required=True, help="TSV of novel-candidate accessions + papers")
    ap.add_argument("--email", default="vaellis@udel.edu")
    args = ap.parse_args(argv)

    Entrez.email = args.email
    Entrez.tool = "malavi-rebuild-lineage-gap-finder"
    cache_dir = Path(args.cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. Collect candidate accessions per genus -----------------------------
    all_ids: List[str] = []
    for genus in args.genera:
        term = build_term(genus)
        ids = esearch_ids(term, args.mindate.replace("-", "/"),
                          args.maxdate.replace("-", "/"), args.max_records)
        print(f"{genus}: {len(ids)} GenBank cytb candidates", file=sys.stderr)
        all_ids.extend(ids)
    all_ids = list(dict.fromkeys(all_ids))  # de-dup, keep order
    if not all_ids:
        print("No candidate sequences found for the window.", file=sys.stderr)
        return 0

    # --- 2. Fetch FASTA + BLAST vs MalAvi --------------------------------------
    fasta = efetch_fasta(all_ids, cache_dir / "candidates.fasta")
    q_by_acc = {r.id.split(".")[0]: r for r in SeqIO.parse(str(fasta), "fasta")}
    best = run_blast(fasta, Path(args.db))

    # --- 3. Classify KNOWN vs NOVEL --------------------------------------------
    novel: List[str] = []
    known = no_match = 0
    for acc, rec in q_by_acc.items():
        # BLAST qseqid is the full FASTA id; match on the accession prefix.
        hit = best.get(rec.id) or best.get(acc)
        if hit is None:
            no_match += 1
            novel.append(acc)  # no MalAvi hit at all -> divergent/novel (review)
        elif hit["pident"] >= KNOWN_MIN_IDENTITY and hit["length"] >= KNOWN_MIN_ALN_LEN:
            known += 1
        else:
            novel.append(acc)

    print(f"\nCandidates: {len(q_by_acc)} | in MalAvi (100% hit): {known} | "
          f"NOVEL (no 100% hit): {len(novel)} (of which {no_match} had no MalAvi hit at all)",
          file=sys.stderr)

    # --- 4. Map novel accessions to their publications -------------------------
    refs = genbank_reference(novel, cache_dir) if novel else {}

    # Group novel accessions by source publication (title) for a paper-level worklist.
    by_paper: Dict[str, List[str]] = defaultdict(list)
    for acc in novel:
        title = refs.get(acc, {}).get("title", "") or "(no reference / unpublished)"
        by_paper[title].append(acc)

    with open(args.out, "w") as out:
        out.write("accession\torganism\tbest_malavi_lineage\tbest_pident\taln_len\tpubmed\tjournal\ttitle\n")
        for acc in novel:
            hit = best.get(q_by_acc[acc].id) or best.get(acc) or {}
            r = refs.get(acc, {})
            out.write("\t".join(str(x) for x in [
                acc, r.get("organism", ""), hit.get("lineage", "-"),
                hit.get("pident", "-"), hit.get("length", "-"),
                r.get("pubmed", ""), r.get("journal", ""), r.get("title", ""),
            ]) + "\n")

    print(f"Wrote {len(novel)} novel-candidate accessions across ~{len(by_paper)} "
          f"publications to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
