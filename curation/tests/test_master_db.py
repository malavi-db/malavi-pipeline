"""Tests for importing the Lund master database into the record store.

The export rules in ``master_db`` were measured against the 2026-03-23 release; these
tests pin each measured rule on a tiny database with the same schema, so that a later
"simplification" of a rule fails here rather than in the reproduction check on release
day. The merge and replay tests cover the two ways the import could quietly lose
something the store already knows: a record's identity, and a correction already made.
"""
import csv
import sqlite3

import pytest

from malavi_curation.master_db import (
    correction_from_log, export_tables, merge_table, normalize_text, replay_corrections,
)
from malavi_curation.release_store import TABLES


SCHEMA = """
CREATE TABLE LM_REFERENCES (REFERENCE_ID integer PRIMARY KEY, REFERENCE_NAME text,
  PUBLICATION_YEAR numeric, TITLE text, JOURNAL_ID integer, VOLUME_PAGES text,
  STUDY_TYPE_ID integer, DELETED text);
CREATE TABLE LM_JOURNALS (JOURNAL_ID integer PRIMARY KEY, JOURNAL_NAME text, DELETED text);
CREATE TABLE LM_STUDY_TYPES (STUDY_TYPE_ID integer PRIMARY KEY, STUDY_TYPE text, DELETED text);
CREATE TABLE TAX_ORDER (ORDER_ID integer PRIMARY KEY, ORDER_NAME text);
CREATE TABLE TAX_FAMILY (FAMILY_ID integer PRIMARY KEY, FAMILY_NAME text, ORDER_ID integer);
CREATE TABLE TAX_GENUS (GENUS_ID integer PRIMARY KEY, GENUS_NAME text, FAMILY_ID integer);
CREATE TABLE TAX_SPECIES (SPECIES_ID integer PRIMARY KEY, SPECIES_NAME text, GENUS_ID integer);
CREATE TABLE TAX_SUB_SPECIES (SUB_SPECIES_ID integer PRIMARY KEY, SUB_SPECIES_NAME text,
  SPECIES_ID integer);
CREATE TABLE LM_CONTINENTS (CONTINENT_ID integer PRIMARY KEY, CONTINENT_NAME text);
CREATE TABLE LM_COUNTRIES (COUNTRY_ID integer PRIMARY KEY, COUNTRY_NAME text,
  CONTINENT_ID integer);
CREATE TABLE LM_COUNTRY_REGIONS (COUNTRY_REGION_ID integer PRIMARY KEY,
  COUNTRY_REGION_NAME text);
CREATE TABLE LM_SITES (SITE_ID integer PRIMARY KEY, SITE_NAME text, COUNTRY_ID integer,
  LATITUDE text, LONGITUDE text, DELETED text);
CREATE TABLE LM_HOST_AGE (HOST_AGE_ID integer PRIMARY KEY, HOST_AGE text);
CREATE TABLE LM_HOST_STATUS (HOST_STATUS_ID integer PRIMARY KEY, HOST_STATUS text);
CREATE TABLE LM_SEQ_LENGTH (SEQ_LENGTH_ID integer PRIMARY KEY, SEQ_LENGTH text);
CREATE TABLE LM_VECTOR_METHODS (VECTOR_METHOD_ID integer PRIMARY KEY,
  VECTOR_METHOD_NAME text);
CREATE TABLE MASTER (LINEAGE_NAME text PRIMARY KEY, SEQ_LENGTH_ID integer, GENBANK_ACC text,
  PARASITE_GENUS_ID integer, REFERENCE_ID integer, DELETED text);
CREATE TABLE SEQUENCES (LINEAGE_NAME text PRIMARY KEY, SEQUENCE text, DELETED text);
CREATE TABLE LM_LINEAGE_NAMES (LINEAGE_KEY integer PRIMARY KEY, LINEAGE_NAME text,
  ALT_NAME text, GENBANK_ACCNR text, REFERENCE_ID integer, DELETED text);
CREATE TABLE HOSTS_AND_SITES (FINDING_ID integer PRIMARY KEY, LINEAGE_NAME text,
  HOST_SPECIES_ID integer, HOST_SUB_SPECIES_ID integer, HOST_AGE_ID integer,
  HOST_STATUS_ID integer, COUNTRY_ID integer, COUNTRY_REGION_ID integer, SITE_ID integer,
  NUMBER_FOUND numeric, NUMBER_TESTED numeric, REFERENCE_ID integer, CAPTIVITY integer,
  COMMENT text, DELETED text);
CREATE TABLE VECTOR_DATA (LINEAGE_NAME text, VECTOR_SPECIES integer,
  VECTOR_METHOD_ID integer, COUNTRY_ID integer, SITE_ID integer, REFERENCE_ID integer,
  DELETED text);
CREATE TABLE MORPHO_SPECIES (FINDING_ID integer PRIMARY KEY, LINEAGE_NAME text,
  GENUS_ID integer, SPECIES_ID integer, REFERENCE_ID integer, MORPHOLOGY_COMMENT text,
  DELETED text);
"""

