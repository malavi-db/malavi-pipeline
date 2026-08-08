"""Unit tests for host/geography extraction.

These use a small explicit gazetteer so they don't depend on the generated
data/gazetteer.json, keeping them fast and hermetic.
"""
from malavi_curation.hosts_geography import extract_hosts_geography

GENERA = {"Accipiter", "Buteo", "Falco", "Gyps", "Bubo"}
VECTOR_GENERA = {"Culex", "Aedes", "Culicoides", "Simulium"}
COUNTRIES = {"Greece", "United States", "Sweden"}


def test_full_binomial_with_known_genus():
    text = "We sampled Accipiter cooperii and Buteo jamaicensis from nests."
    hg = extract_hosts_geography(text, known_genera=GENERA, countries=COUNTRIES)
    assert "Accipiter cooperii" in hg.hosts
    assert "Buteo jamaicensis" in hg.hosts


def test_unknown_genus_filtered_out():
    # Plasmodium is a parasite genus, not an avian host genus -> excluded.
    hg = extract_hosts_geography("Plasmodium relictum infected the host.",
                                 known_genera=GENERA, countries=COUNTRIES)
    assert hg.hosts == []


def test_epithet_stoplist():
    hg = extract_hosts_geography("Falco sp. and Buteo spp. were noted.",
                                 known_genera=GENERA, countries=COUNTRIES)
    assert hg.hosts == []


def test_abbreviations_off_by_default():
    # "H. nisi" must NOT become a host (H. = Haemoproteus, not a bird here).
    text = "Gyps fulvus carried H. nisi parasites."
    hg = extract_hosts_geography(text, known_genera=GENERA, countries=COUNTRIES)
    assert "Gyps fulvus" in hg.hosts
    assert hg.abbreviations_expanded == []


def test_abbreviations_optin_expands_known_genus():
    text = "Buteo jamaicensis and B. lagopus were sampled."
    hg = extract_hosts_geography(text, known_genera=GENERA, countries=COUNTRIES,
                                 expand_abbreviations=True)
    assert "Buteo lagopus" in hg.hosts
    assert "Buteo lagopus" in hg.abbreviations_expanded


def test_country_and_alias():
    hg = extract_hosts_geography("Birds were sampled in the USA and in Greece.",
                                 known_genera=GENERA, countries=COUNTRIES)
    assert "United States" in hg.countries  # via USA alias
    assert "Greece" in hg.countries


def test_supplement_flag():
    hg = extract_hosts_geography("Details are in Supplementary Table S1.",
                                 known_genera=GENERA, countries=COUNTRIES)
    assert hg.needs_supplement is True


def test_vector_species_extracted_and_separated_from_hosts():
    text = ("Buteo jamaicensis was infected; the lineage was also detected in "
            "Culex pipiens and Aedes albopictus mosquitoes.")
    hg = extract_hosts_geography(text, known_genera=GENERA, countries=COUNTRIES,
                                 known_vector_genera=VECTOR_GENERA)
    assert "Buteo jamaicensis" in hg.hosts
    assert "Culex pipiens" in hg.vectors
    assert "Aedes albopictus" in hg.vectors
    # Vectors must not leak into the host list (disjoint vocabularies).
    assert "Culex pipiens" not in hg.hosts


# ---------------------------------------------------------------------------
# Country vocabulary
#
# The gazetteer lists only countries MalAvi already has records for, so using it
# alone means a country new to the database can never be detected -- which is the
# case most worth catching. These pin the wider vocabulary.
# ---------------------------------------------------------------------------

def test_country_vocabulary_includes_countries_new_to_malavi():
    from malavi_curation.hosts_geography import load_country_vocabulary
    v = load_country_vocabulary()
    assert v["in_malavi"] <= v["all"]
    assert v["new_to_malavi"] == v["all"] - v["in_malavi"]
    # Gambia is on the basemap but had no MalAvi records when this was written.
    assert "Gambia" in v["all"]


def test_gambia_is_detected_in_text():
    """The regression: a first-national-record paper must not lose its country."""
    from malavi_curation.hosts_geography import extract_hosts_geography
    text = ("Hooded Vultures were blood sampled in The Gambia between 2019 and 2025. "
            "This represents the first molecular record in The Gambia.")
    res = extract_hosts_geography(text)
    assert "Gambia" in res.countries


def test_article_form_matches_without_duplicating():
    from malavi_curation.hosts_geography import extract_hosts_geography
    res = extract_hosts_geography("Samples came from The Netherlands and from Gambia.")
    assert "Netherlands" in res.countries
    assert "Gambia" in res.countries
    assert len(res.countries) == len(set(res.countries))
