# @title Tests for supplementary-table extraction
# @purpose Lock in the column-mapping and block-handling behaviour that the
#          MalAvi ground-truth benchmark found broken on 2026-07-28.
# @why Each test below corresponds to a specific wrong result measured on a real
#      curated paper, so a regression would silently corrupt curation records.
# @input none (fixtures are built in-memory or written to tmp_path)
# @output none (assertions)
# @program pytest
"""Tests for malavi_curation.table_extract.

Every case here is drawn from a real supplementary file in the ground-truth
corpus, because each one produced a concrete wrong answer before the fix:

* Fecchio et al 2023b -- wide format, ``Lineage.N.{Genus,Name,Accession}``
* McNew et al 2021 -- a metadata sheet, plus a decoy "…per host" column
* Perrin et al 2026 -- four tables in one .docx, records only in the second
"""
from __future__ import annotations

import csv

import pytest

from malavi_curation.table_extract import (
    _column_values_fit,
    _match_columns,
    _rows_from_matrix,
    extract_table_file,
)


# --- Column mapping --------------------------------------------------------

def test_compound_header_assigns_by_rightmost_token():
    """``Lineage.1.Genus`` is a genus column, not the lineage column.

    Before the fix, "lineage" won on all three columns because it was checked
    first, so every extracted record carried the parasite genus abbreviation
    ("PA", "HA", "LE") as its lineage name.
    """
    header = ["Host.Latin.Name", "Country", "Genus.for.all",
              "Lineage.1.Genus", "Lineage.1.Name", "Lineage.1.Accession.."]
    mapping = _match_columns(header)

    assert mapping["lineage_name"] == 4      # Lineage.1.Name
    assert mapping["accession"] == 5         # Lineage.1.Accession..
    assert mapping["host_species"] == 0      # Host.Latin.Name
    assert mapping["country"] == 1
    # The dedicated genus column beats the one embedded in a lineage group.
    assert mapping["parasite_genus"] == 2


def test_repeated_groups_take_the_first_occurrence():
    """With five co-infection slots, the lineage column is slot 1, not slot 5."""
    header = ["Host.Latin.Name",
              "Lineage.1.Name", "Lineage.2.Name", "Lineage.3.Name"]
    assert _match_columns(header)["lineage_name"] == 1


def test_separators_are_normalized():
    """Dots and underscores are word separators, so "host_species" matches."""
    assert _match_columns(["host_species", "lineage_name"]) == {
        "host_species": 0, "lineage_name": 1}


def test_more_specific_header_wins():
    """A bare "Host" loses to an explicit "Host species" column."""
    mapping = _match_columns(["Host", "Host species"])
    assert mapping["host_species"] == 1


def test_bare_species_column_is_the_host():
    """Bukauskaitė et al 2024 names its host column simply "Species"."""
    mapping = _match_columns(["No.", "Year", "Species", "Parasite", "Genetic lineage"])
    assert mapping["host_species"] == 2
    assert mapping["lineage_name"] == 4


@pytest.mark.parametrize("parasite_header", [
    "Parasite species", "Haemoproteus species", "Vector species",
    "Mosquito species", "Culicoides species",
])
def test_parasite_and_vector_columns_never_become_the_host(parasite_header):
    """A column labelled parasite or vector is not the avian host.

    No value check could catch this: a parasite binomial looks exactly like a
    host binomial, so the header has to be trusted to exclude it.
    """
    mapping = _match_columns([parasite_header, "Species"])
    assert mapping["host_species"] == 1


# --- Value-shape validation ------------------------------------------------

def test_column_rejected_when_values_do_not_fit_the_field():
    """"Sequence number per host" matches the header synonym but holds integers."""
    matrix = [["Sequence number per host"], ["1"], ["2"], ["3"], ["1"]]
    assert not _column_values_fit(matrix, 1, 0, "host_species")


def test_column_accepted_when_values_are_binomials():
    matrix = [["Taxon"], ["Columbina talpacoti"], ["Myrmotherula longipennis"]]
    assert _column_values_fit(matrix, 1, 0, "host_species")


def test_unshaped_fields_always_pass():
    """Only fields with a declared shape are value-checked; others pass through."""
    matrix = [["Site"], ["anything at all"], ["!!"]]
    assert _column_values_fit(matrix, 1, 0, "site")


def test_host_falls_through_to_the_column_whose_values_fit():
    """The decoy column is rejected and "Taxon" is found instead.

    This is the McNew et al 2021 layout: the header-synonym match ("…per host")
    is wrong and the real host column ("Taxon") matches only the weakest synonym.
    """
    matrix = [
        ["Haplotype", "Sequence number per host", "Taxon", "Locality"],
        ["T009", "1", "Columbina talpacoti", "Alerta"],
        ["T262", "1", "Myrmotherula longipennis", "Alerta"],
        ["T209", "2", "Automolus ochrolaemus", "Alerta"],
    ]
    parsed, columns = _rows_from_matrix(matrix)

    assert columns["host_species"] == "Taxon"
    assert parsed.records[0]["host_species"] == "Columbina talpacoti"
    assert parsed.records[0]["lineage_name"] == "T009"


