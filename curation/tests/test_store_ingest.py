"""Mapping an approved submission's workbook into record-store rows.

The store had one writer, release_seed, so nothing could add to MalAvi after it was
seeded. These cover the mapping that closes that: what the template supplies directly,
what is derived and from where, and what is deliberately left blank rather than guessed.
"""
import pytest

openpyxl = pytest.importorskip("openpyxl")

from malavi_curation import store_ingest
from malavi_curation.release_store import TABLES


# --------------------------------------------------------------------------- fixtures

HOSTS_HEADER = ["LINEAGE_NAME", "HostSpecies", "HOST_SPECIES_ID", "HostSubspecies",
                "HostAge", "HostStatus", "HostEnvironment", "Country", "CountryRegion",
                "SiteName", "NUMBER_FOUND", "NUMBER_TESTED", "Reference", "COMMENT"]


def workbook(tmp_path, hosts_rows, sites_rows=(), new_lineages=()):
    """A minimal but real ImportMalavi workbook, read by the same code the screen uses."""
    wb = openpyxl.Workbook()
    hosts = wb.active
    hosts.title = "Hosts_and_Sites"
    hosts.append(["The records themselves."])          # the sheet's instruction note
    hosts.append(HOSTS_HEADER)
    for row in hosts_rows:
        hosts.append(row)

    sites = wb.create_sheet("Sites")
    sites.append(["One row per sampling locality."])
    sites.append(["SITE_NAME", "Country", "LATITUDE", "LONGITUDE", "ALTITUDE(m)"])
    for row in sites_rows:
        sites.append(row)

    lineages = wb.create_sheet("NewLineages")
    lineages.append(["One row per NEW lineage."])
    lineages.append(["LINEAGE_NAME", "ParasiteGenus"])
    for row in new_lineages:
        lineages.append(row)

    path = tmp_path / "submission.xlsx"
    wb.save(path)
    return path


# Rows as the store really holds them: SPECIES_NAME is the BINOMIAL, not the epithet.
# This fixture said "merula" until 2026-08-11, which is why every test over it agreed
# with an ingest that was writing the wrong thing into a published column.
EXISTING = [
    {"GENUS_NAME": "Turdus", "SPECIES_NAME": "Turdus merula",
     "ORDER_NAME": "Passeriformes", "FAMILY_NAME": "Turdidae",
     "COUNTRY_NAME": "Sweden", "CONTINENT_NAME": "Europe"},
    {"GENUS_NAME": "Turdus", "SPECIES_NAME": "Turdus pilaris",
     "ORDER_NAME": "Passeriformes", "FAMILY_NAME": "Turdidae",
     "COUNTRY_NAME": "Nigeria", "CONTINENT_NAME": "Africa"},
]


# ----------------------------------------------------------------------- host species

def test_a_binomial_splits_into_genus_and_species():
    """SPECIES_NAME is the binomial. See test_species_name_holds_the_binomial_not_the
    _epithet below for why this test asserted the epithet until 2026-08-11."""
    assert store_ingest.split_host_species("Accipiter tachiro") == (
        "Accipiter", "Accipiter tachiro")


def test_a_third_word_is_not_absorbed_into_the_species():
    """HostSubspecies has its own column; folding it in would hide it from every check."""
    assert store_ingest.split_host_species("Turdus merula aterrimus") == (
        "Turdus", "Turdus merula")


def test_a_bare_genus_yields_no_species():
    assert store_ingest.split_host_species("Turdus") == ("Turdus", "")
    assert store_ingest.split_host_species(None) == ("", "")


# -------------------------------------------------------------------------- taxonomy

def test_order_and_family_come_from_malavis_own_records():
    index = store_ingest.taxonomy_index(EXISTING)
    assert index[("Turdus", "Turdus merula")] == ("Passeriformes", "Turdidae")


def test_a_new_species_in_a_known_genus_still_places():
    index = store_ingest.taxonomy_index(EXISTING)
    assert index[("Turdus", "")] == ("Passeriformes", "Turdidae")


def test_rows_missing_the_values_contribute_nothing():
    index = store_ingest.taxonomy_index(
        [{"GENUS_NAME": "Corvus", "SPECIES_NAME": "corax", "ORDER_NAME": "",
          "FAMILY_NAME": "Corvidae"}])
    assert index == {}


