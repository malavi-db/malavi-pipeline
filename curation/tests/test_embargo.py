"""Lifting an embargo, and the deadlock that made it impossible.

An embargoed submission is refused by ``release_gate.admissibility``, so
``ingest_submissions`` never writes its rows and the record store holds no provenance for
it. ``publish_reference`` — the only thing that lifted an embargo — looked for its
submissions in exactly that store. So the embargo could be set and never lifted, and for a
study whose records were entirely embargoed the program stopped before it got that far.

What is pinned here is that the study a submission belongs to can be established without
the store, that the lift itself works and is recorded in a way enrollment will not undo,
and that the two cases behave differently: a partly-ingested study is renamed and lifted in
one go, while an entirely embargoed one is told the correct order instead of being handed a
reference row that would break the real rename later.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

openpyxl = pytest.importorskip("openpyxl")

from malavi_curation import embargo, ledger, store_ingest
from malavi_curation.config import repo_root


@pytest.fixture(scope="module")
def lift_embargo():
    path = repo_root() / "curation" / "lift_embargo.py"
    spec = importlib.util.spec_from_file_location("_lift_embargo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIG = {"review": {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60},
          "submissions": {"inbox_dir": "inbox"}}

SUBMISSION = "MALAVI-SUB-2026-000001"
STUDY = "Barrow et al unpubl"

HOSTS_HEADER = ["LINEAGE_NAME", "HostSpecies", "HOST_SPECIES_ID", "HostSubspecies",
                "HostAge", "HostStatus", "HostEnvironment", "Country", "CountryRegion",
                "SiteName", "NUMBER_FOUND", "NUMBER_TESTED", "Reference", "COMMENT"]


def write_workbook(path, reference=STUDY, vector_reference=None):
    """A minimal ImportMalavi template citing a study, read by the shipped reader."""
    book = openpyxl.Workbook()
    hosts = book.active
    hosts.title = "Hosts_and_Sites"
    hosts.append(["The records themselves."])
    hosts.append(HOSTS_HEADER)
    hosts.append(["TUMIG19", "Turdus migratorius", "", "", "", "", "", "Brazil", "",
                  "Mata Seca", "1", "10", reference, ""])

    vectors = book.create_sheet("Vectors")
    vectors.append(["One row per vector screening."])
    vectors.append(["LINEAGE_NAME", "VectorSpecies", "VECTOR_METHOD", "Country",
                    "SiteName", "Reference"])
    if vector_reference:
        vectors.append(["TUMIG19", "Culex pipiens", "PCR", "Brazil", "Mata Seca",
                        vector_reference])

    # The sheets template_adapter.looks_like_template expects to see.
    for name, header in (("Sites", ["SITE_NAME", "Country", "LATITUDE", "LONGITUDE",
                                    "ALTITUDE(m)"]),
                         ("NewLineages", ["LINEAGE_NAME", "ParasiteGenus"]),
                         ("Sequences", ["LINEAGE_NAME", "Sequence"]),
                         ("Alt_Lineage_names", ["MalAvi_Name", "Alternative_Name",
                                                "Reference"]),
                         ("Reference", ["REFERENCE_NAME", "PUBLICATION_YEAR", "TITLE",
                                        "JOURNAL_NAME", "Volume", "StartPage", "EndPage"])):
        sheet = book.create_sheet(name)
        sheet.append([f"Instructions for {name}."])
        sheet.append(header)

    book.save(path)
    return path


@pytest.fixture
def inbox(tmp_path):
    """An intake tree with one embargoed submission whose workbook cites STUDY."""
    directory = tmp_path / "inbox"
    directory.mkdir()
    submission_dir = directory / "20260801T120000_Barrow"
    submission_dir.mkdir()
    write_workbook(submission_dir / "ImportMalavi_filled.xlsx")
    # The reverse lookup publish_reference and lift_embargo both use: the ledger is keyed
    # by the opaque id, the directory is named after the submitter.
    (directory / "submission_ids.json").write_text(
        json.dumps({"version": 1, "next": 2,
                    "ids": {submission_dir.name: {
                        "id": SUBMISSION,
                        "minted_at": "2026-08-01T12:00:00+00:00"}}}),
        encoding="utf-8")
    return directory


@pytest.fixture
def entries(inbox):
    held = {}
    entry = ledger.ensure_entry(held, SUBMISSION, "A", "2026-08-01T00:00:00+00:00")
    entry.reserved_names = ["TUMIG19"]
    entry.embargoed = True
    ledger.transition(entry, "ready_for_review", "intake", at="2026-08-01T12:00:00+00:00")
    ledger.save(inbox, held)
    return held


# ------------------------------------------------- the study, without the record store

def test_a_workbook_says_which_study_it_is(tmp_path):
    """The fact that makes the deadlock breakable: the workbook knows, from day one."""
    path = write_workbook(tmp_path / "one.xlsx")
    assert store_ingest.reference_names_in_workbook(path) == [STUDY]


def test_every_sheet_that_names_a_study_is_read(tmp_path):
    """A submission can cite two studies, and the vector sheet is not the host sheet."""
    path = write_workbook(tmp_path / "two.xlsx", vector_reference="Marzal unpubl")
    assert store_ingest.reference_names_in_workbook(path) == ["Barrow et al unpubl",
                                                              "Marzal unpubl"]


def test_an_embargoed_submission_is_found_by_its_study(inbox, entries):
    found, notes = embargo.submissions_for_reference(inbox, entries, STUDY)
    assert found == [SUBMISSION] and notes == []


def test_a_near_miss_spelling_finds_nothing(inbox, entries):
    """Names are compared exactly, as REFERENCE_NAME is everywhere else.

    Guessing here would publish somebody's unpublished data on the strength of a
    resemblance.
    """
    assert embargo.submissions_for_reference(inbox, entries, "Barrow et al unpub")[0] == []
    assert embargo.submissions_for_reference(inbox, entries, "barrow et al unpubl")[0] == []


def test_a_submission_with_no_workbook_is_reported_not_dropped(inbox, entries, tmp_path):
    """It is still a real submission whose records are being held."""
    for path in (inbox / "20260801T120000_Barrow").glob("*.xlsx"):
        path.unlink()

    found, notes = embargo.submissions_for_reference(inbox, entries, STUDY)

    assert found == []
    assert len(notes) == 1 and "cannot tell which study" in notes[0]
    assert SUBMISSION in notes[0], "it names the submission so it can be acted on by id"


def test_the_listing_shows_what_is_held(inbox, entries):
    lines = "\n".join(embargo.describe(inbox, entries))
    assert "1 submission(s) under embargo" in lines
    assert SUBMISSION in lines and STUDY in lines and "TUMIG19" in lines


def test_nothing_held_says_so(inbox, entries):
    entries[SUBMISSION].embargoed = False
    assert embargo.describe(inbox, entries) == ["No submission is under embargo."]


# ------------------------------------------------------------------ lifting it

def _workspace(lift_embargo, monkeypatch, tmp_path):
    monkeypatch.setattr(lift_embargo, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(lift_embargo, "load_config", lambda: CONFIG)


def test_lifting_by_study_releases_the_records(lift_embargo, inbox, entries, tmp_path,
                                               monkeypatch, capsys):
    _workspace(lift_embargo, monkeypatch, tmp_path)

    assert lift_embargo.main(["--reference", STUDY, "--apply"]) == 0
    output = capsys.readouterr().out

    assert "embargo lifted" in output
    reloaded = ledger.load(inbox)[SUBMISSION]
    assert reloaded.embargoed is False


def test_lifting_records_a_decision_enrollment_will_not_undo(lift_embargo, inbox, entries,
                                                             tmp_path, monkeypatch,
                                                             capsys):
    """Otherwise the next intake run re-reads the form answer and re-imposes the embargo.

    ``enrollment.apply_embargo`` consults ``embargo_decided`` for exactly this reason: a
    maintainer lifting an embargo because the submitter emailed "the paper is out" must not
    have it silently re-imposed, with the release going on withholding released records.
    """
    _workspace(lift_embargo, monkeypatch, tmp_path)
    assert ledger.embargo_decided(entries[SUBMISSION]) is False

    lift_embargo.main(["--reference", STUDY, "--apply"])
    capsys.readouterr()

    assert ledger.embargo_decided(ledger.load(inbox)[SUBMISSION]) is True


def test_a_dry_run_writes_nothing(lift_embargo, inbox, entries, tmp_path, monkeypatch,
                                  capsys):
    _workspace(lift_embargo, monkeypatch, tmp_path)

    assert lift_embargo.main(["--reference", STUDY]) == 0
    output = capsys.readouterr().out

    assert "embargo lifted" in output and "[dry-run] nothing was written" in output
    assert ledger.load(inbox)[SUBMISSION].embargoed is True


def test_an_unmatched_study_shows_what_is_actually_held(lift_embargo, inbox, entries,
                                                        tmp_path, monkeypatch, capsys):
    _workspace(lift_embargo, monkeypatch, tmp_path)

    code = lift_embargo.main(["--reference", "Someone else unpubl", "--apply"])
    output = capsys.readouterr().out

    assert code == 1
    assert "No embargoed submission cites" in output
    assert SUBMISSION in output, "the listing is offered rather than a bare refusal"
    assert ledger.load(inbox)[SUBMISSION].embargoed is True


def test_setting_an_embargo_after_the_fact(lift_embargo, inbox, entries, tmp_path,
                                           monkeypatch, capsys):
    """The submitter who filed expecting to publish, then asked us to wait."""
    _workspace(lift_embargo, monkeypatch, tmp_path)
    entries[SUBMISSION].embargoed = False
    ledger.save(inbox, entries)

    assert lift_embargo.main(["--submission", SUBMISSION, "--set", "--apply"]) == 0
    capsys.readouterr()

    assert ledger.load(inbox)[SUBMISSION].embargoed is True


def test_an_unknown_submission_is_refused(lift_embargo, inbox, entries, tmp_path,
                                          monkeypatch, capsys):
    _workspace(lift_embargo, monkeypatch, tmp_path)

    code = lift_embargo.main(["--submission", "MALAVI-SUB-2026-999999", "--apply"])

    assert code == 2
    assert "no submission" in capsys.readouterr().err


def test_a_released_submission_cannot_be_embargoed(lift_embargo, inbox, entries, tmp_path,
                                                   monkeypatch, capsys):
    """Its records are published; the ledger cannot un-publish what people downloaded."""
    _workspace(lift_embargo, monkeypatch, tmp_path)
    entries[SUBMISSION].embargoed = False
    entries[SUBMISSION].state = "released"
    ledger.save(inbox, entries)

    lift_embargo.main(["--submission", SUBMISSION, "--set", "--apply"])
    output = capsys.readouterr().out

    assert "REFUSED" in output
    assert ledger.load(inbox)[SUBMISSION].embargoed is False


def test_the_listing_is_what_you_get_with_no_target(lift_embargo, inbox, entries, tmp_path,
                                                    monkeypatch, capsys):
    _workspace(lift_embargo, monkeypatch, tmp_path)

    assert lift_embargo.main([]) == 0
    output = capsys.readouterr().out

    assert "under embargo" in output and SUBMISSION in output
    assert ledger.load(inbox)[SUBMISSION].embargoed is True, "listing writes nothing"


# --------------------------------------------- publish_reference, the two cases

@pytest.fixture(scope="module")
def publish_reference():
    path = repo_root() / "curation" / "publish_reference.py"
    spec = importlib.util.spec_from_file_location("_publish_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_store(root, host_rows=()):
    """A record store with the six tables, holding only what a test puts in it."""
    from malavi_curation.release_store import TABLES, write_table

    directory = root / "data" / "records"
    directory.mkdir(parents=True, exist_ok=True)
    for name, spec in TABLES.items():
        rows = list(host_rows) if name == "host_records" else []
        write_table(directory, spec, [{column: row.get(column, "") for column in spec.columns}
                                      for row in rows])
    return directory


def _publish_workspace(publish_reference, monkeypatch, tmp_path, inbox):
    # publish_reference finds both the store and the inbox from repo_root, and the fixture
    # inbox is already at tmp_path/"inbox", which is where submissions_inbox looks.
    monkeypatch.setattr(publish_reference, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(publish_reference, "submissions_inbox", lambda root: inbox)


def test_a_fully_embargoed_study_is_told_the_order_not_handed_a_reference_row(
        publish_reference, inbox, entries, tmp_path, monkeypatch, capsys):
    """The case that stopped dead before anything could lift the embargo.

    Nothing in the store cites the name, because the records were never ingested. The old
    code printed "NOTHING CITES" and returned. Adding the reference row here instead would
    be worse than doing nothing: the later, correct rename refuses when the published name
    already has a row, so it would break the very step it was trying to help.
    """
    seed_store(tmp_path)
    _publish_workspace(publish_reference, monkeypatch, tmp_path, inbox)

    code = publish_reference.main([STUDY, "Barrow et al 2027", "--year", "2027",
                                   "--journal", "Mol Ecol", "--apply"])
    output = capsys.readouterr().out

    assert code == 1
    assert "still" in output and "embargoed" in output
    assert SUBMISSION in output
    assert "lift_embargo.py" in output, "it says what to run"
    assert "ingest_submissions.py" in output, "and in what order"
    assert ledger.load(inbox)[SUBMISSION].embargoed is True, "nothing was lifted yet"


def test_a_partly_ingested_study_renames_and_lifts_in_one_go(
        publish_reference, inbox, entries, tmp_path, monkeypatch, capsys):
    """MalAvi already held some of the study; the rest is embargoed.

    The seed rows give the rename something to do. The embargoed submission contributed
    none of them, so ``submissions_behind`` cannot see it -- which is the whole bug -- and
    it has to come from the ledger instead.
    """
    seed_store(tmp_path, host_rows=[
        {"LINEAGE_NAME": "TUMIG19", "REFERENCE_NAME": STUDY, "_source": "seed",
         "_added": "2026-03-23", "RECORD_ID": "HST-000001"}])
    _publish_workspace(publish_reference, monkeypatch, tmp_path, inbox)

    code = publish_reference.main([STUDY, "Barrow et al 2027", "--year", "2027",
                                   "--journal", "Mol Ecol", "--apply"])
    output = capsys.readouterr().out

    assert code == 0
    assert SUBMISSION in output and "embargo lifted" in output
    assert ledger.load(inbox)[SUBMISSION].embargoed is False


def test_the_new_reference_row_is_not_stamped_with_an_embargoed_submission(
        publish_reference, inbox, entries, tmp_path, monkeypatch, capsys):
    """A reference row inherits the provenance of the rows that cite it.

    The embargoed submission supplied none of them. Stamping it there would assert the
    citation came from a submission that contributed nothing, and leave the release gate
    checking that submission's admissibility for a row it never brought.
    """
    from malavi_curation.release_store import TABLES, read_table

    directory = seed_store(tmp_path, host_rows=[
        {"LINEAGE_NAME": "TUMIG19", "REFERENCE_NAME": STUDY, "_source": "seed",
         "_added": "2026-03-23", "RECORD_ID": "HST-000001"}])
    _publish_workspace(publish_reference, monkeypatch, tmp_path, inbox)

    publish_reference.main([STUDY, "Barrow et al 2027", "--year", "2027",
                            "--journal", "Mol Ecol", "--apply"])
    capsys.readouterr()

    references = read_table(directory, TABLES["references"])
    added = [row for row in references
             if row["REFERENCE_NAME"] == "Barrow et al 2027"]
    assert len(added) == 1
    assert added[0]["_source"] == "seed", \
        "the rows that cite it are seed rows, so the citation is seed too"
    assert added[0]["_source"] != SUBMISSION
