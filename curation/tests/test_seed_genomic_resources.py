"""The genomic_resources seed (curation/seed_genomic_resources.py).

A record joins the table only when its barcode, found anywhere in the record on either
strand, is identical to one stored lineage over at least MIN_COMPARABLE positions. One
base off is a curator's question, not a row. Dry run writes only the side table.
"""
from __future__ import annotations

import csv
import importlib.util
import random
import sys
from pathlib import Path

import pytest

from malavi_curation.config import repo_root
from malavi_curation.release_store import TABLES, PROVENANCE_COLUMNS, read_table, store_dir

WIDTH = 479


@pytest.fixture(scope="module")
def seeder():
    path = repo_root() / "curation" / "seed_genomic_resources.py"
    spec = importlib.util.spec_from_file_location("_seed_genomic_resources", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_seed_genomic_resources"] = module
    spec.loader.exec_module(module)
    return module


def _random_seq(seed: int, n: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice("ACGT") for _ in range(n))


def _revcomp(s: str) -> str:
    return s.translate(str.maketrans("ACGT", "TGCA"))[::-1]


def _tsv(path: Path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def world(tmp_path, seeder):
    """A repo with two lineages and a backbone with four records."""
    repo = tmp_path / "repo"
    records = store_dir(repo)
    records.mkdir(parents=True)
    # Two related lineages, as real ones are: the second is the first with 20 changes.
    # (Two unrelated random strings give a reference whose consensus is noise, and the
    # locator, which judges placement against that consensus, then places nothing.)
    lin1 = _random_seq(1, WIDTH)
    rng = random.Random(2)
    lin2 = list(lin1)
    for pos in rng.sample(range(WIDTH), 20):
        lin2[pos] = rng.choice([b for b in "ACGT" if b != lin1[pos]])
    lin2 = "".join(lin2)
    columns = list(TABLES["lineages"].columns) + [c for c in PROVENANCE_COLUMNS
                                                 if c not in TABLES["lineages"].columns]
    with (records / "lineages.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for i, (name, seq) in enumerate((("EXAMPL01", lin1), ("EXAMPL02", lin2)), 1):
            writer.writerow({**{c: "" for c in columns}, "LINEAGE_NAME": name,
                             "SEQUENCE": seq, "GENUS_NAME": "Plasmodium",
                             "RECORD_ID": f"LIN{i:06d}", "_source": "seed", "_added": "2026-09-15"})

    tree = tmp_path / "tree"
    raw = tree / "data" / "raw" / seeder.BACKBONE_FETCH
    raw.mkdir(parents=True)
    (tree / "refs").mkdir()
    pad = _random_seq(9, 3000)
    one_off = lin2[:100] + ("A" if lin2[100] != "A" else "C") + lin2[101:]
    fasta = {
        "AA000001.1": pad + lin1 + pad,               # exact, forward, full length
        "AA000002.1": _revcomp(pad + lin1 + pad[:1000]),  # exact, reverse strand, short
        "AA000003.1": pad + one_off + pad,            # one base off: not seeded
        "AA000004.1": pad[:600],                      # no barcode at all
    }
    (raw / "backbone_avian.fasta").write_text(
        "".join(f">{k} test record\n{v}\n" for k, v in fasta.items()), encoding="utf-8")
    _tsv(raw / "backbone_metadata.tsv",
         [{"accession": k.split(".")[0], "length": len(v), "host_class": "avian",
           "organism": "Plasmodium testii", "definition": f"Plasmodium testii isolate {k} mitochondrion"}
          for k, v in fasta.items()],
         ["accession", "length", "host_class", "organism", "definition"])
    s1_fields = ["accession", "parasite_genus", "parasite_species", "vertebrate_host", "sample_type",
                 "country", "clone_isolate_strain", "mtdna_size_bp", "new_in_pacheco2018", "data_type", "source"]
    _tsv(tree / "refs" / "backbone_mtdna_pacheco2018_S1.tsv",
         [{**{f: "*" for f in s1_fields}, "accession": "AA000001", "vertebrate_host": "Gallus gallus/Chicken",
           "new_in_pacheco2018": "yes"},
          {**{f: "*" for f in s1_fields}, "accession": "AA000002", "vertebrate_host": "*",
           "new_in_pacheco2018": "no"}], s1_fields)
    _tsv(tree / "refs" / "backbone_mtdna_post2018.tsv",
         [{"accession": "AA000003", "source_paper": "Ciloglu_et_al_2020",
           "parasite_focus": "Plasmodium", "data_type": "mtDNA_genome_region"}],
         ["accession", "source_paper", "parasite_focus", "data_type"])
    return repo, tree


def test_dry_run_writes_the_side_table_and_nothing_else(world, seeder, capsys):
    repo, tree = world
    assert seeder.main(["--tree", str(tree), "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "2 record(s) match a MalAvi lineage exactly, covering 1 lineage(s); 2 do not" in out
    assert not (store_dir(repo) / "genomic_resources.csv").exists()
    side = list((repo / "results").glob("GENOMIC_RESOURCES_seed_*.tsv"))
    assert len(side) == 1
    rows = list(csv.DictReader(side[0].open(encoding="utf-8"), delimiter="\t"))
    assert {r["accession"]: r["seeded"] for r in rows} == {
        "AA000001": "yes", "AA000002": "yes", "AA000003": "no", "AA000004": "no"}
    assert [r["nearest_lineage"] for r in rows if r["accession"] == "AA000003"] == ["EXAMPL02"]


def test_apply_writes_only_exact_matches_with_provenance(world, seeder):
    repo, tree = world
    assert seeder.main(["--tree", str(tree), "--repo", str(repo), "--apply"]) == 0
    rows = read_table(store_dir(repo), TABLES["genomic_resources"])
    assert [r["ACCESSION"] for r in rows] == ["AA000001", "AA000002"]
    assert {r["LINEAGE_NAME"] for r in rows} == {"EXAMPL01"}
    full, short = rows
    assert full["RESOURCE_TYPE"] == "mitogenome" and short["RESOURCE_TYPE"] == "mitogenome_partial"
    assert full["HOST_SPECIES"] == "Gallus gallus" and short["HOST_SPECIES"] == ""
    assert full["REFERENCE_NAME"] == "Pacheco et al 2018" and full["DOI"] == "10.1093/molbev/msx285"
    assert short["REFERENCE_NAME"] == "" and "previously published" in short["NOTES"]
    assert all(r["_source"] == "seed" and r["RECORD_ID"].startswith("GEN") for r in rows)
    assert all(r["DATABASE"] == "GenBank" for r in rows)
    # The seed refuses to run twice.
    assert seeder.main(["--tree", str(tree), "--repo", str(repo), "--apply"]) == 1


def test_paper_keys_take_malavi_s_citation_form(seeder):
    assert seeder.paper_key("Ciloglu_et_al_2020") == "Ciloglu et al 2020"
    assert seeder.paper_key("NCBI_scan_2026-06-15") == ""
    assert seeder.host_from_s1("Gallus gallus domesticus/Chicken") == "Gallus gallus domesticus"
    assert seeder.host_from_s1("*") == ""