SEQ = "A" * 479


@pytest.fixture
def db(tmp_path):
    """A four-lineage database exercising every measured rule."""
    path = tmp_path / "MALAVI.sqlite"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.executescript("""
    INSERT INTO LM_JOURNALS VALUES (1, 'Parasitology', NULL);
    INSERT INTO LM_STUDY_TYPES VALUES (1, 'Single species', NULL), (99, 'Unpublished', NULL);
    -- Reference 2 carries the empty-string DELETED and a CRLF in its name: both live.
    INSERT INTO LM_REFERENCES VALUES
      (1, 'Smith et al 2010', 2010, 'A title', 1, '1:1-2', 1, NULL),
      (2, 'Jones et al 2011' || char(13) || char(10), 2011, 'Another', 1, '2:3-4', 1, ''),
      (3, 'Lee unpubl', 9999, 'Not yet', 1, NULL, 99, NULL),
      (4, 'Zhu unpubl 4', 2002, 'A real year, unpublished study type', 1, NULL, 99, NULL);
    INSERT INTO TAX_ORDER VALUES (1, 'Passeriformes'), (2, 'Haemosporida');
    INSERT INTO TAX_FAMILY VALUES (1, 'Turdidae', 1), (2, 'Plasmodiidae', 2), (3, 'Culicidae', 1);
    INSERT INTO TAX_GENUS VALUES (1, 'Turdus', 1), (2, 'Plasmodium', 2), (3, 'Culex', 3);
    -- The host species name carries a trailing newline, as real TAX_SPECIES rows do.
    INSERT INTO TAX_SPECIES VALUES (1, 'Turdus merula' || char(10), 1),
      (2, 'Plasmodium relictum', 2), (3, 'Culex pipiens', 3), (4, 'Plasmodium elongatum', 2);
    INSERT INTO TAX_SUB_SPECIES VALUES (1, 'Turdus merula merula', 1);
    INSERT INTO LM_CONTINENTS VALUES (1, 'Europe');
    INSERT INTO LM_COUNTRIES VALUES (1, 'Sweden', 1);
    INSERT INTO LM_COUNTRY_REGIONS VALUES (1, 'Skane');
    -- The latitude carries a trailing space, as 1,284 real sites do; it must survive.
    INSERT INTO LM_SITES VALUES (1, 'Lund', 1, '55°42.00000'' ', '013°11.00000''', NULL);
    INSERT INTO LM_HOST_AGE VALUES (1, 'Adult');
    INSERT INTO LM_HOST_STATUS VALUES (2, 'Resident');
    INSERT INTO LM_SEQ_LENGTH VALUES (1, 'Full');
    INSERT INTO LM_VECTOR_METHODS VALUES (1, 'PCR, head and thorax');
    -- GONE01 is soft-deleted with a real timestamp; the others are live.
    INSERT INTO MASTER VALUES
      ('SGS1', 1, ' AF495571' || char(160), 2, 1, NULL),
      ('TUMER01', 1, 'AB000001', 2, 2, ''),
      ('TWO01', 1, NULL, 2, 1, NULL),
      ('GONE01', 1, 'X1', 2, 1, '2013-02-10 00:00:00');
    INSERT INTO SEQUENCES VALUES ('SGS1', '%(seq)s', NULL), ('TUMER01', '%(seq)s', NULL),
      ('TWO01', '%(seq)s', NULL), ('GONE01', '%(seq)s', '2013-02-10 00:00:00');
    -- Alternative names: one cell already comma-joined; two rows for one pair whose
    -- accession order differs from key order; a name from a reference with no host
    -- record (must not reach the alt_names table); a row with no lineage.
    INSERT INTO LM_LINEAGE_NAMES VALUES
      (1, 'SGS1', 'P22,P-SGS1', NULL, 1, NULL),
      (2, 'SGS1', 'ZZ9', 'B0002', 1, NULL),
      (3, 'SGS1', 'AA1', 'B0001', 1, NULL),
      (4, 'SGS1', 'ORPHAN', NULL, 2, NULL),
      (5, NULL, 'NOLIN', NULL, 1, NULL),
      (6, 'TUMER01', 'T1', NULL, 2, NULL);
    -- Host records: captive flag NULL exports as Wild; row 3 is soft-deleted.
    INSERT INTO HOSTS_AND_SITES VALUES
      (1, 'SGS1', 1, 1, 1, 2, 1, 1, 1, 3, 10, 1, 0, 'a comment' || char(13) || char(10), NULL),
      (2, 'TUMER01', 1, NULL, NULL, NULL, 1, NULL, NULL, 1, NULL, 2, NULL, NULL, ''),
      (3, 'TWO01', 1, NULL, NULL, NULL, 1, NULL, NULL, 1, NULL, 1, 1, NULL, '2014-01-01'),
      (4, 'TWO01', 1, NULL, NULL, NULL, 1, NULL, NULL, 1, NULL, 3, 1, NULL, NULL);
    INSERT INTO VECTOR_DATA VALUES ('SGS1', 3, 1, 1, 1, 1, NULL);
    -- TWO01 links to two morphospecies from two studies; SGS1 to one from two studies.
    INSERT INTO MORPHO_SPECIES VALUES
      (1, 'SGS1', 2, 2, 1, NULL, NULL), (2, 'SGS1', 2, 2, 2, NULL, NULL),
      (3, 'TWO01', 2, 2, 1, NULL, NULL), (4, 'TWO01', 2, 4, 2, 'rare', NULL);
    """ % {"seq": SEQ})
    c.commit()
    c.close()
    return path