# ------------------------------------------------------------------------- continent

def test_the_continent_comes_from_the_store_not_from_a_region_code():
    """region_for('Sweden') is 'EUROPE'; CONTINENT_NAME is 'Europe'.

    Two vocabularies for two questions. Writing the region code into the continent column
    would corrupt what the Grand Lineage Summary's region columns are derived from, and it
    would look entirely plausible sitting in the CSV.
    """
    index = store_ingest.continent_index(EXISTING)
    assert index == {"Sweden": "Europe", "Nigeria": "Africa"}


def test_unknown_is_not_learned_as_a_continent():
    index = store_ingest.continent_index(
        [{"COUNTRY_NAME": "Atlantis", "CONTINENT_NAME": "Unknown"}])
    assert index == {}


# ------------------------------------------------------------------ the whole mapping

def test_the_template_columns_arrive_in_the_store_row(tmp_path):
    path = workbook(tmp_path, [[
        "TUMIG01", "Turdus merula", "", "aterrimus", "Adult", "Resident", "Wild",
        "Sweden", "Skane", "Krankesjon", 3, 40, "Ellis et al 2027", "a comment"]])
    rows, _notes = store_ingest.host_rows_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)

    assert len(rows) == 1
    row = rows[0]
    # The five columns submission.json has nowhere to put -- the reason this reads the
    # workbook rather than that file.
    assert row["HOST_AGE"] == "Adult"
    assert row["HOST_STATUS"] == "Resident"
    assert row["HOST_ENVIRONMENT"] == "Wild"
    assert row["SUB_SPECIES_NAME"] == "aterrimus"
    assert row["COUNTRY_REGION_NAME"] == "Skane"
    assert row["REFERENCE_NAME"] == "Ellis et al 2027"
    assert row["COMMENT"] == "a comment"
    assert (row["NUMBER_FOUND"], row["NUMBER_TESTED"]) == ("3", "40")


def test_every_store_column_is_present_even_when_empty(tmp_path):
    """A short row would shift columns in the CSV, silently, for every row after it."""
    path = workbook(tmp_path, [["TUMIG01", "Turdus merula"] + [""] * 12])
    rows, _ = store_ingest.host_rows_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    for column in TABLES["host_records"].columns:
        assert column in rows[0]


def test_derived_values_are_filled_in(tmp_path):
    path = workbook(
        tmp_path,
        [["TUMIG01", "Turdus merula", "", "", "", "", "", "Sweden", "", "Krankesjon",
          1, 10, "Ellis et al 2027", ""]],
        sites_rows=[["Krankesjon", "Sweden", "55.7", "13.5", "20"]],
        new_lineages=[["TUMIG01", "Haemoproteus"]])
    rows, notes = store_ingest.host_rows_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    row = rows[0]
    assert (row["ORDER_NAME"], row["FAMILY_NAME"]) == ("Passeriformes", "Turdidae")
    assert row["CONTINENT_NAME"] == "Europe"
    assert row["SITE_COORDINATES"] == "55.7, 13.5"
    assert row["PARASITE_GENUS"] == "Haemoproteus"
    assert notes == []


def test_what_malavi_does_not_know_is_left_blank_and_reported(tmp_path):
    """Never a guess. An unsourced order in a release carries a submitter's name."""
    path = workbook(tmp_path, [[
        "NECMON01", "Necrosyrtes monachus", "", "", "", "", "", "The Gambia", "",
        "Kanifing", 1, 5, "Shimizu et al 2026", ""]])
    rows, notes = store_ingest.host_rows_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    row = rows[0]
    assert row["ORDER_NAME"] == "" and row["FAMILY_NAME"] == ""
    assert row["CONTINENT_NAME"] == ""
    assert any("Necrosyrtes" in note for note in notes)
    assert any("Gambia" in note for note in notes)
    assert any("Kanifing" in note for note in notes)      # no coordinates on Sites


