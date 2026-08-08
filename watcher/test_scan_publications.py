"""Unit tests for the publication watcher's pure logic (no network).

Covers the DOI/title normalization, the cross-source de-dup key, the relevance and
MalAvi title filters, the MalAvi-references loader, and the end-to-end merge in
scan() with the network source functions monkeypatched out. Run with:
    python -m pytest watcher/test_scan_publications.py
"""
import json

import scan_publications as s


# --------------------------------------------------------------------------- #
# normalize_doi
# --------------------------------------------------------------------------- #
def test_normalize_doi_strips_resolver_and_lowercases():
    assert s.normalize_doi("https://doi.org/10.1/AbC") == "10.1/abc"
    assert s.normalize_doi("http://dx.doi.org/10.1/x") == "10.1/x"
    assert s.normalize_doi("doi:10.1/Y") == "10.1/y"
    assert s.normalize_doi("  10.1/Z  ") == "10.1/z"


def test_normalize_doi_empty():
    assert s.normalize_doi("") == ""
    assert s.normalize_doi(None) == ""


# --------------------------------------------------------------------------- #
# dedup_key
# --------------------------------------------------------------------------- #
def test_dedup_key_prefers_doi():
    hit = {"id": "MED:123", "doi": "https://doi.org/10.1/A"}
    assert s.dedup_key(hit) == "doi:10.1/a"


def test_dedup_key_falls_back_to_id_when_no_doi():
    hit = {"id": "PPR:xyz", "doi": ""}
    assert s.dedup_key(hit) == "PPR:xyz"


def test_dedup_key_same_paper_different_sources_collapses():
    epmc = {"id": "MED:1", "doi": "10.1/a"}
    openalex = {"id": "OPENALEX:W9", "doi": "https://doi.org/10.1/A"}
    assert s.dedup_key(epmc) == s.dedup_key(openalex)


# --------------------------------------------------------------------------- #
# title_is_relevant
# --------------------------------------------------------------------------- #
def test_title_is_relevant_matches_substring_case_insensitive():
    terms = ["haemospor", "malaria"]
    assert s.title_is_relevant("Avian MALARIA in blue tits", terms)
    assert s.title_is_relevant("Haemosporidian diversity", terms)


def test_title_is_relevant_rejects_offtopic():
    assert not s.title_is_relevant("Table of Contents", ["haemospor", "malaria"])
    assert not s.title_is_relevant("", ["malaria"])


# --------------------------------------------------------------------------- #
# normalize_title  (HTML entities + punctuation)
# --------------------------------------------------------------------------- #
def test_normalize_title_unescapes_entities_and_strips_punctuation():
    a = s.normalize_title("Plasmodium (Marchiafava &amp; Celli, 1885)")
    b = s.normalize_title("Plasmodium Marchiafava & Celli 1885")
    assert a == b == "plasmodium marchiafava celli 1885"


# --------------------------------------------------------------------------- #
# load_malavi_titles
# --------------------------------------------------------------------------- #
def test_load_malavi_titles_datatables_format(tmp_path):
    payload = {
        "columns": ["REFERENCE_NAME", "PUBLICATION_YEAR", "TITLE", "JOURNAL_NAME"],
        "data": [
            ["Ref A", "2009", "A Study of Avian Malaria", "Journal X"],
            ["Ref B", "2018", "Blood Parasites &amp; Birds", "Journal Y"],
        ],
    }
    p = tmp_path / "refs.json"
    p.write_text(json.dumps(payload))
    titles = s.load_malavi_titles(p)
    assert "a study of avian malaria" in titles
    assert "blood parasites birds" in titles  # entity unescaped, punctuation dropped


def test_load_malavi_titles_plain_list(tmp_path):
    p = tmp_path / "refs.json"
    p.write_text(json.dumps(["Avian Malaria", "Leucocytozoon Review"]))
    titles = s.load_malavi_titles(p)
    assert titles == {"avian malaria", "leucocytozoon review"}


