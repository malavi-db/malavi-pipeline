"""Genomic resources: the sheet -> rows, the vocabulary, and the cyt b check."""
from __future__ import annotations

import random

import pytest

openpyxl = pytest.importorskip("openpyxl")

from malavi_curation import genomic_resources as gr                    # noqa: E402
from malavi_curation.release_store import TABLES                      # noqa: E402
from malavi_curation.sequence_check import Reference                  # noqa: E402
from malavi_curation.template_adapter import CANONICAL_HEADERS, SHEET_GENOMES  # noqa: E402

COLS = TABLES["genomic_resources"].columns


def _book(tmp_path, rows):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(SHEET_GENOMES)
    ws.append(["an instruction note"])
    ws.append(CANONICAL_HEADERS[SHEET_GENOMES])
    for row in rows:
        ws.append(row)
    path = tmp_path / "book.xlsx"
    wb.save(path)
    return openpyxl.load_workbook(path, data_only=True)


class TestSheet:
    def test_rows_become_store_rows(self, tmp_path):
        wb = _book(tmp_path, [
            ["ARACAJ01", "Mitogenome", "PX123975", None, "5900", "near-complete mitochondrial genome",
             "Cariama cristata", "Vieira et al., 2025", "10.1000/x", "individual 2"]])
        links = gr.resource_links(wb)
        rows, notes = gr.rows_from_links(links, "MALAVI-SUB-2026-000007", "2026-10-01", COLS)
        assert notes == [] and len(rows) == 1
        r = rows[0]
        assert r["LINEAGE_NAME"] == "ARACAJ01" and r["RESOURCE_TYPE"] == "mitogenome"
        assert r["ACCESSION"] == "PX123975" and r["DATABASE"] == "GenBank" and r["LENGTH_BP"] == "5900"
        assert r["REFERENCE_NAME"] and r["DOI"] == "10.1000/x" and r["NOTES"] == "individual 2"
        assert r["SOURCE"] == "MALAVI-SUB-2026-000007" and r["_source"] == "MALAVI-SUB-2026-000007"
        assert list(r)[:len(COLS)] == list(COLS)

    def test_bad_type_or_accession_is_not_written_and_is_said(self, tmp_path):
        wb = _book(tmp_path, [
            ["SGS1", "genome-ish", "KY653811", None, None, None, None, "A 2020", None, None],
            ["SGS1", "mitogenome", "not an accession", None, None, None, None, "A 2020", None, None],
            ["", "mitogenome", "KY653811", None, None, None, None, "A 2020", None, None]])
        rows, notes = gr.rows_from_links(gr.resource_links(wb), "S", "r", COLS)
        assert rows == [] and len(notes) == 3
        assert "not one of" in notes[0] and "not a recognizable accession" in notes[1] and "no lineage" in notes[2]

    def test_duplicates_collapse(self, tmp_path):
        wb = _book(tmp_path, [
            ["SGS1", "mitogenome", "KY653811", None, None, None, None, "A 2020", None, None],
            ["SGS1", "mitogenome", "ky653811", None, None, None, None, "A 2020", None, None]])
        rows, _ = gr.rows_from_links(gr.resource_links(wb), "S", "r", COLS)
        assert len(rows) == 1 and rows[0]["ACCESSION"] == "KY653811"

    def test_a_missing_sheet_is_no_rows(self, tmp_path):
        wb = openpyxl.Workbook()
        assert gr.resource_links(wb) == []


class TestAccessionShapes:
    @pytest.mark.parametrize("acc", ["KY653811", "KY653811.1", "NC_012426", "MT513721",
                                     "PRJNA123456", "SRR1234567", "ERX1234567", "GCA_000123456.1",
                                     "JAAB01000001"])
    def test_accepted(self, acc):
        assert gr.ACCESSION_RE.match(acc)

    @pytest.mark.parametrize("acc", ["", "SGS1", "12345", "KY6538", "doi:10.1/x"])
    def test_refused(self, acc):
        assert not gr.ACCESSION_RE.match(acc)


class TestCytbCheck:
    def _ref(self):
        rng = random.Random(3)
        full = "".join(rng.choice("ACGT") for _ in range(479))
        other = full[:100] + ("A" if full[100] != "A" else "C") + full[101:]
        pads = ["".join(r.choice("ACGT") for _ in range(479)) for r in (random.Random(i) for i in range(3))]
        ref = Reference.from_sequences(["P_SGS1", "P_OTHER01", "P_A", "P_B", "P_C"], [full, other] + pads)
        return ref, full, other

    def test_a_genome_carrying_the_lineage_matches(self):
        ref, full, _ = self._ref()
        flank = "".join(random.Random(9).choice("ACGT") for _ in range(2000))
        genome = flank + full + flank[::-1]
        res = gr.cytb_check("KY000001", "SGS1", ("KY000001.1 mito", genome), ref)
        assert res.status == "matches" and res.lineage_found == "SGS1" and res.distance == 0

    def test_a_genome_carrying_another_lineage_is_reported(self):
        ref, full, other = self._ref()
        flank = "".join(random.Random(9).choice("ACGT") for _ in range(500))
        res = gr.cytb_check("KY000002", "SGS1", ("d", flank + other + flank), ref)
        assert res.status == "other_lineage" and res.lineage_found == "OTHER01"

    def test_the_reverse_strand_is_found(self):
        ref, full, _ = self._ref()
        comp = str.maketrans("ACGT", "TGCA")
        genome = ("".join(random.Random(4).choice("ACGT") for _ in range(300)) + full).translate(comp)[::-1]
        res = gr.cytb_check("KY000003", "SGS1", ("d", genome), ref)
        assert res.status == "matches"

    def test_no_barcode_and_not_found(self):
        ref, *_ = self._ref()
        assert gr.cytb_check("KY000004", "SGS1", None, ref).status == "not_found"
        assert gr.cytb_check("KY000005", "SGS1", ("d", "ACGT" * 50), ref).status == "no_barcode"
        junk = "".join(random.Random(8).choice("ACGT") for _ in range(3000))
        assert gr.cytb_check("KY000006", "SGS1", ("d", junk), ref).status == "no_barcode"