def _by(rows, **match):
    return [row for row in rows if all(row[k] == v for k, v in match.items())]


def test_normalize_text_collapses_crlf_and_nbsp():
    assert normalize_text("Perrin et al 2026\r\n") == "Perrin et al 2026"
    assert normalize_text("\xa0 KF717063") == "KF717063"
    assert normalize_text("Aysul et al\r\n 2013") == "Aysul et al 2013"
    assert normalize_text(None) == ""


def test_soft_delete_semantics(db):
    """Only a non-empty DELETED is a deletion; the empty string is live."""
    tables, findings = export_tables(db)
    names = {row["LINEAGE_NAME"] for row in tables["lineages"]}
    assert "GONE01" not in names and "TUMER01" in names
    # host row 3 (dated DELETED) is gone; row 2 (empty-string DELETED) stays
    assert len(tables["host_records"]) == 3
    assert {f["kind"]: f["detail"] for f in findings}["soft_deleted_lineages_excluded"] \
        == ["GONE01"]


def test_host_record_derivations(db):
    tables, _ = export_tables(db)
    sgs1 = _by(tables["host_records"], LINEAGE_NAME="SGS1")[0]
    assert sgs1["ALT_NAME"] == "P-SGS1,P22,AA1,ZZ9", \
        "split the joined cell, order by accession then name, de-duplicate"
    assert sgs1["PARASITE_GENUS"] == "Plasmodium"
    assert (sgs1["ORDER_NAME"], sgs1["FAMILY_NAME"], sgs1["GENUS_NAME"],
            sgs1["SPECIES_NAME"]) == ("Passeriformes", "Turdidae", "Turdus", "Turdus merula")
    assert sgs1["SUB_SPECIES_NAME"] == "Turdus merula merula"
    assert sgs1["HOST_ENVIRONMENT"] == "Wild"
    assert sgs1["SITE_COORDINATES"] == "55°42.00000' , 013°11.00000'", \
        "joined raw, then whitespace-collapsed: the release's own form"
    assert (sgs1["NUMBER_FOUND"], sgs1["NUMBER_TESTED"]) == ("3", "10")
    assert sgs1["COMMENT"] == "a comment"
    tumer = _by(tables["host_records"], LINEAGE_NAME="TUMER01")[0]
    assert tumer["HOST_ENVIRONMENT"] == "Wild", "a NULL captivity flag exports as Wild"
    assert tumer["REFERENCE_NAME"] == "Jones et al 2011"
    assert tumer["SITE_NAME"] == "" and tumer["SITE_COORDINATES"] == ""
    assert tumer["NUMBER_TESTED"] == ""
    two = _by(tables["host_records"], LINEAGE_NAME="TWO01")[0]
    assert two["HOST_ENVIRONMENT"] == "Captivity"
    assert two["REFERENCE_NAME"] == "Lee unpubl", \
        "records citing an unpublished study are still exported"