# --- Blocks: sheets, documents and documentation ---------------------------

def test_metadata_sheet_is_skipped(tmp_path):
    """A ``Label | Contents`` data dictionary must not become records.

    McNew's "Dataset S1 metadata" sheet produced records such as
    ``DEPARTMENT x IN PERU`` because a row like ``Haplotype | Haplotype of the
    host…`` maps two fields and so looked like a header.
    """
    openpyxl = pytest.importorskip("openpyxl")

    workbook = openpyxl.Workbook()
    meta = workbook.active
    meta.title = "Dataset S1 metadata"
    for row in [["Label", "Contents"],
                ["Haplotype", "Haplotype of the host specimen"],
                ["Department", "Department in Peru"]]:
        meta.append(row)

    data = workbook.create_sheet("Dataset S1")
    for row in [["Haplotype", "Taxon"],
                ["T009", "Columbina talpacoti"],
                ["T262", "Myrmotherula longipennis"]]:
        data.append(row)

    path = tmp_path / "supplement.xlsx"
    workbook.save(path)

    extracted = extract_table_file(path)
    hosts = {row["host_species"] for row in extracted.rows}
    assert hosts == {"Columbina talpacoti", "Myrmotherula longipennis"}

    skipped = [b for b in extracted.blocks if b.get("skipped")]
    assert len(skipped) == 1
    assert skipped[0]["block"] == "Dataset S1 metadata"


def test_rows_are_unioned_across_sheets(tmp_path):
    """Records in a later sheet are reachable, not shadowed by an earlier one."""
    openpyxl = pytest.importorskip("openpyxl")

    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "Screening totals"
    for row in [["bird species", "infected", "screened"],
                ["Batis molitor", "1", "1"]]:
        first.append(row)

    second = workbook.create_sheet("Interactions")
    for row in [["lineage name", "bird species"],
                ["AFR120", "Turtur chalcospilos"],
                ["CRIATR01", "Crithagra atrogularis"]]:
        second.append(row)

    path = tmp_path / "two_sheets.xlsx"
    workbook.save(path)

    extracted = extract_table_file(path)
    lineages = {row["lineage_name"] for row in extracted.rows
                if row.get("lineage_name")}
    assert lineages == {"AFR120", "CRIATR01"}


def test_csv_round_trip(tmp_path):
    """The single-block path still works for plain delimited files."""
    path = tmp_path / "rows.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Lineage", "Host species", "Country"])
        writer.writerow(["CYACYA05", "Cyanocompsa cyanoides", "Brazil"])

    extracted = extract_table_file(path)
    assert len(extracted.rows) == 1
    assert extracted.rows[0]["lineage_name"] == "CYACYA05"
    assert extracted.rows[0]["country"] == "Brazil"


def test_unsupported_suffix_raises(tmp_path):
    path = tmp_path / "notes.rtf"
    path.write_text("irrelevant")
    with pytest.raises(ValueError):
        extract_table_file(path)


# --- The association rule --------------------------------------------------
#
# Added 2026-07-29, when carving tables out of PDFs began proposing blocks that
# were really running prose. Each test below is a shape that produced a wrong
# record on a real corpus paper before the rule existed.

def test_prose_block_yields_no_records():
    """Two columns of body text are not a table of records.

    `pdftotext -layout` renders a two-column journal page as lines with a wide
    gap down the middle, which is structurally identical to a two-column table.
    On Schmid et al 2017a this produced 77 "records" whose host species were
    sentence fragments.
    """
    matrix = [
        ["Bird species were sampled at", "GenBank and thus considered"],
        ["Anopheles mascarensis, Culex", "novel. The sequence was 95%"],
        ["eles, Coquillettidia, Lutzia", "homologous to the previously"],
    ]
    parsed, _ = _rows_from_matrix(matrix)
    assert parsed.records == []


def test_row_without_a_lineage_is_not_a_record():
    """A host with no lineage beside it is a mention, not an association."""
    matrix = [
        ["Lineage", "Host species"],
        ["AFR120", "Turtur chalcospilos"],
        [None, "Quelea quelea"],
    ]
    parsed, _ = _rows_from_matrix(matrix)
    assert [r["host_species"] for r in parsed.records] == ["Turtur chalcospilos"]


