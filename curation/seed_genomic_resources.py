#!/usr/bin/env python3
# @title Seed the genomic_resources table from the malaviTree mitochondrial backbone
# @purpose Write the first genomic_resources rows: one per avian haemosporidian
#          mitochondrial genome in the malaviTree backbone whose cytochrome b barcode is
#          identical, over the whole 479 bp window, to a lineage MalAvi already names.
#          Everything that does not match exactly is written to a side table for the
#          curators and NOT to the store. Dry run by default.
# @why Curator meeting 2026-09-24, point 9: a table of mitochondrial genomes linked to
#      cyt b lineages. The link is taken from the sequence itself, never from a label:
#      a genome filed under the wrong lineage would mislead every phylogenetic use.
# @input /mnt/ellisbiostore/malaviTree/data/raw/backbone_mtdna_2026-06-15/backbone_avian.fasta
# @input /mnt/ellisbiostore/malaviTree/data/raw/backbone_mtdna_2026-06-15/backbone_metadata.tsv
# @input /mnt/ellisbiostore/malaviTree/refs/backbone_mtdna_pacheco2018_S1.tsv
# @input /mnt/ellisbiostore/malaviTree/refs/backbone_mtdna_post2018.tsv
# @input data/records/lineages.csv (the reference the barcode is called against)
# @output data/records/genomic_resources.csv (with --apply)
# @output results/GENOMIC_RESOURCES_seed_<date>.tsv (every candidate and its call)
# @program python3
# @critical-var FULL_GENOME_MIN_BP
# @critical-var MIN_COMPARABLE
# @critical-flag seed_genomic_resources.py "" --apply
"""Usage: .venv/bin/python curation/seed_genomic_resources.py [--tree DIR] [--apply]

Refuses to run over a genomic_resources table that already has rows: this is the seed,
not a sync. Every row it writes carries ``_source = seed``; ``_added`` is left for the
release build to stamp.

**The rule for a row.** The record's cytochrome b barcode, located anywhere in the
record on either strand by the same code the submission screen uses, must be identical
to one MalAvi lineage over at least MIN_COMPARABLE of the 479 positions, with no
mismatch. A record whose barcode is one base from a lineage is a candidate for a *new*
lineage or for a sequence correction, which is a curator's call, so it goes to the side
table and not to the store. The call is deterministic: same inputs, same rows.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation import normalize, reference_names, sequence_check       # noqa: E402
from malavi_curation.config import repo_root                                 # noqa: E402
from malavi_curation.release_store import (                                  # noqa: E402
    SEED, TABLES, assign_ids, read_high_water, read_store, store_dir, write_store,
)

# Where the backbone lives and which fetch it is. The date is part of the provenance
# written into SOURCE, so a re-fetch is a different seed.
DEFAULT_TREE = Path("/mnt/ellisbiostore/malaviTree")
BACKBONE_FETCH = "backbone_mtdna_2026-06-15"

# A haemosporidian mitochondrial genome is about 6 kb (cox3 + cox1 + cytb and the
# fragmented rRNAs). Shorter records in the backbone are the coding region only, which
# the table calls a partial mitogenome.
FULL_GENOME_MIN_BP = 5500

# Positions of the 479 bp window a record must determine for an exact match to count.
# The template asks submitters for 470; the same floor applies here.
MIN_COMPARABLE = 470

# The one DOI the accessions README states; nothing else is guessed.
DOI = {"Pacheco et al 2018": "10.1093/molbev/msx285"}


def read_tsv(path: Path) -> List[Dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def read_fasta(path: Path) -> Dict[str, str]:
    """Accession (without version) -> sequence, from the backbone FASTA."""
    records: Dict[str, str] = {}
    current: Optional[str] = None
    parts: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(">"):
            if current:
                records[current] = "".join(parts)
            current = line[1:].split()[0].split(".")[0].upper()
            parts = []
        elif current:
            parts.append(line.strip())
    if current:
        records[current] = "".join(parts)
    return records


def paper_key(source_paper: str) -> str:
    """``Ciloglu_et_al_2020`` -> ``Ciloglu et al 2020``, MalAvi's citation-key form.
    An NCBI-scan marker is not a paper and gives ''."""
    if not source_paper or source_paper.startswith("NCBI_scan"):
        return ""
    text = source_paper.replace("_", " ")
    text = re.sub(r"\bet al\b", "et al.", text)
    return reference_names.published_form(text)


def host_from_s1(value: str) -> str:
    """Pacheco S1 writes the host as ``Gallus gallus domesticus/Chicken``; ``*`` is unknown."""
    value = (value or "").strip()
    if not value or value == "*":
        return ""
    return value.split("/")[0].strip()


def build_reference(store: Dict[str, List[Dict[str, str]]]) -> sequence_check.Reference:
    """Every lineage in the store, by its plain name, as the barcode reference."""
    names = [row["LINEAGE_NAME"] for row in store["lineages"] if row.get("SEQUENCE")]
    seqs = [row["SEQUENCE"].upper() for row in store["lineages"] if row.get("SEQUENCE")]
    return sequence_check.Reference.from_sequences(names, seqs, where="data/records/lineages.csv")


def call_barcode(accession: str, sequence: str, ref: sequence_check.Reference) -> Dict[str, Any]:
    """The screen's own call on the record's barcode, reduced to what the seed needs."""
    result = sequence_check.check_sequence(sequence, ref, label=accession)
    nearest, distance, comparable = (result.nearest[0] if result.nearest else ("", -1, 0))
    nearest = normalize.lineage_name(re.sub(r"^[PHLN]_", "", nearest)) or nearest
    exact = (result.verdict == "known_lineage" and distance == 0
             and comparable >= MIN_COMPARABLE and bool(nearest))
    return {"verdict": result.verdict, "nearest": nearest, "distance": distance,
            "comparable": comparable, "exact": exact,
            "window": result.window, "stops": result.n_stop_codons}


def candidates(tree: Path) -> List[Dict[str, Any]]:
    """Every avian backbone record with what the curated tables say about it."""
    raw = tree / "data" / "raw" / BACKBONE_FETCH
    sequences = read_fasta(raw / "backbone_avian.fasta")
    metadata = {r["accession"].split(".")[0].upper(): r
                for r in read_tsv(raw / "backbone_metadata.tsv")}
    s1 = {r["accession"].split(".")[0].upper(): r
          for r in read_tsv(tree / "refs" / "backbone_mtdna_pacheco2018_S1.tsv")}
    post = {r["accession"].split(".")[0].upper(): r
            for r in read_tsv(tree / "refs" / "backbone_mtdna_post2018.tsv")}
    out = []
    for accession in sorted(sequences):
        meta = metadata.get(accession, {})
        if s1.get(accession):
            row = s1[accession]
            reference = ("Pacheco et al 2018" if (row.get("new_in_pacheco2018") or "").strip().lower() == "yes"
                         else "")
            note = ("" if reference else
                    "listed in Pacheco et al 2018 table S1 as previously published; the original study is not recorded here")
            source_list = "Pacheco et al 2018 table S1"
            host = host_from_s1(row.get("vertebrate_host", ""))
        elif post.get(accession):
            reference = paper_key(post[accession].get("source_paper", ""))
            note = ""
            source_list = "post-2018 curated list"
            host = ""
        else:
            reference = ""
            note = ""
            source_list = "NCBI scan 2026-06-15"
            host = ""
        out.append({
            "accession": accession,
            "sequence": sequences[accession],
            "length": len(sequences[accession]),
            "organism": (meta.get("organism") or "").strip(),
            "definition": (meta.get("definition") or "").strip(),
            "host": host,
            "reference": reference,
            "doi": DOI.get(reference, ""),
            "source_list": source_list,
            "note": note,
        })
    return out


def make_row(candidate: Dict[str, Any], call: Dict[str, Any], columns) -> Dict[str, Any]:
    row = {column: "" for column in columns}
    notes = [f"cyt b barcode inside the record is {call['nearest']} over "
             f"{call['comparable']} of 479 positions, no mismatch (seed check)"]
    if candidate["note"]:
        notes.append(candidate["note"])
    row.update({
        "LINEAGE_NAME": call["nearest"],
        "RESOURCE_TYPE": "mitogenome" if candidate["length"] >= FULL_GENOME_MIN_BP else "mitogenome_partial",
        "ACCESSION": candidate["accession"],
        "DATABASE": "GenBank",
        "LENGTH_BP": str(candidate["length"]),
        "DESCRIPTION": candidate["definition"] or candidate["organism"],
        "HOST_SPECIES": candidate["host"],
        "REFERENCE_NAME": candidate["reference"],
        "DOI": candidate["doi"],
        "SOURCE": f"seed: malaviTree {BACKBONE_FETCH}, {candidate['source_list']}",
        "NOTES": "; ".join(notes),
        "_source": SEED,
        "_added": "",
    })
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, default=DEFAULT_TREE,
                        help="the malaviTree project directory")
    parser.add_argument("--apply", action="store_true",
                        help="write the rows to data/records/genomic_resources.csv")
    parser.add_argument("--repo", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = args.repo or repo_root()
    store = read_store(store_dir(root))
    if store.get("genomic_resources"):
        print(f"genomic_resources already holds {len(store['genomic_resources'])} row(s); "
              f"this is the seed, not a sync. Nothing done.", file=sys.stderr)
        return 1
    if not store.get("lineages"):
        print("the record store has no lineages to call barcodes against.", file=sys.stderr)
        return 1

    print("== malavi_rebuild :: seed genomic_resources ==")
    ref = build_reference(store)
    print(f"reference: {len(ref.names)} lineages x {ref.width} bp, from the record store")
    pool = candidates(args.tree)
    print(f"backbone: {len(pool)} avian record(s) from {args.tree / 'data/raw' / BACKBONE_FETCH}")

    rows: List[Dict[str, Any]] = []
    side: List[Dict[str, Any]] = []
    for candidate in pool:
        call = call_barcode(candidate["accession"], candidate["sequence"], ref)
        side.append({
            "accession": candidate["accession"], "length_bp": candidate["length"],
            "organism": candidate["organism"], "verdict": call["verdict"],
            "nearest_lineage": call["nearest"], "distance": call["distance"],
            "comparable": call["comparable"], "stop_codons": call["stops"],
            "seeded": "yes" if call["exact"] else "no",
            "reference": candidate["reference"], "source_list": candidate["source_list"],
        })
        if call["exact"]:
            rows.append(make_row(candidate, call, TABLES["genomic_resources"].columns))

    # One lineage can have several genomes (replicate clones, several isolates): fine,
    # the key is lineage + type + accession. Report how many lineages are covered.
    lineages = sorted({row["LINEAGE_NAME"] for row in rows})
    by_verdict: Dict[str, int] = {}
    for entry in side:
        by_verdict[entry["verdict"]] = by_verdict.get(entry["verdict"], 0) + 1
    print(f"\n{len(rows)} record(s) match a MalAvi lineage exactly, covering {len(lineages)} lineage(s); "
          f"{len(side) - len(rows)} do not and are left for the curators.")
    print("calls: " + ", ".join(f"{k} {v}" for k, v in sorted(by_verdict.items())))

    side_path = root / "results" / f"GENOMIC_RESOURCES_seed_{date.today().isoformat()}.tsv"
    side_path.parent.mkdir(parents=True, exist_ok=True)
    with side_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(side[0].keys()) if side else ["accession"],
                                delimiter="\t")
        writer.writeheader()
        writer.writerows(side)
    print(f"every candidate and its call: {side_path}")

    if not args.apply:
        print("\n[dry-run] nothing was written to the store. Re-run with --apply.")
        return 0
    store["genomic_resources"] = assign_ids(
        TABLES["genomic_resources"], rows,
        floor=read_high_water(store_dir(root)).get("genomic_resources", 0))
    write_store(store_dir(root), store)
    print(f"\nwrote {len(rows)} row(s) to {store_dir(root) / 'genomic_resources.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