def test_alt_names_table_is_restricted_to_pairs_with_a_host_record(db):
    tables, findings = export_tables(db)
    triples = {(r["LINEAGE_NAME"], r["ALT_NAME"], r["REFERENCE_NAME"])
               for r in tables["alt_names"]}
    assert triples == {("SGS1", "P22", "Smith et al 2010"),
                       ("SGS1", "P-SGS1", "Smith et al 2010"),
                       ("SGS1", "AA1", "Smith et al 2010"),
                       ("SGS1", "ZZ9", "Smith et al 2010"),
                       ("TUMER01", "T1", "Jones et al 2011")}
    assert ("SGS1", "ORPHAN", "Jones et al 2011") not in triples
    assert {f["kind"]: f["detail"] for f in findings}[
        "alt_name_rows_without_lineage_skipped"] == 1


def test_references_table_leaves_unpublished_out_and_normalizes_names(db):
    tables, _ = export_tables(db)
    names = [r["REFERENCE_NAME"] for r in tables["references"]]
    assert names == ["Smith et al 2010", "Jones et al 2011"], \
        "year 9999 and study type Unpublished are both left out"
    assert tables["references"][0]["JOURNAL_NAME"] == "Parasitology"
    assert tables["references"][0]["STUDY_TYPE"] == "Single species"


def test_lineages_fan_out_per_morphospecies_and_strip_accessions(db):
    tables, findings = export_tables(db)
    sgs1 = _by(tables["lineages"], LINEAGE_NAME="SGS1")
    assert len(sgs1) == 1 and sgs1[0]["SPECIES_NAME"] == "Plasmodium relictum", \
        "two studies linking the same species give one summary row"
    assert sgs1[0]["GENBANK_ACC"] == "AF495571"
    two = _by(tables["lineages"], LINEAGE_NAME="TWO01")
    assert sorted(r["SPECIES_NAME"] for r in two) == \
        ["Plasmodium elongatum", "Plasmodium relictum"]
    assert {f["kind"]: f["detail"] for f in findings}[
        "lineages_with_several_morphospecies"] == ["TWO01"]
    assert all(r["SEQUENCE"] == SEQ for r in tables["lineages"])


def test_vector_and_morpho_tables(db):
    tables, _ = export_tables(db)
    assert tables["vector_records"] == [{
        "LINEAGE_NAME": "SGS1", "VECTOR_SPECIES": "Culex pipiens",
        "VECTOR_METHOD": "PCR, head and thorax", "COUNTRY_NAME": "Sweden",
        "SITE_NAME": "Lund", "REFERENCE_NAME": "Smith et al 2010"}]
    assert len(tables["morpho_species"]) == 4
    assert _by(tables["morpho_species"], LINEAGE_NAME="TWO01",
               SPECIES_NAME="Plasmodium elongatum")[0]["MORPHOLOGY_COMMENT"] == "rare"


def test_exported_tables_have_exactly_the_store_columns(db):
    tables, _ = export_tables(db)
    for name, spec in TABLES.items():
        for row in tables[name]:
            assert list(row.keys()) == list(spec.columns), name


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _host(lineage, species, site, reference, **extra):
    row = {column: "" for column in TABLES["host_records"].columns}
    row.update({"LINEAGE_NAME": lineage, "SPECIES_NAME": species, "SITE_NAME": site,
                "REFERENCE_NAME": reference})
    row.update(extra)
    return row