def test_mosquito_column_is_never_read_as_the_avian_host():
    """Kim & Tsuda 2012 heads its mosquito column "Species".

    That matches the deliberately weak bare-"Species" host synonym, and the
    values are well-formed binomials, so only the genus vocabulary can reject it.
    """
    matrix = [
        ["Species", "No. examined", "Lineage"],
        ["Culex pipiens pallens", "1146", "PADOM02"],
        ["Aedes albopictus", "62", "CXQUI01"],
        ["Armigeres subalbatus", "2", "GALLUS01"],
    ]
    parsed, columns = _rows_from_matrix(matrix)
    assert "host_species" not in columns
    assert parsed.records == []


def test_vector_table_yields_vector_rows_and_its_accessions():
    """Perrin et al 2026's Supplementary Table S4, which has no avian host.

    Before vector parsing existed this block was dropped whole, taking 21
    accessions with it and dragging that paper's accession recall to 67%.
    """
    matrix = [
        ["parasite genus", "lineage name", "mosquito species", "GenBank number"],
        ["Haemoproteus", "CULPIP03", "Culex pipiens", "PX925004"],
        ["Haemoproteus", "PARUS1", "Mansonia africana", "AF254977"],
    ]
    parsed, _ = _rows_from_matrix(matrix)

    assert parsed.records == []
    assert [v["vector_species"] for v in parsed.vectors] == [
        "Culex pipiens", "Mansonia africana"]
    assert parsed.accessions == ["PX925004", "AF254977"]


# --- Value normalization ---------------------------------------------------

def test_printed_lineage_loses_its_genus_prefix():
    """Papers print ``lBUBT3``; MalAvi stores ``BUBT3``.

    Checked against the 2026-03-23 release: no MalAvi lineage name begins with a
    lowercase letter, so the strip is unambiguous. Without it, every lineage read
    out of Harl et al 2026's printed Table 1 failed to join.
    """
    matrix = [
        ["ID", "Host species", "MalAvi lineage", "Country"],
        ["AH0822", "Buteo buteo", "lBUBT3", "Austria"],
        ["AH0167", "Accipiter nisus", "hACCNIS07", "Austria"],
        ["AH0391", "Fringilla coelebs", "CCF3", "Austria"],
    ]
    parsed, _ = _rows_from_matrix(matrix)
    assert [r["lineage_name"] for r in parsed.records] == ["BUBT3", "ACCNIS07", "CCF3"]


def test_absent_markers_do_not_become_values():
    """A literal "None"/"NA"/"—" cell is empty, not a lineage name."""
    matrix = [
        ["Lineage", "Host species"],
        ["CYACYA05", "Cyanocompsa cyanoides"],
        ["None", "Turtur chalcospilos"],
        ["—", "Quelea quelea"],
    ]
    parsed, _ = _rows_from_matrix(matrix)
    assert [r["lineage_name"] for r in parsed.records] == ["CYACYA05"]


# --- Wide "slot" layouts ---------------------------------------------------

def test_every_coinfection_slot_becomes_its_own_record():
    """Fecchio et al 2023b records up to five co-infecting lineages per bird.

    Only slot 1 was ever read, which capped that paper's recall at 71%.
    """
    matrix = [
        ["Host.Latin.Name", "Country",
         "Lineage.1.Genus", "Lineage.1.Name", "Lineage.1.Accession..",
         "Lineage.2.Genus", "Lineage.2.Name", "Lineage.2.Accession.."],
        ["Cyanocompsa cyanoides", "Brazil",
         "PA", "CYACYA05", "KU562119", "PA", "CYACYA02", "KU562120"],
        ["Phaethornis malaris", "Brazil",
         "PL", "PHAMAL02", "KU562250", "None", "None", "None"],
    ]
    parsed, _ = _rows_from_matrix(matrix)

    assert [(r["lineage_name"], r["host_species"], r["country"]) for r in parsed.records] == [
        ("CYACYA05", "Cyanocompsa cyanoides", "Brazil"),
        ("CYACYA02", "Cyanocompsa cyanoides", "Brazil"),
        ("PHAMAL02", "Phaethornis malaris", "Brazil"),
    ]


def test_an_incidental_number_in_a_header_is_not_a_slot():
    """One numbered header does not make a wide layout."""
    matrix = [
        ["Lineage", "Host species", "Site 1 name"],
        ["AFR120", "Turtur chalcospilos", "Maun"],
        ["GRW09", "Quelea quelea", "Maun"],
    ]
    parsed, columns = _rows_from_matrix(matrix)
    assert len(parsed.records) == 2
    assert columns["site"] == "Site 1 name"


# --- Counts ----------------------------------------------------------------

