"""The genomic_resources sync (curation/sync_genomic_resources.py).

Nothing here touches the network: the candidate collector is replaced by a stub, and the
GenBank flat files are fixtures fetched once from NCBI (curation/tests/fixtures/genomic/).
What is tested is the part that must not drift: how a flat file becomes a reference, a
DOI and a host; the type rule; that a dry run writes nothing to the store; and that a
barcode one base off a lineage is never proposed as a row.
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
FIXTURES = Path(__file__).parent / "fixtures" / "genomic"


@pytest.fixture(scope="module")
def sync():
    path = repo_root() / "curation" / "sync_genomic_resources.py"
    spec = importlib.util.spec_from_file_location("_sync_genomic_resources", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_sync_genomic_resources"] = module
    spec.loader.exec_module(module)
    return module


def _random_seq(seed: int, n: int) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice("ACGT") for _ in range(n))


def _revcomp(s: str) -> str:
    return s.translate(str.maketrans("ACGT", "TGCA"))[::-1]


# ---------------------------------------------------------------------------------------
# The GenBank flat file: reference, DOI, host
# ---------------------------------------------------------------------------------------

def test_flat_file_gives_reference_pubmed_host_and_sequence(sync):
    rec = sync.parse_genbank((FIXTURES / "KY653811.gb").read_text(encoding="utf-8"))[0]
    assert rec.accession == "KY653811" and rec.length == 5882 and len(rec.sequence) == 5882
    assert rec.organism == "Haemoproteus sacharovi" and rec.organelle == "mitochondrion"
    assert rec.host == "Zenaida macroura" and rec.isolate == "MAP03"
    # Two REFERENCE blocks: the paper (with a PubMed id) and the deposit itself.
    assert [r.is_published for r in rec.references] == [True, False]
    assert rec.published_reference.pubmed == "29126122"
    # Without PubMed, the name comes from the AUTHORS and the JOURNAL year as written.
    assert sync.study_from_genbank(rec, {}).reference_name == "Andreina Pacheco et al 2017"
    assert sync.study_from_genbank(rec, {}).doi == ""
    # With PubMed, the published record wins: its first author, year and DOI.
    pubmed = {"29126122": sync.Study("Pacheco et al 2018", "10.1093/molbev/msx285", "29126122", "PubMed 29126122")}
    study = sync.study_from_genbank(rec, pubmed)
    assert (study.reference_name, study.doi) == ("Pacheco et al 2018", "10.1093/molbev/msx285")


def test_unpublished_and_direct_submission_are_not_references(sync):
    rec = sync.parse_genbank((FIXTURES / "PV839585.gb").read_text(encoding="utf-8"))[0]
    assert rec.published_reference is None
    study = sync.study_from_genbank(rec, {})
    assert study.reference_name == "" and study.doi == ""
    # The record's own statements are still read.
    assert rec.host == "Astur gentilis" and rec.strain == "lACCGEN04"
    assert sync.stated_lineage([("/strain", rec.strain)], {"ACCGEN04"}) == \
        "record states: ACCGEN04 (/strain, behind the genus letter)"
    # Resemblance is not a statement.
    assert sync.stated_lineage([("/strain", "SGS1-like")], {"SGS1"}) == ""
    assert sync.stated_lineage([("/isolate", "AH2297")], {"ACCGEN04"}) == ""


def test_several_records_in_one_fetch_are_split(sync):
    text = "\n".join((FIXTURES / name).read_text(encoding="utf-8") for name in ("KY653811.gb", "PV839585.gb"))
    assert [r.accession for r in sync.parse_genbank(text)] == ["KY653811", "PV839585"]


def test_citation_keys_follow_malavi(sync):
    assert sync.citation_key(["Hellgren"], "2005") == "Hellgren 2005"
    assert sync.citation_key(["Baillie", "Brunton"], "2011") == "Baillie & Brunton 2011"
    assert sync.citation_key(["Pacheco", "Matta", "Valkiunas"], "2018") == "Pacheco et al 2018"
    assert sync.citation_key([], "2018") == "" and sync.citation_key(["Hellgren"], "") == ""
    assert sync._surnames_from_genbank("Harl,J., Himmel,T. and Weissenboeck,H.") == ["Harl", "Himmel", "Weissenboeck"]
    assert sync._surnames_from_genbank("Bensch,S. and Hellgren,O.") == ["Bensch", "Hellgren"]


# ---------------------------------------------------------------------------------------
# The type rule
# ---------------------------------------------------------------------------------------

def test_type_rule(sync):
    mito = sync.parse_genbank((FIXTURES / "KY653811.gb").read_text(encoding="utf-8"))[0]
    assert sync.resource_type_for(mito) == "mitogenome"
    short = sync.GbRecord(length=sync.FULL_GENOME_MIN_BP - 1, organelle="mitochondrion")
    assert sync.resource_type_for(short) == "mitogenome_partial"
    exact = sync.GbRecord(length=sync.FULL_GENOME_MIN_BP, organelle="mitochondrion")
    assert sync.resource_type_for(exact) == "mitogenome"
    api = sync.parse_genbank((FIXTURES / "MW250349.gb").read_text(encoding="utf-8"))[0]
    assert api.organelle == "plastid:apicoplast" and sync.resource_type_for(api) == "apicoplast_genome"
    tsa = sync.GbRecord(length=17131, keywords="TSA; Transcriptome Shotgun Assembly")
    assert sync.resource_type_for(tsa) == "transcriptome"


def test_avian_taxon_rule_is_a_name_list(sync):
    assert sync.is_avian_taxon("Plasmodium relictum")
    assert sync.is_avian_taxon("Haemoproteus majoris (apicomplexans)")
    assert not sync.is_avian_taxon("Plasmodium falciparum 3D7")
    assert not sync.is_avian_taxon("Hepatocystis sp. ex Piliocolobus tephrosceles")
    assert not sync.is_avian_taxon("")


# ---------------------------------------------------------------------------------------
# The store: dry run writes nothing; one base off is never a row
# ---------------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, sync):
    """A repo with two lineages and a filled genomic_resources table of one row."""
    repo = tmp_path / "repo"
    records = store_dir(repo)
    records.mkdir(parents=True)
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
            writer.writerow({**{c: "" for c in columns}, "LINEAGE_NAME": name, "SEQUENCE": seq,
                             "GENUS_NAME": "Plasmodium", "RECORD_ID": f"LIN-{i:06d}",
                             "_source": "seed", "_added": "2026-09-15"})
    gcolumns = list(TABLES["genomic_resources"].columns) + [c for c in PROVENANCE_COLUMNS
                                                           if c not in TABLES["genomic_resources"].columns]
    with (records / "genomic_resources.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=gcolumns)
        writer.writeheader()
        writer.writerow({**{c: "" for c in gcolumns}, "LINEAGE_NAME": "EXAMPL01", "RESOURCE_TYPE": "mitogenome",
                         "ACCESSION": "KY653811", "DATABASE": "GenBank", "LENGTH_BP": "5882",
                         "DESCRIPTION": "seeded", "RECORD_ID": "GEN-000001", "_source": "seed", "_added": ""})
    pad = _random_seq(9, 3000)
    one_off = lin2[:100] + ("A" if lin2[100] != "A" else "C") + lin2[101:]
    pool = [
        # exact, forward: the only road to a row
        sync.Candidate("AA000001", "mitogenome", "test", organism="Plasmodium testii",
                       length_bp=6479, description="exact", sequence=pad + lin1 + pad,
                       study=sync.Study("Tester et al 2026", "10.1000/test", "1", "PubMed 1")),
        # exact on the reverse strand inside a set of contigs (an assembly)
        sync.Candidate("GCA_000000001.1", "nuclear_genome", "test", organism="Plasmodium testii",
                       contigs={"c1": _random_seq(5, 20000), "c2": _revcomp(pad + lin2 + pad)}),
        # ONE base off lineage 2: a curator's question, never a row
        sync.Candidate("AA000003", "mitogenome", "test", organism="Plasmodium testii",
                       length_bp=6479, sequence=pad + one_off + pad),
        # no barcode, but the record states a lineage name in /strain
        sync.Candidate("AA000004", "apicoplast_genome", "test", organism="Plasmodium testii",
                       length_bp=600, sequence=pad[:600], strain="EXAMPL01"),
        # no barcode, nothing stated
        sync.Candidate("PRJNA000001", "transcriptome", "test", database="SRA/ENA", organism="Plasmodium testii"),
        # already in the table
        sync.Candidate("KY653811", "mitogenome", "test", organism="Haemoproteus sacharovi",
                       length_bp=5882, sequence=pad + lin1 + pad),
    ]
    return repo, pool


class _OfflineClient:
    """Refuses the network: a test that reaches it fails loudly."""
    cache_dir = Path("unused")
    requests_made = 0
    cache_hits = 0

    def fetch(self, url):
        raise AssertionError(f"network call in a test: {url}")

    def efetch(self, *a, **k):
        return ""

    def esummary(self, *a, **k):
        return []

    def esearch(self, *a, **k):
        return []


def _run(sync, monkeypatch, repo, pool, apply=False):
    monkeypatch.setattr(sync, "collect_candidates", lambda client, sources, log: list(pool))
    # The fill step fetches the existing rows' records: stubbed to the fixture.
    fixture = sync.parse_genbank((FIXTURES / "KY653811.gb").read_text(encoding="utf-8"))
    monkeypatch.setattr(sync, "fetch_nucleotide_records",
                        lambda client, accessions: {r.accession: r for r in fixture
                                                    if r.accession in {a.split(".")[0] for a in accessions}})
    monkeypatch.setattr(sync, "pubmed_studies", lambda client, pmids: {
        "29126122": sync.Study("Pacheco et al 2018", "10.1093/molbev/msx285", "29126122", "PubMed 29126122")})
    argv = ["--repo", str(repo), "--date", "2026-10-16"] + (["--apply"] if apply else [])
    return sync.main(argv, client=_OfflineClient())


def test_dry_run_writes_the_side_tables_and_never_the_store(world, sync, monkeypatch, capsys):
    repo, pool = world
    store_file = store_dir(repo) / "genomic_resources.csv"
    before = store_file.read_bytes()
    assert _run(sync, monkeypatch, repo, pool) == 0
    assert store_file.read_bytes() == before
    assert not (store_dir(repo) / "id_high_water.json").exists()
    out = capsys.readouterr().out
    assert "[dry-run] nothing was written to the store. 2 row(s) would be added" in out

    side = list(csv.DictReader((repo / "results" / "GENOMIC_RESOURCES_sync_2026-10-16.tsv").open(), delimiter="\t"))
    actions = {r["accession"]: r["proposed_action"] for r in side}
    assert actions == {"AA000001": "add", "GCA_000000001.1": "add", "AA000003": "curator call",
                       "AA000004": "curator call", "PRJNA000001": "paper needed", "KY653811": "already in table"}
    one_off = next(r for r in side if r["accession"] == "AA000003")
    assert one_off["nearest_lineage"] == "EXAMPL02" and one_off["distance"] == "1"
    stated = next(r for r in side if r["accession"] == "AA000004")
    assert stated["stated_lineage"] == "record states: EXAMPL01 (/strain)"
    assembly = next(r for r in side if r["accession"] == "GCA_000000001.1")
    assert assembly["nearest_lineage"] == "EXAMPL02" and assembly["distance"] == "0"

    fills = list(csv.DictReader((repo / "results" / "GENOMIC_RESOURCES_fill_2026-10-16.tsv").open(), delimiter="\t"))
    proposed = {(f["column"], f["action"]): f["new_value"] for f in fills if f["RECORD_ID"] == "GEN-000001"}
    assert proposed == {("REFERENCE_NAME", "fill"): "Pacheco et al 2018",
                        ("DOI", "fill"): "10.1093/molbev/msx285",
                        ("HOST_SPECIES", "fill"): "Zenaida macroura"}


def test_apply_adds_only_exact_matches_and_fills_only_empty_cells(world, sync, monkeypatch):
    repo, pool = world
    assert _run(sync, monkeypatch, repo, pool, apply=True) == 0
    rows = read_table(store_dir(repo), TABLES["genomic_resources"])
    # The store writer orders rows by their key, so look them up by accession.
    by_acc = {r["ACCESSION"]: r for r in rows}
    assert set(by_acc) == {"KY653811", "AA000001", "GCA_000000001.1"}
    seeded, exact, assembly = by_acc["KY653811"], by_acc["AA000001"], by_acc["GCA_000000001.1"]
    # The existing row kept its id and got its empty cells filled.
    assert seeded["RECORD_ID"] == "GEN-000001" and seeded["DESCRIPTION"] == "seeded"
    assert (seeded["REFERENCE_NAME"], seeded["DOI"], seeded["HOST_SPECIES"]) == \
        ("Pacheco et al 2018", "10.1093/molbev/msx285", "Zenaida macroura")
    assert exact["LINEAGE_NAME"] == "EXAMPL01" and exact["REFERENCE_NAME"] == "Tester et al 2026"
    assert exact["_source"] == "sync" and exact["RECORD_ID"].startswith("GEN-") and exact["_added"] == ""
    assert assembly["LINEAGE_NAME"] == "EXAMPL02" and assembly["RESOURCE_TYPE"] == "nuclear_genome"
    assert "contig c2" in assembly["NOTES"]
    # The one-base-off record is nowhere in the store.
    assert not any(r["ACCESSION"] == "AA000003" for r in rows)


def test_fill_never_overwrites_and_keeps_a_doi_to_its_own_study(sync):
    rec = sync.parse_genbank((FIXTURES / "KY653811.gb").read_text(encoding="utf-8"))[0]
    pubmed = {"29126122": sync.Study("Pacheco et al 2018", "10.1093/molbev/msx285", "29126122", "PubMed 29126122")}
    rows = [
        {"RECORD_ID": "GEN-000001", "ACCESSION": "KY653811", "LINEAGE_NAME": "X",
         "REFERENCE_NAME": "Pacheco et al 2018", "DOI": "", "HOST_SPECIES": "Columba livia"},
        {"RECORD_ID": "GEN-000002", "ACCESSION": "KY653811", "LINEAGE_NAME": "X",
         "REFERENCE_NAME": "Somebody et al 2020", "DOI": "", "HOST_SPECIES": ""},
    ]
    fills = {(f["RECORD_ID"], f["column"]): f["action"] for f in sync.proposed_fills(rows, {"KY653811": rec}, pubmed)}
    assert fills[("GEN-000001", "REFERENCE_NAME")] == "same"
    assert fills[("GEN-000001", "DOI")] == "fill"
    assert fills[("GEN-000001", "HOST_SPECIES")] == "conflict (kept)"
    # A different study on the row: the record's DOI is not that study's DOI.
    assert fills[("GEN-000002", "REFERENCE_NAME")] == "conflict (kept)"
    assert fills[("GEN-000002", "DOI")].startswith("none: record cites")
    assert fills[("GEN-000002", "HOST_SPECIES")] == "fill"
    before = [dict(r) for r in rows]
    assert sync.apply_fills(rows, sync.proposed_fills(rows, {"KY653811": rec}, pubmed)) == 2
    assert rows[0]["HOST_SPECIES"] == "Columba livia" and rows[0]["DOI"] == "10.1093/molbev/msx285"
    assert rows[1]["REFERENCE_NAME"] == "Somebody et al 2020" and rows[1]["DOI"] == "" \
        and rows[1]["HOST_SPECIES"] == "Zenaida macroura"
    assert before[0]["DOI"] == ""  # the copy proves the rows were changed in place, nothing else


def test_an_identical_barcode_with_a_second_reading_is_not_a_row(sync):
    """A tie, a complete lineage one base away from a partial exact match, or a record
    that states another name: each is a curator's call, never an add."""
    lin1 = _random_seq(1, WIDTH)
    rng = random.Random(3)
    lin2 = list(lin1)
    for pos in rng.sample(range(WIDTH), 20):
        lin2[pos] = rng.choice([b for b in "ACGT" if b != lin1[pos]])
    lin2 = "".join(lin2)
    one_off = lin1[:200] + ("A" if lin1[200] != "A" else "C") + lin1[201:]
    partial = one_off[:50] + "N" + one_off[51:]          # identical to one_off over 478
    ref = sync.sequence_check.Reference.from_sequences(
        ["FULL01", "FULL02", "PART01", "DUP01"], [lin1, lin2, partial, lin2])
    # exact and alone: a row
    assert sync.call_barcode("a", lin1, ref).exact
    # identical to PART01 over 478 but one base from FULL01 over 479: not a row
    call = sync.call_barcode("b", one_off, ref)
    assert call.distance == 0 and not call.exact and "one base from FULL01 over 479" in call.caveat
    # identical to two stored lineages: not a row
    call = sync.call_barcode("c", lin2, ref)
    assert call.distance == 0 and not call.exact and "also identical to" in call.caveat
    # exact, but the record itself says another lineage: not a row
    call = sync.call_barcode("d", lin1, ref)
    candidate = sync.Candidate("X1", "nuclear_genome", "test", stated_lineage="record states: FULL02 (/strain)")
    assert sync.decide(candidate, call, set()) == "curator call" and "the record states FULL02" in call.caveat
    candidate = sync.Candidate("X2", "nuclear_genome", "test", stated_lineage="record states: FULL01 (/strain)")
    assert sync.decide(candidate, sync.call_barcode("e", lin1, ref), set()) == "add"


def test_kmer_prefilter_skips_low_complexity_kmers(sync):
    class Ref:
        seqs = ["ACGTTGCAAGGCTTACGATCGGATTACAGCATGCA" * 14, "AAAAAAAAAAAATTTTTTTTTTTTAAAAAAAAAAAA" * 14]
    index = sync.build_kmer_index(Ref())
    assert "ACGTTGCAAGGCTTACGATCGGAT" in index
    assert "AAAAAAAAAAAATTTTTTTTTTTT" not in index
    # A short sequence is registered whole; a long one only around its hits.
    assert sync.barcode_windows("ACGT" * 100, index) == ["ACGT" * 100]
    long = "G" * 20000 + "ACGTTGCAAGGCTTACGATCGGATTACAGCATGCA" + "C" * 20000
    windows = sync.barcode_windows(long, index)
    assert len(windows) == 1 and "ACGTTGCAAGGCTTACGATCGGAT" in windows[0] and len(windows[0]) <= 2 * sync.WINDOW_FLANK_BP