def test_merge_keeps_identity_for_identical_and_edited_rows_and_reports_the_rest():
    spec = TABLES["host_records"]
    existing = [
        dict(_host("A", "Turdus merula", "Lund", "Smith 2010", NUMBER_FOUND="1"),
             RECORD_ID="HST-000001", _source="seed", _added="2026-03-23"),
        dict(_host("B", "Turdus merula", "Lund", "Smith 2010", NUMBER_FOUND="2"),
             RECORD_ID="HST-000002", _source="seed", _added="2026-03-23"),
        dict(_host("C", "Turdus merula", "Lund", "Smith 2010", NUMBER_FOUND="3"),
             RECORD_ID="HST-000003", _source="seed", _added="2026-03-23"),
    ]
    exported = [
        _host("A", "Turdus merula", "Lund", "Smith 2010", NUMBER_FOUND="1"),   # identical
        _host("B", "Turdus merula", "Lund", "Smith 2010", NUMBER_FOUND="5"),   # edited
        _host("D", "Turdus merula", "Lund", "Smith 2010", NUMBER_FOUND="4"),   # new
    ]
    merged, report = merge_table(spec, existing, exported, "2026-09-15")
    by_lineage = {row["LINEAGE_NAME"]: row for row in merged}
    assert by_lineage["A"]["RECORD_ID"] == "HST-000001"
    assert by_lineage["B"]["RECORD_ID"] == "HST-000002" and \
        by_lineage["B"]["NUMBER_FOUND"] == "5"
    assert by_lineage["B"]["_added"] == "2026-03-23", "an edit is not a new record"
    assert by_lineage["D"]["RECORD_ID"] == "HST-000004", "ids are never reissued"
    assert by_lineage["D"]["_source"] == "seed" and by_lineage["D"]["_added"] == "2026-09-15"
    assert "C" not in by_lineage
    assert (report["n_kept_identical"], report["n_changed"], report["n_added"],
            report["n_removed"]) == (1, 1, 1, 1)
    assert report["changed"][0]["columns"] == {"NUMBER_FOUND": {"was": "2", "now": "5"}}
    assert report["removed"][0]["record_id"] == "HST-000003"


def test_merge_pairs_rows_sharing_a_natural_key_by_closeness():
    """Two records of one lineage in one host at one site from one study, differing in
    age class: an edit to one of them keeps that one's identity, not its sibling's."""
    spec = TABLES["host_records"]
    existing = [
        dict(_host("A", "Turdus merula", "Lund", "Smith 2010", HOST_AGE="Adult",
                   NUMBER_FOUND="1"),
             RECORD_ID="HST-000001", _source="seed", _added="2026-03-23"),
        dict(_host("A", "Turdus merula", "Lund", "Smith 2010", HOST_AGE="Nestling",
                   NUMBER_FOUND="2"),
             RECORD_ID="HST-000002", _source="seed", _added="2026-03-23"),
    ]
    exported = [
        _host("A", "Turdus merula", "Lund", "Smith 2010", HOST_AGE="Nestling",
              NUMBER_FOUND="9"),
        _host("A", "Turdus merula", "Lund", "Smith 2010", HOST_AGE="Adult",
              NUMBER_FOUND="1"),
    ]
    merged, report = merge_table(spec, existing, exported, "2026-09-15")
    by_id = {row["RECORD_ID"]: row for row in merged}
    assert by_id["HST-000001"]["HOST_AGE"] == "Adult"
    assert by_id["HST-000002"]["HOST_AGE"] == "Nestling" and \
        by_id["HST-000002"]["NUMBER_FOUND"] == "9"
    assert (report["n_kept_identical"], report["n_changed"], report["n_added"],
            report["n_removed"]) == (1, 1, 0, 0)


def test_merge_never_reissues_a_removed_row_id():
    spec = TABLES["host_records"]
    existing = [
        dict(_host("A", "Turdus merula", "Lund", "Smith 2010"),
             RECORD_ID="HST-000001", _source="seed", _added="2026-03-23"),
        dict(_host("B", "Turdus merula", "Lund", "Smith 2010"),
             RECORD_ID="HST-000002", _source="seed", _added="2026-03-23"),
    ]
    exported = [_host("C", "Turdus merula", "Lund", "Smith 2010")]
    merged, report = merge_table(spec, existing, exported, "2026-09-15")
    assert len(merged) == 1 and merged[0]["RECORD_ID"] == "HST-000003"
    assert report["n_added"] == 1 and report["n_removed"] == 2


# ---------------------------------------------------------------------------
# Correction replay
# ---------------------------------------------------------------------------