# --------------------------------------------------------------------------- #
# scan()  — end-to-end merge with network sources monkeypatched out
# --------------------------------------------------------------------------- #
def _fake_config():
    return {
        "watcher": {
            "sources": ["europepmc", "openalex", "crossref"],
            "europepmc_query": "epmc-query",
            "keyword_query": "kw-query",
            "relevance_filter_sources": ["crossref"],
            "relevance_terms": ["malaria"],
            "lookback_days": 14,
            "delivery": "issue",
            "max_items_per_batch": 50,
        }
    }


def _install_fake_sources(monkeypatch):
    """Wire SOURCE_FUNCS to canned hits exercising every merge path."""
    def epmc(query, days):
        return [
            {"id": "MED:1", "doi": "10.1/a", "title": "Avian malaria in tits",
             "journal": "J", "year": "2026", "url": "u", "sources": ["europepmc"]},
            {"id": "MED:2", "doi": "10.1/b", "title": "Already in MalAvi malaria paper",
             "journal": "J", "year": "2025", "url": "u", "sources": ["europepmc"]},
        ]

    def openalex(query, days):
        # Same paper as MED:1 (DOI differs only in case) -> should collapse.
        return [
            {"id": "OPENALEX:W1", "doi": "https://doi.org/10.1/A", "title": "Avian malaria in tits",
             "journal": "J", "year": "2026", "url": "u", "sources": ["openalex"]},
        ]

    def crossref(query, days):
        return [
            {"id": "DOI:10.1/c", "doi": "10.1/c", "title": "Table of Contents",
             "journal": "J", "year": "2026", "url": "u", "sources": ["crossref"]},  # dropped: no term
            {"id": "DOI:10.1/d", "doi": "10.1/d", "title": "Malaria vectors of birds",
             "journal": "J", "year": "2026", "url": "u", "sources": ["crossref"]},  # kept
        ]

    monkeypatch.setattr(s, "load_config", _fake_config)
    monkeypatch.setattr(s, "SOURCE_FUNCS",
                        {"europepmc": epmc, "openalex": openalex, "crossref": crossref})


def test_scan_merges_dedups_and_filters(monkeypatch, tmp_path):
    _install_fake_sources(monkeypatch)
    monkeypatch.setattr(s, "SEEN_PATH", tmp_path / "seen.json")
    malavi = {s.normalize_title("Already in MalAvi malaria paper")}

    batch = s.scan(malavi_titles=malavi, save=True)
    keys = {s.dedup_key(h) for h in batch}

    # 10.1/a survives once, tagged with both sources it came from.
    assert "doi:10.1/a" in keys
    merged_a = next(h for h in batch if s.dedup_key(h) == "doi:10.1/a")
    assert merged_a["sources"] == ["europepmc", "openalex"]
    # Crossref TOC dropped by relevance filter; the real crossref paper kept.
    assert "doi:10.1/c" not in keys
    assert "doi:10.1/d" in keys
    # Already-in-MalAvi paper dropped by the title filter.
    assert "doi:10.1/b" not in keys
    assert len(batch) == 2


def test_scan_excludes_already_seen(monkeypatch, tmp_path):
    _install_fake_sources(monkeypatch)
    seen_file = tmp_path / "seen.json"
    seen_file.write_text(json.dumps(["doi:10.1/d"]))  # pretend we reported it before
    monkeypatch.setattr(s, "SEEN_PATH", seen_file)

    batch = s.scan(save=False)
    keys = {s.dedup_key(h) for h in batch}
    assert "doi:10.1/d" not in keys           # excluded as already seen
    assert "doi:10.1/a" in keys


def test_scan_no_save_leaves_seen_untouched(monkeypatch, tmp_path):
    _install_fake_sources(monkeypatch)
    seen_file = tmp_path / "seen.json"
    monkeypatch.setattr(s, "SEEN_PATH", seen_file)

    s.scan(save=False)
    assert not seen_file.exists()  # nothing persisted
