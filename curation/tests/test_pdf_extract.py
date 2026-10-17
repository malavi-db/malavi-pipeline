"""Tests for pdf_extract.

Real extraction is exercised against a benchmark PDF when one is available (the
PDFs are gitignored, so CI without them skips these). The accession round-trip
test is the meaningful one: extract text from a known paper and confirm we recover
an accession we know it deposited.
"""
from pathlib import Path

import pytest

pdfplumber = pytest.importorskip("pdfplumber")  # skip whole module without the 'pdf' extra

from malavi_curation.accession_mine import mine_accessions  # noqa: E402
from malavi_curation.pdf_extract import extract_pdf  # noqa: E402

PDF_DIR = Path(__file__).resolve().parents[1] / "benchmark" / "pdfs"
MARKAKIS = PDF_DIR / "Markakis et al 2025a.pdf"


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        extract_pdf("/no/such/file.pdf")


@pytest.mark.skipif(not MARKAKIS.is_file(), reason="benchmark PDF not present")
def test_extract_and_mine_known_accessions():
    doc = extract_pdf(MARKAKIS)
    assert doc.n_pages > 0
    assert len(doc.all_text()) > 1000
    hits = mine_accessions(doc.all_text().upper())
    # Markakis et al 2025a deposited the Gyps fulvus lineage accessions PV357399-401.
    for acc in ("PV357399", "PV357400", "PV357401"):
        assert acc in hits.nucleotide


# --- Carving unruled tables out of -layout text ----------------------------
#
# pdfplumber finds zero ruled tables in four of the ten ground-truth papers,
# because their tables have no ruling lines. These fixtures are trimmed verbatim
# from the `pdftotext -layout` output of those papers.

from malavi_curation.pdf_extract import carve_layout_tables  # noqa: E402


def test_carves_a_whitespace_aligned_table():
    """Harl et al 2026's Table 1, whose sub-headings have no column gaps.

    The "Leucocytozoon toddi L3 group" line is a sub-heading inside the table. It
    must not split the block in two, or only the first three data rows survive.
    """
    layout = "\n".join([
        "Table 1 Samples for mitochondrial genome analysis",
        "ID                 Host species          MalAvi lineage   Country",
        "",
        "Leucocytozoon toddi L3 group",
        "AH0249             Circus aeruginosus    lCIAE09          Austria",
        "AH0322             Accipiter nisus       lACNI08          Austria",
        "AH0822             Buteo buteo           lBUBT3           Austria",
    ])
    tables = carve_layout_tables(layout)

    assert len(tables) == 1
    caption, matrix = tables[0]
    assert caption.startswith("Table 1")
    assert matrix[0] == ["ID", "Host species", "MalAvi lineage", "Country"]
    assert matrix[-1] == ["AH0822", "Buteo buteo", "lBUBT3", "Austria"]


def test_wrapped_cells_are_folded_into_the_row_they_continue():
    """A cell too wide for its column wraps onto the next line.

    Himmel et al 2024's Table 2 does this on nearly every row, so without the
    merge each lineage name is stranded on a row with no key of its own.
    """
    layout = "\n".join([
        "Table 2 Parasites detected",
        "Bird       Collection date   Parasite species and lineages",
        "AH1965     2019-05-04        Haemoproteus",
        "                             fringillae hCCF3",
        "AH1972     2019-05-07        H. magnus hCCF09",
    ])
    tables = carve_layout_tables(layout)

    _, matrix = tables[0]
    assert matrix[1] == ["AH1965", "2019-05-04",
                         "Haemoproteus fringillae hCCF3"]
    assert matrix[2] == ["AH1972", "2019-05-07", "H. magnus hCCF09"]


def test_a_page_break_ends_a_table():
    layout = "\n".join([
        "ID       Host species          Lineage",
        "AH0249   Circus aeruginosus    lCIAE09",
        "AH0322   Accipiter nisus       lACNI08",
        "\x0cHarl et al. Parasites & Vectors      Page 5 of 16",
        "AH9999   Not part of the table lXXXX01",
    ])
    tables = carve_layout_tables(layout)

    assert len(tables) == 1
    _, matrix = tables[0]
    assert [row[0] for row in matrix] == ["ID", "AH0249", "AH0322"]


def test_a_short_run_of_aligned_lines_is_not_a_table():
    """Two lines can line up by accident; a table needs at least three."""
    layout = "Some heading\nleft column      right column\n"
    assert carve_layout_tables(layout) == []