def test_correction_from_log_reads_the_selector_back():
    where = correction_from_log({"CORRECTION_ID": "COR-000006", "TABLE": "vector_records",
                                 "SELECTOR": "where VECTOR_METHOD='Unkown'",
                                 "COLUMN": "VECTOR_METHOD", "NEW_VALUE": "Unknown",
                                 "REASON": "spelling", "ACTOR": "maintainer"})
    assert (where.selector_kind, where.selector_column, where.selector_value) == \
        ("where", "VECTOR_METHOD", "Unkown")
    site = correction_from_log({"CORRECTION_ID": "COR-000005", "TABLE": "host_records",
                                "SELECTOR": "site 'Mata Seca State Park'",
                                "COLUMN": "SITE_COORDINATES", "NEW_VALUE": "x",
                                "REASON": "", "ACTOR": ""})
    assert (site.selector_kind, site.selector_value) == ("site", "Mata Seca State Park")
    record = correction_from_log({"CORRECTION_ID": "COR-000099", "TABLE": "lineages",
                                  "SELECTOR": "record LIN-000007", "COLUMN": "GENBANK_ACC",
                                  "NEW_VALUE": "y", "REASON": "", "ACTOR": ""})
    assert (record.selector_kind, record.selector_value) == ("record", "LIN-000007")


def test_replay_reapplies_only_where_the_fault_is_still_present():
    store = {"vector_records": [
        {"RECORD_ID": "VEC-000001", "LINEAGE_NAME": "A", "VECTOR_SPECIES": "Culex",
         "VECTOR_METHOD": "Unkown", "COUNTRY_NAME": "", "SITE_NAME": "",
         "REFERENCE_NAME": "r"},
        {"RECORD_ID": "VEC-000002", "LINEAGE_NAME": "B", "VECTOR_SPECIES": "Culex",
         "VECTOR_METHOD": "Unknown", "COUNTRY_NAME": "", "SITE_NAME": "",
         "REFERENCE_NAME": "r"}]}
    log = [
        {"CORRECTION_ID": "COR-000006", "TABLE": "vector_records",
         "SELECTOR": "where VECTOR_METHOD='Unkown'", "COLUMN": "VECTOR_METHOD",
         "NEW_VALUE": "Unknown", "REASON": "spelling", "ACTOR": "m", "ROWS_CHANGED": "90"},
        {"CORRECTION_ID": "COR-000031", "TABLE": "lineages",
         "SELECTOR": "where GENUS_NAME='N/A'", "COLUMN": "GENUS_NAME",
         "NEW_VALUE": "Haemoproteus", "REASON": "", "ACTOR": "m", "ROWS_CHANGED": "7"},
    ]
    outcome = replay_corrections(store, log)
    assert store["vector_records"][0]["VECTOR_METHOD"] == "Unknown"
    assert outcome[0]["status"] == "reapplied" and outcome[0]["rows_changed"] == 1
    assert outcome[1]["status"] == "not_needed" and outcome[1]["rows_changed"] == 0


def test_write_export_round_trips_through_csv(tmp_path, db):
    from malavi_curation.master_db import write_export
    tables, _ = export_tables(db)
    paths = write_export(tmp_path / "out", tables)
    assert {p.name for p in paths} == {spec.filename for spec in TABLES.values()}
    with open(tmp_path / "out" / "host_records.csv", newline="", encoding="utf-8") as h:
        rows = list(csv.DictReader(h))
    assert len(rows) == 3 and "RECORD_ID" not in rows[0]


def test_merge_store_leaves_ingested_submission_rows_alone():
    """A second import must not delete what a submission put in the store: Lund's export
    is the whole truth about seed rows only."""
    from malavi_curation.master_db import merge_store
    existing = {name: [] for name in TABLES}
    existing["host_records"] = [
        dict(_host("A", "Turdus merula", "Lund", "Smith 2010"),
             RECORD_ID="HST-000001", _source="seed", _added="2026-03-23"),
        dict(_host("Z", "Turdus merula", "Lund", "Jones 2026"),
             RECORD_ID="HST-000002", _source="MALAVI-SUB-2026-000009", _added="2026-09-15"),
    ]
    exported = {name: [] for name in TABLES}
    exported["host_records"] = [_host("A", "Turdus merula", "Lund", "Smith 2010")]
    store, report = merge_store(existing, exported, "2026-10-01")
    ids = {row["RECORD_ID"]: row for row in store["host_records"]}
    assert set(ids) == {"HST-000001", "HST-000002"}
    assert ids["HST-000002"]["_source"] == "MALAVI-SUB-2026-000009"
    assert report["host_records"]["n_removed"] == 0
    assert report["host_records"]["n_kept_from_submissions"] == 1
