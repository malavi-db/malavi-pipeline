"""Tests for the unpublished-reference convention and the rename that ends it.

Two things have to hold for MalAvi to accept records before publication.

The first is that "unpublished" is recognizable. It is spelled into the reference name
and nowhere else — there is no flag column — so a helper that misses one spelling makes
that study invisible to every curator who filters on it.

The second is that publishing a study does not silently merge it with another one.
REFERENCE_NAME is part of the natural key of every record table, so a rename changes row
identity, and the seed store already contains rows sharing a key. A collision test that
cannot tell a pre-existing duplicate from one the rename just created is worse than no
test: it fires on every rename, and then it gets ignored.
"""
from __future__ import annotations

import csv
import importlib.util
import re
from pathlib import Path

import pytest

from malavi_curation import reference_names
from malavi_curation.release_store import TABLES

REPO = Path(__file__).resolve().parents[2]
STORE = REPO / "data" / "records"
SPEC = TABLES["host_records"]


def _load_publish_reference():
    """Import curation/publish_reference.py, which is a script rather than a module."""
    path = REPO / "curation" / "publish_reference.py"
    spec = importlib.util.spec_from_file_location("publish_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(lineage, species, site, reference):
    row = {column: "" for column in SPEC.columns}
    row.update({"LINEAGE_NAME": lineage, "SPECIES_NAME": species,
                "SITE_NAME": site, "REFERENCE_NAME": reference})
    return row


class TestRecognition:
    @pytest.mark.parametrize("name", [
        "Barrow et al unpubl",            # the ordinary case
        "Marzal unpubl",                  # one author
        "Witt & McNew unpubl",            # two, joined with &
        "Rubenstein, Ellis and Ricklefs unpubl",   # three, joined with commas and "and"
        "Dimitar Dimitrov unpubl",        # a full personal name
        "Rojo et al unpubl b",            # letter disambiguator
        "Hellgren et al unpubl 3",        # numeric disambiguator
    ])
    def test_every_shape_in_the_seed_is_recognized(self, name):
        assert reference_names.is_unpublished(name)
        assert reference_names.problem_with(name) is None

    @pytest.mark.parametrize("name", [
        "Bensch et al 2000", "Fecchio et al 2019a", "Hellgren 2005",
    ])
    def test_published_citations_are_left_alone(self, name):
        assert not reference_names.is_unpublished(name)
        assert reference_names.problem_with(name) is None

    def test_the_misspelling_is_recognized_but_reported(self):
        """'unpub' has to count as unpublished AND be flagged.

        Counting it is what stops a curator's filter missing those rows today; flagging
        it is what stops the second spelling spreading into new submissions.
        """
        assert reference_names.is_unpublished("Romano et al unpub")
        assert "unpubl" in (reference_names.problem_with("Romano et al unpub") or "")
        assert reference_names.canonical("Romano et al unpub") == "Romano et al unpubl"

    def test_canonical_preserves_a_disambiguator(self):
        assert reference_names.canonical("Rojo et al unpub b") == "Rojo et al unpubl b"

    def test_canonical_leaves_a_correct_name_untouched(self):
        assert reference_names.canonical("Barrow et al unpubl") == "Barrow et al unpubl"

    def test_the_marker_outside_the_convention_is_flagged(self):
        assert reference_names.problem_with("Unpubl data from Barrow") is not None

    def test_authors_are_recoverable(self):
        assert reference_names.authors_of("Barrow et al unpubl") == "Barrow et al"
        assert reference_names.authors_of("Bensch et al 2000") == ""


class TestAgainstTheRealStore:
    """The convention is a description of MalAvi's own data, so check it against them."""

    def _names_citing_unpubl(self):
        found = set()
        for table in ("host_records", "vector_records", "alt_names", "morpho_species"):
            path = STORE / f"{table}.csv"
            if not path.is_file():
                pytest.skip(f"{path} is not present")
            with open(path, newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    name = (row.get("REFERENCE_NAME") or "").strip()
                    if re.search(r"unpub", name, re.I):
                        found.add(name)
        return found

    def test_every_unpublished_name_in_the_store_is_recognized(self):
        names = self._names_citing_unpubl()
        assert names, "expected the seed store to contain unpublished references"
        missed = sorted(n for n in names if not reference_names.is_unpublished(n))
        assert not missed, f"unrecognized unpublished references: {missed}"

    def test_no_published_reference_is_mistaken_for_an_unpublished_one(self):
        path = STORE / "references.csv"
        if not path.is_file():
            pytest.skip(f"{path} is not present")
        with open(path, newline="", encoding="utf-8") as handle:
            published = [(row.get("REFERENCE_NAME") or "").strip()
                         for row in csv.DictReader(handle)]
        wrong = [n for n in published if reference_names.is_unpublished(n)]
        assert not wrong, f"published citations read as unpublished: {wrong}"

    def test_unpublished_studies_have_no_reference_row(self):
        """The convention's other half, asserted so a future import cannot quietly end it.

        An unpublished study has no year, title, journal or pages. A reference row for one
        would either be mostly blank or invented, and MalAvi's answer has always been to
        have no row at all.
        """
        path = STORE / "references.csv"
        if not path.is_file():
            pytest.skip(f"{path} is not present")
        with open(path, newline="", encoding="utf-8") as handle:
            rows = [(row.get("REFERENCE_NAME") or "").strip()
                    for row in csv.DictReader(handle)]
        assert not [n for n in rows if re.search(r"unpub", n, re.I)]


class TestRenameCollisions:
    def setup_method(self):
        self.collisions = _load_publish_reference().collisions

    def test_a_renamed_row_landing_on_another_study_is_a_collision(self):
        rows = [_row("BT7", "Parus major", "Lund", "X et al unpubl"),
                _row("BT7", "Parus major", "Lund", "X et al 2027")]
        assert self.collisions(SPEC, rows, "X et al unpubl", "X et al 2027")

    def test_a_duplicate_inside_the_renamed_study_is_not_a_collision(self):
        """It was already a duplicate; the rename neither created nor worsened it.

        This is the case that made the first version of the check useless — it reported
        thousands of pre-existing duplicates and refused every rename.
        """
        rows = [_row("BT7", "Parus major", "Lund", "X et al unpubl"),
                _row("BT7", "Parus major", "Lund", "X et al unpubl")]
        assert not self.collisions(SPEC, rows, "X et al unpubl", "X et al 2027")

    def test_a_duplicate_in_an_unrelated_study_is_not_a_collision(self):
        rows = [_row("BT7", "Parus major", "Lund", "Other et al 2001"),
                _row("BT7", "Parus major", "Lund", "Other et al 2001"),
                _row("AB1", "Sylvia borin", "Kvismaren", "X et al unpubl")]
        assert not self.collisions(SPEC, rows, "X et al unpubl", "X et al 2027")

    def test_a_clean_rename_reports_nothing(self):
        rows = [_row("AB1", "Sylvia borin", "Kvismaren", "X et al unpubl"),
                _row("BT7", "Parus major", "Lund", "Other et al 2001")]
        assert not self.collisions(SPEC, rows, "X et al unpubl", "X et al 2027")


# =====================================================================================
# Publishing a reference lifts the embargo on the submissions behind it.
#
# The submitter asked us to hold their records until the study was out; renaming
# "<Authors> unpubl" to a real citation is the moment it is out. Leaving the flag set
# would keep the release gate refusing records whose paper is on a shelf.
# =====================================================================================

import importlib.util as _importlib_util

from malavi_curation import ledger as _ledger
from malavi_curation.config import repo_root as _repo_root


@pytest.fixture(scope="module")
def publish_reference():
    path = _repo_root() / "curation" / "publish_reference.py"
    spec = _importlib_util.spec_from_file_location("_publish_reference", path)
    module = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tables(*sources, name="Ellis et al unpubl"):
    return {"host_records": [{"REFERENCE_NAME": name, "_source": s} for s in sources]}


def test_the_submissions_are_found_through_provenance(publish_reference):
    """No separate mapping, and nothing for a curator to remember: _source already says."""
    tables = _tables("MALAVI-SUB-2026-000123", "MALAVI-SUB-2026-000123",
                     "MALAVI-SUB-2026-000456")
    assert publish_reference.submissions_behind(tables, "Ellis et al unpubl") == [
        "MALAVI-SUB-2026-000123", "MALAVI-SUB-2026-000456"]


def test_seed_rows_carry_no_submission(publish_reference):
    """A study can have both: some records from the old release, some by submission."""
    tables = _tables("seed", "MALAVI-SUB-2026-000123")
    assert publish_reference.submissions_behind(tables, "Ellis et al unpubl") == [
        "MALAVI-SUB-2026-000123"]


def test_rows_citing_another_study_are_ignored(publish_reference):
    tables = {"host_records": [
        {"REFERENCE_NAME": "Ellis et al unpubl", "_source": "MALAVI-SUB-2026-000123"},
        {"REFERENCE_NAME": "Someone else 2020", "_source": "MALAVI-SUB-2026-000999"}]}
    assert publish_reference.submissions_behind(tables, "Ellis et al unpubl") == [
        "MALAVI-SUB-2026-000123"]


def test_a_dry_run_lifts_nothing(publish_reference, tmp_path):
    """The preview must not write. It runs before the rename is on disk."""
    entry = _ledger.ensure_entry({}, "MALAVI-SUB-2026-000123", "A",
                                 "2026-08-01T00:00:00+00:00")
    entry.embargoed = True
    _ledger.save(tmp_path, {entry.submission_id: entry})

    lines = publish_reference.lift_embargoes(tmp_path, [entry.submission_id],
                                             "Ellis et al 2027", apply=False)
    assert "would lift" in lines[0]
    assert _ledger.load(tmp_path)[entry.submission_id].embargoed is True


def test_applying_lifts_the_embargo(publish_reference, tmp_path):
    entry = _ledger.ensure_entry({}, "MALAVI-SUB-2026-000123", "A",
                                 "2026-08-01T00:00:00+00:00")
    entry.embargoed = True
    _ledger.save(tmp_path, {entry.submission_id: entry})

    lines = publish_reference.lift_embargoes(tmp_path, [entry.submission_id],
                                             "Ellis et al 2027", apply=True)
    assert "embargo lifted" in lines[0]
    reloaded = _ledger.load(tmp_path)[entry.submission_id]
    assert reloaded.embargoed is False
    # Recorded explicitly, so the nightly intake does not re-impose the form's old answer.
    assert _ledger.embargo_decided(reloaded)


def test_a_missing_ledger_is_reported_not_raised(publish_reference, tmp_path):
    """The rename is the substance; a ledger problem must not undo work already done."""
    lines = publish_reference.lift_embargoes(tmp_path, ["MALAVI-SUB-2026-000123"],
                                             "Ellis et al 2027", apply=True)
    assert "no review ledger" in lines[0]


def test_a_submission_that_was_never_embargoed_says_so(publish_reference, tmp_path):
    entry = _ledger.ensure_entry({}, "MALAVI-SUB-2026-000123", "A",
                                 "2026-08-01T00:00:00+00:00")
    _ledger.save(tmp_path, {entry.submission_id: entry})
    lines = publish_reference.lift_embargoes(tmp_path, [entry.submission_id],
                                             "Ellis et al 2027", apply=True)
    assert "was not embargoed" in lines[0]
