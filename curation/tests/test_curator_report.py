"""Tests for curator_report rendering and the end-to-end pipeline."""
from pathlib import Path

import pytest

from malavi_curation.accession_mine import AccessionHits
from malavi_curation.curator_report import render_report
from malavi_curation.hosts_geography import HostGeography
from malavi_curation.record_builder import build_submission

PDF_DIR = Path(__file__).resolve().parents[1] / "benchmark" / "pdfs"
MARKAKIS = PDF_DIR / "Markakis et al 2025a.pdf"


def test_render_report_contains_key_fields():
    acc = AccessionHits(nucleotide=["PV357399"], ranges=["PV357399-PV357401"])
    hg = HostGeography(hosts=["Gyps fulvus"], countries=["Greece"], needs_supplement=True)
    sub = build_submission({"title": "Vultures of Greece", "year": 2025},
                           accessions=acc, hostgeo=hg)
    report = render_report([sub], headings=["Markakis et al 2025a"])
    assert "Markakis et al 2025a" in report
    assert "Gyps fulvus" in report
    assert "PV357399" in report
    assert "Greece" in report
    assert "supplementary data" in report          # the needs_supplement flag
    assert "curator review required" in report


@pytest.mark.skipif(not MARKAKIS.is_file(), reason="benchmark PDF not present")
def test_pipeline_end_to_end_produces_valid_submission():
    from malavi_curation.pipeline import process_pdf

    sub = process_pdf(MARKAKIS, reference={"title": "Markakis et al 2025a"}, schema_validate=True)
    accs = set(sub["accessions"])
    # The Gyps fulvus lineage accessions this paper deposited.
    assert {"PV357399", "PV357400", "PV357401"} <= accs
    # Host names from prose are curator leads, not records: a record needs a
    # lineage attached, which prose mining cannot supply.
    assert sub["records"] == []
    assert "Gyps fulvus" in set(sub["provenance"]["candidate_hosts"])