def test_counts_are_read_when_the_column_holds_integers():
    matrix = [
        ["Lineage", "Host species", "No. examined", "No. infected"],
        ["CCF3", "Fringilla coelebs", "20", "7"],
        ["CCF6", "Fringilla coelebs", "20", "10"],
    ]
    parsed, _ = _rows_from_matrix(matrix)
    assert parsed.records[0]["number_tested"] == "20"
    assert parsed.records[0]["number_found"] == "7"


def test_a_percentage_column_is_not_a_count():
    """"No. positive pools (%)" holds "11 (13)", which is not a number found.

    Inventing prevalence is a precision failure the benchmark checks explicitly,
    so counts are held to a 90% integer threshold rather than the usual 30%.
    """
    matrix = [
        ["Lineage", "Host species", "No. positive pools (%)"],
        ["CCF3", "Fringilla coelebs", "11 (13)"],
        ["CCF6", "Fringilla coelebs", "4 (22)"],
    ]
    parsed, columns = _rows_from_matrix(matrix)
    assert "number_found" not in columns
    assert parsed.records[0].get("number_found") is None


# --- Co-infection cells: several lineages in one cell ----------------------

def test_coinfection_cell_becomes_one_record_per_lineage():
    """Harl et al 2026's Table S1 lists a bird's lineages in a single cell.

    Its "MalAvi lineages only" column reads ``lBUTBUT03, lBUBT3, hBUBT1`` -- three
    real associations for that bird. Kept whole, the cell became the nonexistent
    lineage ``BUTBUT03,LBUBT3,HBUBT1``: one false positive *and* three missed true
    positives, so a co-infection cell costs twice.
    """
    matrix = [
        ["IndID", "Host (Species)", "Country", "MalAvi lineages only"],
        ["AH0025", "Buteo buteo", "Austria", "lBUTBUT03, lBUBT3, hBUBT1"],
        ["AH0111", "Clanga pomarina", "Austria", "lCLAPOM02, lCLAPOM03"],
        ["AH0822", "Buteo buteo", "Austria", "lBUBT3"],
    ]
    parsed, _ = _rows_from_matrix(matrix)

    assert [(r["lineage_name"], r["host_species"]) for r in parsed.records] == [
        ("BUTBUT03", "Buteo buteo"),
        ("BUBT3", "Buteo buteo"),
        ("BUBT1", "Buteo buteo"),
        ("CLAPOM02", "Clanga pomarina"),
        ("CLAPOM03", "Clanga pomarina"),
        ("BUBT3", "Buteo buteo"),
    ]
    # Every split row keeps the cell it came out of, so the curator sees the
    # co-infection rather than a bare name with no context.
    assert parsed.records[0]["lineage_name_source"] == "BUTBUT03, lBUBT3, hBUBT1"
    # A single-lineage cell is not a split, so it carries no source annotation.
    assert parsed.records[5].get("lineage_name_source") is None


def test_the_rest_of_the_row_is_copied_onto_every_split_lineage():
    """A co-infection is several parasites in ONE bird: the sample is shared."""
    matrix = [
        ["Lineage", "Host species", "Country", "Site", "Number tested"],
        ["lCIAE03, pTURDUS1", "Circus aeruginosus", "Austria", "Vienna", "12"],
    ]
    parsed, _ = _rows_from_matrix(matrix)

    assert len(parsed.records) == 2
    for record in parsed.records:
        assert record["host_species"] == "Circus aeruginosus"
        assert record["country"] == "Austria"
        assert record["site"] == "Vienna"
        assert record["number_tested"] == "12"
    assert [r["lineage_name"] for r in parsed.records] == ["CIAE03", "TURDUS1"]


@pytest.mark.parametrize("cell", [
    "L. toddi L2 (lBUBT2), L. toddi L3 (lBUBT3)",   # morphospecies + parenthetical
    "BUBT2, not a lineage",                          # one part is prose
    "MIX",                                           # a category word
    "12, 15",                                        # bare numbers
    "Plasmodium spp., Haemoproteus spp.",            # genus names, not lineages
])
def test_a_cell_that_is_not_a_plain_list_is_left_alone(cell):
    """The split fires only when EVERY part looks like a lineage name.

    Anything else is a cell whose structure we do not understand, and guessing at
    it would invent lineage names. It passes through untouched for the curator.
    """
    matrix = [
        ["Lineage", "Host species"],
        [cell, "Buteo buteo"],
    ]
    parsed, _ = _rows_from_matrix(matrix)
    assert len(parsed.records) <= 1
    for record in parsed.records:
        assert record.get("lineage_name_source") is None


def test_a_repeated_lineage_in_one_cell_is_not_duplicated():
    matrix = [
        ["Lineage", "Host species"],
        ["lBUBT3, hBUBT1, lBUBT3", "Buteo buteo"],
    ]
    parsed, _ = _rows_from_matrix(matrix)
    assert [r["lineage_name"] for r in parsed.records] == ["BUBT3", "BUBT1"]
