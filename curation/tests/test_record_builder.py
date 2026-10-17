"""Tests for record_builder: extraction outputs -> validated submission record."""
from malavi_curation.accession_mine import AccessionHits
from malavi_curation.hosts_geography import HostGeography
from malavi_curation.record_builder import build_submission


def _sample():
    acc = AccessionHits(nucleotide=["PV357399", "PV357400"], ranges=["PV357399-PV357400"])
    hg = HostGeography(hosts=["Gyps fulvus"], countries=["Greece"], needs_supplement=False)
    return acc, hg


def test_build_submission_validates_against_schema():
    acc, hg = _sample()
    sub = build_submission({"doi": "10.0/x", "title": "T", "year": 2025},
                           accessions=acc, hostgeo=hg, validate=True)
    assert sub["reference"]["doi"] == "10.0/x"
    assert sub["accessions"] == ["PV357399", "PV357400"]
    # Prose mining yields host MENTIONS, not host-lineage associations, so it
    # must produce no records at all. The names survive as curator leads.
    assert sub["records"] == []
    assert sub["provenance"]["candidate_hosts"] == ["Gyps fulvus"]
    assert sub["provenance"]["candidate_countries"] == ["Greece"]
    assert sub["provenance"]["source"] == "curation_helper"
    assert sub["provenance"]["needs_review"] is True
    assert sub["provenance"]["accession_ranges"] == ["PV357399-PV357400"]


def test_ambiguous_country_listed_but_never_guessed():
    acc = AccessionHits(nucleotide=["PV357399"])
    hg = HostGeography(hosts=["Gyps fulvus"], countries=["Greece", "Spain"])
    sub = build_submission({"title": "T"}, accessions=acc, hostgeo=hg)
    assert sub["records"] == []
    # Both localities are shown to the curator; neither is silently chosen.
    assert sub["provenance"]["candidate_countries"] == ["Greece", "Spain"]


def test_structured_table_still_produces_real_records():
    """The structured path is the ONLY route to a record, and it still works."""
    rows = [{"lineage_name": "SGS1", "host_species": "Parus major",
             "country": "Sweden", "number_tested": 50, "number_found": 7}]
    sub = build_submission({"title": "T"}, structured_records=rows, validate=True)
    assert len(sub["records"]) == 1
    rec = sub["records"][0]
    assert rec["lineage_name"] == "SGS1" and rec["host_species"] == "Parus major"


def test_prose_hosts_never_become_records_even_with_one_country():
    """Regression: the Gambia paper produced 15 records and none was real."""
    hg = HostGeography(hosts=["Eudyptula minor", "Aegypius monachus"],
                       countries=["Gambia"])
    sub = build_submission({"title": "T"}, hostgeo=hg, validate=True)
    assert sub["records"] == []
    assert set(sub["provenance"]["candidate_hosts"]) == {"Eudyptula minor",
                                                         "Aegypius monachus"}


def test_minimal_reference_only():
    sub = build_submission({"title": "Just a title"})
    assert sub["records"] == []
    assert sub["vectors"] == []
    assert sub["accessions"] == []


def test_vectors_from_hostgeo_become_candidate_vector_records():
    hg = HostGeography(hosts=["Gyps fulvus"], vectors=["Culex pipiens"],
                       countries=["Japan"])
    sub = build_submission({"title": "T"}, hostgeo=hg, validate=True)
    assert sub["vectors"][0]["vector_species"] == "Culex pipiens"
    # Sole country is attached to vector candidates too.
    assert sub["vectors"][0]["country"] == "Japan"


def test_prevalence_carried_on_structured_records():
    rows = [{"lineage_name": "SGS1", "host_species": "Parus major",
             "country": "Sweden", "number_tested": 50, "number_found": 7}]
    sub = build_submission({"title": "T"}, structured_records=rows, validate=True)
    rec = sub["records"][0]
    assert rec["number_tested"] == 50 and rec["number_found"] == 7


def test_structured_vectors_take_precedence():
    vrows = [{"lineage_name": "GRW04", "vector_species": "Culex pipiens",
              "vector_method": "PCR", "country": "Japan"}]
    sub = build_submission({"title": "T"}, structured_vectors=vrows, validate=True)
    assert sub["vectors"][0]["vector_method"] == "PCR"


def test_the_sole_country_is_attached_to_structured_records():
    """A record is lineage x host x *place*, and supplements rarely name the place.

    When the paper mentions exactly one country, that is the study's country and
    every record belongs to it. This copies a fact the document states; it is not
    an inference, which is why several countries leaves the field for the curator.
    """
    submission = build_submission(
        {"title": "test"},
        hostgeo=HostGeography(countries=["Austria"]),
        structured_records=[{"lineage_name": "BUBT3", "host_species": "Buteo buteo"}],
        validate=False,
    )
    assert submission["records"][0]["country"] == "Austria"


def test_several_candidate_countries_leaves_the_record_unplaced():
    submission = build_submission(
        {"title": "test"},
        hostgeo=HostGeography(countries=["Austria", "Germany"]),
        structured_records=[{"lineage_name": "BUBT3", "host_species": "Buteo buteo"}],
        validate=False,
    )
    assert submission["records"][0]["country"] is None


def test_structured_and_mentioned_vectors_are_both_kept():
    """The supplement covers the mosquitoes sequenced; the text names the rest."""
    submission = build_submission(
        {"title": "test"},
        hostgeo=HostGeography(vectors=["Culex pipiens", "Aedes aegypti"]),
        structured_vectors=[{"lineage_name": "CULPIP03", "vector_species": "Culex pipiens"}],
        validate=False,
    )
    by_species = {v["vector_species"]: v for v in submission["vectors"]}
    assert set(by_species) == {"Culex pipiens", "Aedes aegypti"}
    assert by_species["Culex pipiens"]["lineage_name"] == "CULPIP03"
    assert by_species["Aedes aegypti"]["lineage_name"] is None
