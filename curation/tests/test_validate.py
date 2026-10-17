"""Test the malaviR validation bridge (validate.py -> validate_record.R).

Requires Rscript with the malaviR package; skipped otherwise so CI without R
still passes. Also checks the graceful-skip path when R is unavailable.
"""
import shutil
import subprocess

import pytest

from malavi_curation.validate import validate_submission


def _has_r_and_malavir() -> bool:
    rscript = shutil.which("Rscript")
    if not rscript:
        return False
    try:
        out = subprocess.run(
            [rscript, "-e", 'cat(requireNamespace("malaviR", quietly=TRUE))'],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return out.stdout.strip().upper().startswith("TRUE")


def test_graceful_skip_without_r(monkeypatch):
    # Force "no Rscript" -> a note is attached, no exception.
    monkeypatch.setattr("malavi_curation.validate.shutil.which", lambda _: None)
    sub = {"records": [{"host_species": "Gyps fulvus", "lineage_name": "GYPFUL01"}]}
    out = validate_submission(sub)
    assert any("validation skipped" in v for v in out["validation"])


@pytest.mark.skipif(not _has_r_and_malavir(), reason="Rscript + malaviR not available")
def test_contamination_flag_via_r():
    sub = {"records": [
        {"host_species": "Fregata magnificens", "lineage_name": "PARUS1", "country": "Brazil"},
        {"host_species": "Parus majr", "lineage_name": None},
    ]}
    out = validate_submission(sub, version="latest")
    joined = " ".join(out["validation"]).lower()
    assert "cross-contamination" in joined        # PARUS1 in a frigatebird
    assert "host name 'parus majr'" in joined     # typo flagged as unrecognized
