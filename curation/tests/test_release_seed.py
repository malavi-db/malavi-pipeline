"""Tests for seeding the canonical record store from the last external release.

The seed happens once, and after it the store — not the release ZIP — is the authoritative
MalAvi. So the round-trip verifier is the only thing standing between a faithful migration
and silent, unrecoverable loss of scientific records presented as a verified one. Both
tests below are regressions from the independent review of 2026-08-06.
"""
import shutil

import pytest


# ---------------------------------------------------------------------------------
# Regressions from the independent review of 2026-08-06. Both are ways the seed could
# lose scientific data while reporting a clean migration -- the worst available
# outcome for the one gate between "the release is the database" and "the store is".
# ---------------------------------------------------------------------------------

def test_a_missing_source_table_is_not_a_clean_round_trip(tmp_path):
    """REGRESSION: a missing CSV was skipped, so an EMPTY table verified as clean."""
    from malavi_curation.release_seed import seed_store, verify_round_trip
    from malavi_curation.config import repo_root

    downloads = repo_root() / "docs" / "assets" / "downloads" / "tables"
    if not downloads.is_dir():
        pytest.skip("release tables are not in this checkout")
    release = "2026-03-23"

    # Copy the release aside, then remove one table, exactly as a botched checkout would.
    staged = tmp_path / "tables"
    staged.mkdir()
    copied = list(downloads.glob(f"*{release}.csv"))
    if not copied:
        pytest.skip(f"no {release} tables in this checkout")
    for src in copied:
        shutil.copy(src, staged / src.name)
    victim = staged / f"vector_data_{release}.csv"
    if not victim.is_file():
        pytest.skip("vector_data table not present to remove")
    victim.unlink()

    store, report = seed_store(staged, release)
    assert report["missing"], "the seed should record the missing source"
    assert store["vector_records"] == [], "the store table is empty, as expected"

    check = verify_round_trip(staged, release, store)
    assert not check["clean"], \
        "an empty authoritative table must never verify as a clean round trip"
    assert check["missing"]


def test_a_renamed_column_is_refused_rather_than_blanked(tmp_path):
    """REGRESSION: a column absent from the source read as "" on BOTH sides and matched."""
    from malavi_curation.release_seed import seed_store, TABLES, _SOURCE_FILES
    from malavi_curation.config import repo_root

    downloads = repo_root() / "docs" / "assets" / "downloads" / "tables"
    release = "2026-03-23"
    source = downloads / f"references_{release}.csv"
    if not source.is_file():
        pytest.skip("references table is not in this checkout")

    staged = tmp_path / "tables"
    staged.mkdir()
    for src in downloads.glob(f"*{release}.csv"):
        shutil.copy(src, staged / src.name)

    # Rename one expected column in the header, as an upstream format change would.
    target = staged / f"references_{release}.csv"
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    expected = TABLES["references"].columns[0]
    lines[0] = lines[0].replace(expected, expected + "_OLD", 1)
    target.write_text("".join(lines), encoding="utf-8")

    with pytest.raises(ValueError, match="missing column"):
        seed_store(staged, release)
