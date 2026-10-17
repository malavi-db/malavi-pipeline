"""Unit tests for the accession miner — the real logic in the Phase 1 scaffold."""
from malavi_curation.accession_mine import expand_accession_ranges, mine_accessions, mine_doi


def test_classic_genbank_nucleotide():
    text = "We deposited sequences AF069611 and MN696030 in GenBank."
    hits = mine_accessions(text)
    assert "AF069611" in hits.nucleotide
    assert "MN696030" in hits.nucleotide


def test_versioned_accession_kept_with_suffix():
    hits = mine_accessions("Accession KY653758.1 was used.")
    assert "KY653758.1" in hits.nucleotide


def test_bioproject_and_sra():
    text = "Reads under PRJNA384068 (runs SRR5278536, ERX1234567)."
    hits = mine_accessions(text)
    assert "PRJNA384068" in hits.bioproject
    assert "SRR5278536" in hits.sra
    assert "ERX1234567" in hits.sra


def test_assembly_refseq_and_wgs():
    text = "Genomes GCF_001447265.1, GCA_900002375.1, NC_012426 and WGS LSRZ01000000."
    hits = mine_accessions(text)
    assert "GCF_001447265.1" in hits.assembly
    assert "GCA_900002375.1" in hits.assembly
    assert "NC_012426" in hits.assembly
    assert "LSRZ01000000" in hits.assembly


def test_dedup_and_sorted():
    hits = mine_accessions("AF069611 AF069611 AB000001")
    assert hits.nucleotide == ["AB000001", "AF069611"]


def test_empty_text():
    hits = mine_accessions("")
    assert hits.is_empty()
    assert mine_accessions("no accessions here, just prose").is_empty()


def test_all_combines_classes():
    hits = mine_accessions("AF069611 and PRJNA384068")
    assert hits.all() == ["AF069611", "PRJNA384068"]


# -- range expansion (the key feature for recall) -----------------------------

def test_range_full_prefix_both_ends():
    accs, labels = expand_accession_ranges("deposited under PV948475-PV948494.")
    assert "PV948475" in accs and "PV948494" in accs
    assert "PV948490" in accs and "PV948491" in accs  # interior, never written
    assert len(accs) == 20
    assert labels == ["PV948475-PV948494"]


def test_range_en_dash():
    accs, _ = expand_accession_ranges("PQ562381–PQ562407")
    assert "PQ562381" in accs and "PQ562407" in accs and "PQ562400" in accs


def test_range_abbreviated_endpoint():
    accs, _ = expand_accession_ranges("MN696030-45")
    assert accs[0] == "MN696030" and accs[-1] == "MN696045" and len(accs) == 16


def test_mismatched_prefixes_not_a_range():
    # Two distinct accessions joined by a dash must NOT be expanded.
    accs, labels = expand_accession_ranges("AF069611-MN696030")
    assert accs == [] and labels == []


def test_runaway_range_capped():
    # End before start, or absurd spans, are rejected.
    assert expand_accession_ranges("PV900000-PV100000") == ([], [])


def test_mine_accessions_includes_range_interior():
    hits = mine_accessions("New sequences PV948475-PV948494 were deposited.")
    assert "PV948490" in hits.nucleotide
    assert "PV948475-PV948494" in hits.ranges


# --- DOI mining --------------------------------------------------------------
def test_mine_doi_basic():
    assert mine_doi("Available at https://doi.org/10.1111/mec.12345 today.") == "10.1111/mec.12345"


def test_mine_doi_strips_trailing_punctuation():
    # A DOI ending a sentence must not keep the period.
    assert mine_doi("See doi:10.1186/s12936-024-04901-6.") == "10.1186/s12936-024-04901-6"


def test_mine_doi_picks_modal_over_cited():
    # The article's own DOI repeats (running header); a cited DOI appears once.
    text = (
        "10.1002/ece3.9999 header\nBody text.\n"
        "References: Smith 2009 10.1111/j.1755-0998.2009.02694.x\n"
        "10.1002/ece3.9999 footer"
    )
    assert mine_doi(text) == "10.1002/ece3.9999"


def test_mine_doi_none_when_absent():
    assert mine_doi("No identifier here.") is None
    assert mine_doi("") is None


def test_mine_doi_lowercases():
    assert mine_doi("DOI: 10.1234/ABC.DEF") == "10.1234/abc.def"