def test_provenance_is_stamped_for_the_release_gate(tmp_path):
    """_source is exactly what release_gate checks against the review ledger."""
    path = workbook(tmp_path, [["TUMIG01", "Turdus merula"] + [""] * 12])
    rows, _ = store_ingest.host_rows_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert rows[0]["_source"] == "MALAVI-SUB-2026-000123"
    assert rows[0]["_added"] == "2026-09-01"
    # RECORD_ID is minted by assign_ids at write time and never reissued.
    assert "RECORD_ID" not in rows[0]


def test_a_lineage_already_in_malavi_gets_no_genus_from_the_submitter(tmp_path):
    """Restating a known lineage's genus is how one lineage ends up with two."""
    path = workbook(tmp_path,
                    [["SGS1", "Turdus merula"] + [""] * 12],
                    new_lineages=[["TUMIG99", "Haemoproteus"]])
    rows, _ = store_ingest.host_rows_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert rows[0]["PARASITE_GENUS"] == ""


def test_a_workbook_with_no_records_sheet_is_reported(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.title = "Something else"
    path = tmp_path / "wrong.xlsx"
    wb.save(path)
    rows, notes = store_ingest.host_rows_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert rows == []
    assert "Hosts_and_Sites" in notes[0]


# ------------------------------------------------------------------ replacing on a fix
#
# Re-ingesting is what happens after a correction, and a correction says the earlier
# version was wrong. These pin what must survive it and what must not.

SPEC = TABLES["host_records"]
MINE = "MALAVI-SUB-2026-000123"


def stored(lineage, species, site, reference, record_id, source=MINE, **extra):
    row = {column: "" for column in SPEC.columns}
    row.update({"LINEAGE_NAME": lineage, "SPECIES_NAME": species, "SITE_NAME": site,
                "REFERENCE_NAME": reference, "RECORD_ID": record_id,
                "_source": source, "_added": "2026-01-01"})
    row.update(extra)
    return row


def incoming(lineage, species, site, reference, **extra):
    row = {column: "" for column in SPEC.columns}
    row.update({"LINEAGE_NAME": lineage, "SPECIES_NAME": species, "SITE_NAME": site,
                "REFERENCE_NAME": reference, "_source": MINE, "_added": "2026-09-01"})
    row.update(extra)
    return row


def test_an_unchanged_row_keeps_its_record_id():
    """The store promises an id is assigned once. A correction is not a re-import."""
    existing = [stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000042")]
    rows, counts = store_ingest.replace_submission_rows(
        SPEC, existing, [incoming("TUMIG01", "merula", "Krankesjon", "Ellis 2027")], MINE)
    assert rows[0]["RECORD_ID"] == "HST-000042"
    assert counts["kept"] == 1


def test_a_corrected_row_keeps_its_id_but_takes_the_new_values():
    existing = [stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000042",
                       HOST_AGE="Adult")]
    rows, counts = store_ingest.replace_submission_rows(
        SPEC, existing,
        [incoming("TUMIG01", "merula", "Krankesjon", "Ellis 2027", HOST_AGE="Juvenile")],
        MINE)
    assert rows[0]["RECORD_ID"] == "HST-000042"
    assert rows[0]["HOST_AGE"] == "Juvenile"
    assert counts["replaced"] == 1


def test_the_first_seen_release_is_not_rewritten_by_a_correction():
    """_added answers "since when has MalAvi held this?" -- a fixed host age does not
    change the answer, and rewriting it would turn the column into a copy of today."""
    existing = [stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000042")]
    rows, _ = store_ingest.replace_submission_rows(
        SPEC, existing,
        [incoming("TUMIG01", "merula", "Krankesjon", "Ellis 2027", HOST_AGE="Adult")],
        MINE)
    assert rows[0]["_added"] == "2026-01-01"


def test_a_row_the_correction_drops_is_removed():
    existing = [stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000042"),
                stored("TUMIG01", "pilaris", "Krankesjon", "Ellis 2027", "HST-000043")]
    rows, counts = store_ingest.replace_submission_rows(
        SPEC, existing, [incoming("TUMIG01", "merula", "Krankesjon", "Ellis 2027")], MINE)
    assert len(rows) == 1
    assert counts["removed"] == 1


def test_a_new_row_is_added_without_an_id():
    """assign_ids mints it at write time, and never reissues an existing one."""
    existing = [stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000042")]
    rows, counts = store_ingest.replace_submission_rows(
        SPEC, existing,
        [incoming("TUMIG01", "merula", "Krankesjon", "Ellis 2027"),
         incoming("TUMIG02", "merula", "Krankesjon", "Ellis 2027")], MINE)
    assert counts["added"] == 1
    assert not rows[1].get("RECORD_ID")


def test_seed_rows_and_other_submissions_are_never_touched():
    """Deciding somebody else's record is superseded is a curator's judgment."""
    existing = [
        stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000001", source="seed"),
        stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000002",
               source="MALAVI-SUB-2026-000999"),
        stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000003"),
    ]
    rows, counts = store_ingest.replace_submission_rows(SPEC, existing, [], MINE)
    assert [row["RECORD_ID"] for row in rows] == ["HST-000001", "HST-000002"]
    assert counts["removed"] == 1


def test_surviving_rows_stay_where_they_were():
    """So the CSV diff of a correction shows the correction, not a reshuffle."""
    existing = [
        stored("AAA01", "merula", "S", "R", "HST-000001", source="seed"),
        stored("TUMIG01", "merula", "Krankesjon", "Ellis 2027", "HST-000042"),
        stored("ZZZ99", "merula", "S", "R", "HST-000009", source="seed"),
    ]
    rows, _ = store_ingest.replace_submission_rows(
        SPEC, existing,
        [incoming("TUMIG01", "merula", "Krankesjon", "Ellis 2027", HOST_AGE="Adult")],
        MINE)
    assert [row["RECORD_ID"] for row in rows] == ["HST-000001", "HST-000042", "HST-000009"]


# ------------------------------------------------------- the other four store tables

def full_workbook(tmp_path, new_lineages=(), sequences=(), reference=(), vectors=(),
                  alt_names=()):
    wb = openpyxl.Workbook()
    hosts = wb.active
    hosts.title = "Hosts_and_Sites"
    hosts.append(["The records themselves."])
    hosts.append(HOSTS_HEADER)

    def sheet(name, note, header, rows):
        ws = wb.create_sheet(name)
        ws.append([note])
        ws.append(header)
        for row in rows:
            ws.append(row)

    sheet("NewLineages", "One row per NEW lineage.",
          ["LINEAGE_NAME", "GENBANK_NR", "ParasiteGenus", "HOST_SPECIES_ID",
           "Reference", "COMMENT"], new_lineages)
    sheet("Sequences", "The sequence for every new lineage.",
          ["LINEAGE_NAME", "SEQUENCE"], sequences)
    sheet("Reference", "The publication.",
          ["REFERENCE_NAME", "PUBLICATION_YEAR", "TITLE", "JOURNAL_NAME", "Volume",
           "StartPage", "EndPage", "DOI"], reference)
    sheet("Vectors", "Lineages detected in arthropod vectors.",
          ["LINEAGE_NAME", "VectorSpecies", "VECTOR_METHOD", "Country", "CountryRegion",
           "SiteName", "No_found", "No_tested", "Reference", "Comment"], vectors)
    sheet("Alt_Lineage_names", "Synonyms.",
          ["MalAvi_Name", "Alternative_Name", "GenBankNr", "Reference", "Comment"],
          alt_names)

    path = tmp_path / "full.xlsx"
    wb.save(path)
    return path


WINDOW = "A" * 479


def test_a_new_lineage_carries_its_sequence_and_computed_length(tmp_path):
    path = full_workbook(
        tmp_path,
        new_lineages=[["TUMIG99", "PZ000001", "Haemoproteus", "", "Ellis 2027", ""]],
        sequences=[["TUMIG99", WINDOW]])
    tables, notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    row = tables["lineages"][0]
    assert row["SEQ_LENGTH"] == ""             # categorical, and a curator's judgment
    assert row["GENUS_NAME"] == "Haemoproteus"
    assert row["SPECIES_NAME"] == ""           # morphospecies is a curator's judgment
    assert any("SEQ_LENGTH" in note for note in notes)


def test_a_sequence_off_the_barcode_window_is_reported(tmp_path):
    """All 5,368 lineages in the store are exactly 479 bp. A 485 would be the first."""
    path = full_workbook(
        tmp_path,
        new_lineages=[["TUMIG99", "PZ000001", "Haemoproteus", "", "Ellis 2027", ""]],
        sequences=[["TUMIG99", "A" * 485]])
    _tables, notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert any("485 bp" in note and "479" in note for note in notes)


def test_several_accessions_are_reported_not_silently_written(tmp_path):
    """Every lineage in the store carries exactly one. Which to keep is a curator's call."""
    path = full_workbook(
        tmp_path,
        new_lineages=[["TUMIG99", "PZ1, PZ2, PZ3", "Haemoproteus", "", "Ellis 2027", ""]],
        sequences=[["TUMIG99", WINDOW]])
    _tables, notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert any("3 accessions" in note for note in notes)


def test_a_lineage_with_no_sequence_is_reported(tmp_path):
    path = full_workbook(
        tmp_path,
        new_lineages=[["TUMIG99", "PZ000001", "Haemoproteus", "", "Ellis 2027", ""]])
    _tables, notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert any("no sequence" in note for note in notes)


def test_an_unpublished_study_gets_no_reference_row(tmp_path):
    """0 of the 526 rows in references.csv are unpublished, and that is the convention.

    A stub row here would be a citation to a publication that does not exist, and two
    years later it would be indistinguishable from a real one.
    """
    path = full_workbook(
        tmp_path,
        reference=[["Ellis et al unpubl", "", "", "", "", "", "", ""]])
    tables, notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert tables["references"] == []
    assert any("unpublished" in note for note in notes)


def test_a_published_study_gets_its_row_with_assembled_pages(tmp_path):
    path = full_workbook(
        tmp_path,
        reference=[["Ellis et al 2027", "2027", "A  title\nwrapped", "Mol Ecol",
                    "36", "1123", "1140", "10.1111/mec.1"]])
    tables, notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    row = tables["references"][0]
    assert row["VOLUME_PAGES"] == "36:1123-1140"
    # Excel wraps long titles; no title in the store contains a newline.
    assert row["TITLE"] == "A title wrapped"
    assert row["STUDY_TYPE"] == ""             # the template does not ask; a curator sets it
    assert any("DOI" in note for note in notes)


def test_vector_records_map_and_report_what_cannot_be_held(tmp_path):
    path = full_workbook(
        tmp_path,
        vectors=[["TUMIG01", "Culex pipiens", "PCR", "Sweden", "Skane", "Krankesjon",
                  2, 30, "Ellis 2027", "a note"]])
    tables, notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    row = tables["vector_records"][0]
    assert row["VECTOR_SPECIES"] == "Culex pipiens"
    assert row["VECTOR_METHOD"] == "PCR"
    assert row["COUNTRY_NAME"] == "Sweden"
    # vector_records has no column for counts, and dropping them in silence would be a
    # loss nobody could see.
    assert any("No_found" in note for note in notes)


def test_the_synonym_is_not_stored_the_wrong_way_round(tmp_path):
    """MalAvi_Name is MalAvi's name; Alternative_Name is the paper's.

    Swapped, MalAvi would answer to the paper's name and offer its own as the synonym.
    """
    path = full_workbook(
        tmp_path,
        alt_names=[["SGS1", "Lineage-A", "AF000001", "Ellis 2027", ""]])
    tables, _notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    row = tables["alt_names"][0]
    assert row["LINEAGE_NAME"] == "SGS1"
    assert row["ALT_NAME"] == "Lineage-A"


def test_morpho_species_is_not_produced(tmp_path):
    """The template has no sheet for it: it is a taxonomic act with its own literature."""
    path = full_workbook(tmp_path)
    tables, _notes = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert "morpho_species" not in tables


# ---------------------------------------------------- duplicates within one submission
#
# The natural key (lineage, species, site, reference) cannot separate two samplings of
# one host at one site in one paper. 678 keys in the seed are duplicated across 1,624
# rows, and the store's stated position is that identical rows are preserved rather than
# merged. A dict keyed on the row silently kept the last of each group.

def test_two_incoming_rows_with_one_key_are_both_written():
    rows, counts = store_ingest.replace_submission_rows(
        SPEC, [], [incoming("TUMIG01", "merula", "S", "R", NUMBER_FOUND="1"),
                   incoming("TUMIG01", "merula", "S", "R", NUMBER_FOUND="5")], MINE)
    assert len(rows) == 2
    assert counts["added"] == 2


def test_duplicate_rows_keep_their_own_ids_and_their_own_data():
    """The chimera case: one row's RECORD_ID with another row's values, and one row lost."""
    existing = [stored("TUMIG01", "merula", "S", "R", "HST-000001", NUMBER_FOUND="1"),
                stored("TUMIG01", "merula", "S", "R", "HST-000002", NUMBER_FOUND="2")]
    rows, counts = store_ingest.replace_submission_rows(
        SPEC, existing,
        [incoming("TUMIG01", "merula", "S", "R", NUMBER_FOUND="1"),
         incoming("TUMIG01", "merula", "S", "R", NUMBER_FOUND="5")], MINE)
    assert [(row["RECORD_ID"], row["NUMBER_FOUND"]) for row in rows] == [
        ("HST-000001", "1"), ("HST-000002", "5")]
    assert counts == {"kept": 1, "replaced": 1, "added": 0, "removed": 0}


def test_a_correction_that_drops_one_of_two_duplicates_removes_exactly_one():
    existing = [stored("TUMIG01", "merula", "S", "R", "HST-000001"),
                stored("TUMIG01", "merula", "S", "R", "HST-000002")]
    rows, counts = store_ingest.replace_submission_rows(
        SPEC, existing, [incoming("TUMIG01", "merula", "S", "R")], MINE)
    assert len(rows) == 1
    assert counts["removed"] == 1


# ------------------------------------------------------- what a re-ingest would erase
#
# replace_submission_rows reports a blanked value as "replaced", which is indistinguishable
# in the counts from a correction that genuinely changed something. These cover the check
# that tells the two apart, because the columns the mapping deliberately leaves blank are
# exactly the ones a curator is asked to fill in afterwards.

def test_a_value_the_workbook_cannot_supply_is_reported_as_lost():
    before = [stored("TUMIG01", "merula", "S", "R", "HST-000001", ALT_NAME="B12")]
    after = [incoming("TUMIG01", "merula", "S", "R", RECORD_ID="HST-000001")]
    lost = store_ingest.blanked_values(SPEC, before, after, MINE)
    assert [(item["column"], item["was"]) for item in lost] == [("ALT_NAME", "B12")]


def test_a_changed_value_is_a_correction_not_a_loss():
    """Changing a value is the point of a re-ingest. Only emptying one is a loss."""
    before = [stored("TUMIG01", "merula", "S", "R", "HST-000001", NUMBER_FOUND="1")]
    after = [incoming("TUMIG01", "merula", "S", "R", RECORD_ID="HST-000001",
                      NUMBER_FOUND="3")]
    assert store_ingest.blanked_values(SPEC, before, after, MINE) == []


def test_a_dropped_row_is_not_counted_column_by_column():
    """A removed row is one removal, already counted, not twenty lost values."""
    before = [stored("TUMIG01", "merula", "S", "R", "HST-000001", ALT_NAME="B12",
                     COMMENT="from the paper")]
    assert store_ingest.blanked_values(SPEC, before, [], MINE) == []


def test_another_submissions_rows_are_not_examined():
    before = [stored("TUMIG01", "merula", "S", "R", "HST-000001", ALT_NAME="B12",
                     source="seed")]
    assert store_ingest.blanked_values(SPEC, before, [], MINE) == []


# --------------------------------------------------------------- the name that was agreed
#
# A proposed lineage name MalAvi already owns is a WARNING at screen time, because the
# report offers a free alternative and approving the submission adopts it. Nothing applied
# that agreement at the write until 2026-08-11: rehearsed on the demo submission, the
# ledger held name_corrections {'TUMIG10': 'TUMIG32'}, reserved TUMIG32 publicly, and the
# store received TUMIG10 -- the name of a different lineage, with a different sequence.

def _row(table, **values):
    row = {column: "" for column in TABLES[table].columns}
    row.update(values)
    return row


def test_every_table_that_carries_a_lineage_name_is_renamed_together():
    """A rename applied to some tables leaves records pointing at a lineage row that no
    longer exists under that name."""
    tables = {
        "lineages": [_row("lineages", LINEAGE_NAME="TUMIG10", GENBANK_ACC="PQ118836")],
        "host_records": [_row("host_records", LINEAGE_NAME="TUMIG10",
                              SPECIES_NAME="Turdus migratorius")],
        "vector_records": [_row("vector_records", LINEAGE_NAME="TUMIG10")],
        "alt_names": [_row("alt_names", LINEAGE_NAME="TUMIG10", ALT_NAME="B12")],
    }
    renamed, notes = store_ingest.apply_name_corrections(tables, {"TUMIG10": "TUMIG32"})

    for table in ("lineages", "host_records", "vector_records", "alt_names"):
        assert renamed[table][0]["LINEAGE_NAME"] == "TUMIG32", table
    assert len(notes) == 1 and "TUMIG10 -> TUMIG32" in notes[0]


def test_the_superseded_proposal_is_not_recorded_as_a_synonym():
    """TUMIG10 belongs to a different lineage. Writing it in as an alternative name would
    assert that MalAvi's TUMIG10 and this new lineage are the same thing."""
    tables = {"alt_names": [_row("alt_names", LINEAGE_NAME="TUMIG10", ALT_NAME="B12")]}
    renamed, _ = store_ingest.apply_name_corrections(tables, {"TUMIG10": "TUMIG32"})
    assert renamed["alt_names"][0]["ALT_NAME"] == "B12"


def test_a_lineage_nobody_corrected_is_left_alone():
    tables = {"lineages": [_row("lineages", LINEAGE_NAME="TUMIG31")]}
    renamed, notes = store_ingest.apply_name_corrections(tables, {"TUMIG10": "TUMIG32"})
    assert renamed["lineages"][0]["LINEAGE_NAME"] == "TUMIG31"
    assert notes == []


def test_every_lineage_bearing_table_is_covered():
    """REGRESSION GUARD: a table added to the store must not be left behind by a rename."""
    assert set(store_ingest.lineage_name_tables()) == {
        "lineages", "host_records", "vector_records", "alt_names", "morpho_species"}


# ------------------------------------------------------------------- refusing a collision

def _store(*lineage_rows):
    return {"lineages": list(lineage_rows)}


def test_a_name_another_source_holds_is_refused():
    store = _store(_row("lineages", LINEAGE_NAME="TUMIG10", GENBANK_ACC="KF314763",
                        SEQUENCE="ACGT", _source="seed"))
    incoming_rows = [_row("lineages", LINEAGE_NAME="TUMIG10", GENBANK_ACC="PQ118836",
                          SEQUENCE="ACGA", _source=MINE)]
    messages = store_ingest.colliding_lineages(store, incoming_rows, MINE)
    assert len(messages) == 1
    assert "already a lineage in MalAvi (KF314763)" in messages[0]
    assert "two lineages under one name" in messages[0]


def test_an_identical_sequence_is_refused_with_its_own_reason():
    """Not a new lineage at all: it is a record of the one MalAvi already holds."""
    store = _store(_row("lineages", LINEAGE_NAME="TUMIG10", SEQUENCE="ACGT",
                        _source="seed"))
    incoming_rows = [_row("lineages", LINEAGE_NAME="TUMIG10", SEQUENCE="ACGT",
                          _source=MINE)]
    messages = store_ingest.colliding_lineages(store, incoming_rows, MINE)
    assert "the sequences are identical" in messages[0]


def test_a_re_ingest_does_not_collide_with_itself():
    """replace_submission_rows rewrites this submission's rows, so the version being
    replaced must not refuse the correction that replaces it."""
    store = _store(_row("lineages", LINEAGE_NAME="TUMIG32", SEQUENCE="ACGT",
                        _source=MINE))
    incoming_rows = [_row("lineages", LINEAGE_NAME="TUMIG32", SEQUENCE="ACGA",
                          _source=MINE)]
    assert store_ingest.colliding_lineages(store, incoming_rows, MINE) == []


def test_a_free_name_is_not_refused():
    store = _store(_row("lineages", LINEAGE_NAME="TUMIG10", _source="seed"))
    incoming_rows = [_row("lineages", LINEAGE_NAME="TUMIG31", _source=MINE)]
    assert store_ingest.colliding_lineages(store, incoming_rows, MINE) == []


def test_one_submission_declaring_the_same_new_name_twice_is_refused():
    store = _store()
    incoming_rows = [_row("lineages", LINEAGE_NAME="TUMIG31", _source=MINE),
                     _row("lineages", LINEAGE_NAME="TUMIG31", _source=MINE)]
    messages = store_ingest.colliding_lineages(store, incoming_rows, MINE)
    assert "declared as a new lineage 2 times" in messages[0]


# ------------------------------------------------------- the two 2026-08-11 review blockers
#
# Both were found by an independent code review and then confirmed by running the real
# ingest against a scratch copy of the real store. Neither was caught by any check, warning
# or the edition report.

def test_species_name_holds_the_binomial_not_the_epithet():
    """REGRESSION: the ingest wrote 'migratorius' where MalAvi holds 'Turdus migratorius'.

    18,473 of the 18,493 seeded host records have a SPECIES_NAME beginning with their
    GENUS_NAME. Writing the bare epithet made derive_summary count one bird as two host
    species (inflating SUM_HOST), put an epithet in the published host-species list, and
    was invisible to the edition report because _host_binomial rebuilds the same string
    from either form.
    """
    assert store_ingest.split_host_species("Turdus migratorius") == (
        "Turdus", "Turdus migratorius")


def test_a_subspecies_is_still_not_absorbed_into_the_species():
    """The third word belongs in HostSubspecies, which has its own column."""
    assert store_ingest.split_host_species("Turdus migratorius propinquus") == (
        "Turdus", "Turdus migratorius")


def test_a_genus_with_no_epithet_leaves_the_binomial_empty():
    """MalAvi holds genus-only records; the binomial must not be the genus repeated."""
    assert store_ingest.split_host_species("Sphenisciformes") == ("Sphenisciformes", "")
    assert store_ingest.split_host_species("") == ("", "")


def test_a_lower_case_name_cannot_slip_past_the_collision_refusal():
    """REGRESSION: the screen normalized lineage names and the ingest did not, so a
    submitter typing 'tumig19' defeated both the agreed rename and the refusal, and the
    store gained a second lineage differing only in case."""
    store = _store(_row("lineages", LINEAGE_NAME="TUMIG19", GENBANK_ACC="KF314763",
                        SEQUENCE="ACGT", _source="seed"))
    incoming_rows = [_row("lineages", LINEAGE_NAME="tumig19", GENBANK_ACC="PQ118836",
                          SEQUENCE="ACGA", _source=MINE)]
    messages = store_ingest.colliding_lineages(store, incoming_rows, MINE)
    assert messages, "a case variant of a held name must still be refused"
    assert "TUMIG19 is already a lineage in MalAvi" in messages[0]


def test_a_rename_applies_to_a_case_variant_of_the_agreed_name():
    tables = {"lineages": [_row("lineages", LINEAGE_NAME="tumig10")]}
    renamed, notes = store_ingest.apply_name_corrections(tables, {"TUMIG10": "TUMIG32"})
    assert renamed["lineages"][0]["LINEAGE_NAME"] == "TUMIG32"
    assert notes


def test_the_workbook_reader_normalizes_every_lineage_name(tmp_path):
    """Read through the real workbook path, not the helpers: a name typed with a space
    or in lower case must reach the store in MalAvi's casing, or it becomes a new lineage
    and the FASTA id truncates at the space."""
    path = full_workbook(
        tmp_path,
        new_lineages=[["sgs 1", "PZ000001", "Haemoproteus", "", "Ellis 2027", ""]],
        sequences=[["SGS 1", WINDOW]])
    tables, _ = store_ingest.tables_from_workbook(
        path, "MALAVI-SUB-2026-000123", "2026-09-01", EXISTING)
    assert tables["lineages"][0]["LINEAGE_NAME"] == "SGS1"
    # And the sequence still joins, though the two sheets spelled the name differently.
    assert tables["lineages"][0]["SEQUENCE"] == WINDOW
