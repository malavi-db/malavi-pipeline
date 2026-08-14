"""The path by which an approved submission's records actually reach MalAvi.

``store_ingest`` could map a workbook into store rows and nothing called it, so the store
had one writer -- the seed -- and a curator's approval reached no data. These tests are
written against that wiring: which submissions the run picks up, which it refuses, and
what it must never quietly destroy on the way past.
"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

openpyxl = pytest.importorskip("openpyxl")

from malavi_curation import ledger, release_gate
from malavi_curation.config import repo_root
from malavi_curation.release_store import TABLES, read_store, write_store

SUBMISSION = "MALAVI-SUB-2026-000123"
DIRECTORY = "20260801T120000_A_Person"

CLOCKS = {"publish_hold_hours": 24, "awaiting_submitter_timeout_days": 60}

REGISTRY = [
    {"id": "lead", "name": "Lead Curator", "email": "lead@example.edu",
     "role": "lead", "active": True},
    {"id": "alice", "name": "Alice", "email": "alice@example.edu", "role": "curator"},
]


# --------------------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def cli():
    """The CLI as a module, loaded from its real path the way the other CLI tests do."""
    path = repo_root() / "curation" / "ingest_submissions.py"
    spec = importlib.util.spec_from_file_location("_ingest_submissions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "curators.yml"
    path.write_text(yaml.safe_dump({"curators": REGISTRY}), encoding="utf-8")
    return path


def approved_entry(registry, submission_id=SUBMISSION, *, hours_ago=48):
    """An entry walked through the states the machine requires, approved 48h ago.

    The same helper as test_release_gate's: the publish hold is measured against the wall
    clock, so the approval has to be genuinely in the past.
    """
    approved_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
                   ).replace(microsecond=0).isoformat()
    entry = ledger.ensure_entry({}, submission_id, "A", "2026-08-01T00:00:00+00:00")
    ledger.transition(entry, "ready_for_review", "intake", at="2026-08-01T12:00:00+00:00")
    ledger.transition(entry, "in_review", "alice", at="2026-08-01T12:00:00+00:00")
    ledger.record_verdict(entry, "alice@example.edu", "approve",
                          at="2026-08-01T13:00:00+00:00", registry_path=registry)
    ledger.transition(entry, "approved", "alice", at=approved_at, config=CLOCKS)
    return entry


# The seed the store starts from. Turdus merula is here so that the ingest has somewhere
# to read an order, a family and a continent from -- all three are derived from MalAvi's
# own records rather than from an external taxonomy.
SEED_HOST = {
    "LINEAGE_NAME": "TURDUS01", "ALT_NAME": "", "PARASITE_GENUS": "Plasmodium",
    "ORDER_NAME": "Passeriformes", "FAMILY_NAME": "Turdidae",
    "GENUS_NAME": "Turdus", "SPECIES_NAME": "merula", "SUB_SPECIES_NAME": "",
    "HOST_STATUS": "", "HOST_AGE": "", "HOST_ENVIRONMENT": "",
    "CONTINENT_NAME": "Europe", "COUNTRY_NAME": "Sweden", "COUNTRY_REGION_NAME": "",
    "SITE_NAME": "Kvismaren", "SITE_COORDINATES": "", "NUMBER_FOUND": "1",
    "NUMBER_TESTED": "10", "REFERENCE_NAME": "Bensch 2000", "COMMENT": "",
    "RECORD_ID": "HST-000001", "_source": "seed", "_added": "2026-03-23",
}


HOSTS_HEADER = ["LINEAGE_NAME", "HostSpecies", "HOST_SPECIES_ID", "HostSubspecies",
                "HostAge", "HostStatus", "HostEnvironment", "Country", "CountryRegion",
                "SiteName", "NUMBER_FOUND", "NUMBER_TESTED", "Reference", "COMMENT"]


def workbook_at(path, hosts_rows):
    """A minimal but real filled ImportMalavi template."""
    book = openpyxl.Workbook()
    hosts = book.active
    hosts.title = "Hosts_and_Sites"
    hosts.append(["The records themselves."])
    hosts.append(HOSTS_HEADER)
    for row in hosts_rows:
        hosts.append(row)

    sites = book.create_sheet("Sites")
    sites.append(["One row per sampling locality."])
    sites.append(["SITE_NAME", "Country", "LATITUDE", "LONGITUDE", "ALTITUDE(m)"])
    sites.append(["Ottenby", "Sweden", "56.2", "16.4", ""])

    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def host_row(lineage="TUMIG19", species="Turdus merula", site="Ottenby",
             reference="Ellis 2026", found="2", tested="20"):
    """One row of Hosts_and_Sites, in the header's order."""
    return [lineage, species, "", "", "", "", "", "Sweden", "", site, found, tested,
            reference, ""]


@pytest.fixture
def project(tmp_path, registry, cli, monkeypatch):
    """A store, an inbox, an approved submission and its workbook, wired into the CLI.

    Real files throughout: the store is written and re-read through release_store, so
    these exercise the round trip rather than a mock of it.
    """
    store_path = tmp_path / "records"
    inbox = tmp_path / "submissions"
    inbox.mkdir(parents=True)

    seed = {name: [] for name in TABLES}
    seed["host_records"] = [dict(SEED_HOST)]
    write_store(store_path, seed)

    entry = approved_entry(registry)
    ledger.save(inbox, {SUBMISSION: entry})
    (inbox / "submission_ids.json").write_text(json.dumps(
        {"version": 1, "next": 2,
         "ids": {DIRECTORY: {"id": SUBMISSION, "minted_at": "2026-08-01T00:00:00+00:00"}}}
    ), encoding="utf-8")

    workbook_at(inbox / DIRECTORY / "ImportMalavi_A_Person.xlsx", [host_row()])

    monkeypatch.setattr(cli, "store_dir", lambda _root: store_path)
    monkeypatch.setattr(cli, "submissions_inbox", lambda _root: inbox)
    return {"store": store_path, "inbox": inbox, "entry": entry}


def host_records(project):
    return read_store(project["store"])["host_records"]


def mine(project):
    """This submission's rows in the store."""
    return [row for row in host_records(project) if row["_source"] == SUBMISSION]


# --------------------------------------------------------------- the ingest itself

def test_an_approved_submission_reaches_the_store(cli, project, capsys):
    """The gap this closes: approval used to reach no data at all."""
    code = cli.main(["--release", "2026-08-14", "--apply"])
    assert code == 0, capsys.readouterr().out

    rows = mine(project)
    assert len(rows) == 1
    assert rows[0]["LINEAGE_NAME"] == "TUMIG19"
    assert rows[0]["_added"] == "2026-08-14"
    assert rows[0]["RECORD_ID"].startswith("HST-")


def test_the_dry_run_writes_nothing(cli, project, capsys):
    """The default. This writes the authoritative MalAvi from files a submitter sent."""
    code = cli.main(["--release", "2026-08-14"])
    assert code == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert mine(project) == []


def test_derived_values_come_from_malavis_own_records(cli, project):
    """Order, family and continent are looked up in the store, never invented."""
    cli.main(["--release", "2026-08-14", "--apply"])
    row = mine(project)[0]
    assert row["ORDER_NAME"] == "Passeriformes"
    assert row["FAMILY_NAME"] == "Turdidae"
    assert row["CONTINENT_NAME"] == "Europe"
    assert row["SITE_COORDINATES"] == "56.2, 16.4"


def test_the_seed_is_never_touched(cli, project):
    """Somebody else's rows are not this importer's business."""
    cli.main(["--release", "2026-08-14", "--apply"])
    seeded = [row for row in host_records(project) if row["_source"] == "seed"]
    assert seeded == [SEED_HOST]


def test_a_second_run_ingests_nothing_again(cli, project, capsys):
    """Idempotent by exclusion: what the store already holds is left alone.

    Re-running the ingest over a submission already in the store would map the mapping's
    deliberate blanks back over anything a curator has filled in since. So the default set
    is what the store does NOT hold, and a re-ingest has to be asked for by name.
    """
    cli.main(["--release", "2026-08-14", "--apply"])
    code = cli.main(["--release", "2026-08-14", "--apply"])
    assert code == 0
    assert "Nothing to ingest" in capsys.readouterr().out
    assert len(mine(project)) == 1


# ------------------------------------------------------------------- what it refuses

def test_an_embargoed_submission_is_refused(cli, project, capsys):
    """The one that would break every later release.

    An embargoed submission is approved, so a looser ingest would happily write its rows;
    release_gate then refuses to build ANY release while they sit there. Ingest and gate
    ask the same function so this cannot happen.
    """
    entry = project["entry"]
    ledger.set_embargo(entry, True, actor="alice")
    ledger.save(project["inbox"], {SUBMISSION: entry})

    code = cli.main(["--release", "2026-08-14", "--apply", "--submission", SUBMISSION])
    assert code == 2
    assert "embargo" in capsys.readouterr().out
    assert mine(project) == []


def test_a_submission_still_in_review_is_refused(cli, project, capsys):
    entry = ledger.ensure_entry({}, SUBMISSION, "A", "2026-08-01T00:00:00+00:00")
    ledger.transition(entry, "ready_for_review", "intake", at="2026-08-01T12:00:00+00:00")
    ledger.save(project["inbox"], {SUBMISSION: entry})

    code = cli.main(["--release", "2026-08-14", "--apply", "--submission", SUBMISSION])
    assert code == 2
    assert "not 'approved'" in capsys.readouterr().out
    assert mine(project) == []


def test_the_ingest_and_the_gate_cannot_disagree(cli, project):
    """Whatever the ingest writes, the gate must be willing to publish.

    Stated as a property rather than a list of states, because the failure mode is a new
    state being handled in one place and not the other.
    """
    cli.main(["--release", "2026-08-14", "--apply"])
    entries = ledger.load(project["inbox"])
    result = release_gate.check(read_store(project["store"]), entries)
    assert result.ok
    assert result.publishing == [SUBMISSION]


def test_a_submission_with_no_template_is_refused_with_a_reason(cli, project, capsys):
    """A paper-only submission is legitimate, and has nothing to ingest yet."""
    for path in (project["inbox"] / DIRECTORY).glob("*.xlsx"):
        path.unlink()

    code = cli.main(["--release", "2026-08-14", "--apply", "--submission", SUBMISSION])
    assert code == 2
    assert "no filled ImportMalavi template" in capsys.readouterr().out


def test_a_supplementary_spreadsheet_is_not_mistaken_for_a_template(cli, project):
    """Submissions travel with the paper's own tables; they are not submissions."""
    book = openpyxl.Workbook()
    book.active.title = "Table S1"
    book.active.append(["prevalence", "n"])
    book.save(project["inbox"] / DIRECTORY / "supplement.xlsx")

    cli.main(["--release", "2026-08-14", "--apply"])
    assert len(mine(project)) == 1


def test_a_bad_release_tag_stops_the_run(cli, project, capsys):
    """_added is read as a release tag; 'next' is not one."""
    assert cli.main(["--release", "next", "--apply"]) == 1
    assert "must be a date" in capsys.readouterr().err
    assert mine(project) == []


# ------------------------------------------------- what a re-ingest must not destroy

def curator_fills(project, column, value):
    """A curator edits the store, which is where the record lives once it is ingested."""
    store = read_store(project["store"])
    for row in store["host_records"]:
        if row["_source"] == SUBMISSION:
            row[column] = value
    write_store(project["store"], store)


def test_a_curator_fill_is_not_silently_blanked(cli, project, capsys):
    """The failure this exists to prevent.

    ``ALT_NAME`` comes from the workbook's Alt_Lineage_names sheet, and this submission has
    none, so every ingest of it maps a blank into that column. A curator who fills one in
    would have it emptied by the next re-ingest and counted as 'replaced' --
    indistinguishable from a real correction, and reported to nobody. The run refuses
    instead, and lists what would be lost.
    """
    cli.main(["--release", "2026-08-14", "--apply"])
    curator_fills(project, "ALT_NAME", "Lineage-B12")

    code = cli.main(["--release", "2026-08-14", "--apply", "--submission", SUBMISSION])

    assert code == 2
    out = capsys.readouterr().out
    assert "would be emptied" in out
    assert "Lineage-B12" in out
    assert mine(project)[0]["ALT_NAME"] == "Lineage-B12"


def test_the_loss_can_be_accepted_deliberately(cli, project):
    """Refusing is the default, not the only option -- with the loss listed either way."""
    cli.main(["--release", "2026-08-14", "--apply"])
    curator_fills(project, "ALT_NAME", "Lineage-B12")

    code = cli.main(["--release", "2026-08-14", "--apply", "--submission", SUBMISSION,
                     "--allow-blanking"])
    assert code == 0
    assert mine(project)[0]["ALT_NAME"] == ""


def test_a_filled_in_taxonomy_survives_a_re_ingest_by_itself(cli, project, capsys):
    """Not every blank behaves like ALT_NAME, and the difference is worth knowing.

    Order and family are derived from MalAvi's own records. Once a curator fills them in,
    the row IS one of MalAvi's own records, so the next ingest of the same host reads them
    back out of the store rather than re-blanking them. The gap the mapping leaves closes
    itself on the first fill.
    """
    workbook_at(project["inbox"] / DIRECTORY / "ImportMalavi_A_Person.xlsx",
                [host_row(species="Nucifraga caryocatactes")])
    cli.main(["--release", "2026-08-14", "--apply"])
    assert "no record of Nucifraga caryocatactes" in capsys.readouterr().out

    store = read_store(project["store"])
    for row in store["host_records"]:
        if row["_source"] == SUBMISSION:
            row["ORDER_NAME"] = "Passeriformes"
            row["FAMILY_NAME"] = "Corvidae"
    write_store(project["store"], store)

    code = cli.main(["--release", "2026-08-14", "--apply", "--submission", SUBMISSION])

    assert code == 0
    assert mine(project)[0]["FAMILY_NAME"] == "Corvidae"


def test_a_correction_replaces_rather_than_appends(cli, project):
    """Re-ingest is what a correction does, and both versions must not survive it."""
    cli.main(["--release", "2026-08-14", "--apply"])
    first = mine(project)[0]["RECORD_ID"]

    workbook_at(project["inbox"] / DIRECTORY / "ImportMalavi_A_Person.xlsx",
                [host_row(found="3")])
    code = cli.main(["--release", "2026-08-20", "--apply", "--submission", SUBMISSION])

    assert code == 0
    rows = mine(project)
    assert len(rows) == 1
    assert rows[0]["NUMBER_FOUND"] == "3"
    # The identity survives, and so does the release the record first appeared in: a
    # correction to a count does not change since when MalAvi has held the record.
    assert rows[0]["RECORD_ID"] == first
    assert rows[0]["_added"] == "2026-08-14"


def test_a_row_the_correction_drops_leaves_the_store(cli, project):
    cli.main(["--release", "2026-08-14", "--apply"])
    workbook_at(project["inbox"] / DIRECTORY / "ImportMalavi_A_Person.xlsx",
                [host_row(), host_row(lineage="TUMIG20")])
    cli.main(["--release", "2026-08-14", "--apply", "--submission", SUBMISSION])
    assert len(mine(project)) == 2

    workbook_at(project["inbox"] / DIRECTORY / "ImportMalavi_A_Person.xlsx",
                [host_row()])
    cli.main(["--release", "2026-08-14", "--apply", "--submission", SUBMISSION])
    assert [row["LINEAGE_NAME"] for row in mine(project)] == ["TUMIG19"]


def test_two_templates_in_one_submission_do_not_delete_each_other(cli, project):
    """Both are read before anything is replaced.

    Replacing per workbook would have the second one remove the first one's rows:
    replace_submission_rows replaces everything the submission contributed, which is
    right once and destructive twice.
    """
    workbook_at(project["inbox"] / DIRECTORY / "part_a.xlsx", [host_row()])
    workbook_at(project["inbox"] / DIRECTORY / "part_b.xlsx",
                [host_row(lineage="TUMIG21")])
    for path in (project["inbox"] / DIRECTORY).glob("ImportMalavi_*.xlsx"):
        path.unlink()

    cli.main(["--release", "2026-08-14", "--apply"])
    assert sorted(row["LINEAGE_NAME"] for row in mine(project)) == ["TUMIG19", "TUMIG21"]


# ------------------------------------------------ the name a submission was approved under
#
# A proposed lineage name MalAvi already owns only WARNS at screen time, because the report
# offers a free alternative and approving the submission adopts it. Rehearsed on the real
# demo submission on 2026-08-11, that agreement never reached the data: the ledger held
# name_corrections {'TUMIG10': 'TUMIG32'} and reserved TUMIG32 publicly, while the store
# received TUMIG10 -- a lineage MalAvi already held, under a different sequence and
# accession. The store carried two lineages under one name and only a build-time warning
# noticed.

WINDOW = "A" * 479
OTHER_WINDOW = "C" + "A" * 478


def lineage_workbook(path, new_lineages, sequences, hosts_rows):
    """A filled template that declares new lineages, not only records."""
    book = openpyxl.Workbook()
    hosts = book.active
    hosts.title = "Hosts_and_Sites"
    hosts.append(["The records themselves."])
    hosts.append(HOSTS_HEADER)
    for row in hosts_rows:
        hosts.append(row)

    def sheet(name, header, rows):
        worksheet = book.create_sheet(name)
        worksheet.append(["note"])
        worksheet.append(header)
        for row in rows:
            worksheet.append(row)

    sheet("Sites", ["SITE_NAME", "Country", "LATITUDE", "LONGITUDE", "ALTITUDE(m)"],
          [["Ottenby", "Sweden", "56.2", "16.4", ""]])
    sheet("NewLineages", ["LINEAGE_NAME", "GENBANK_NR", "ParasiteGenus",
                          "HOST_SPECIES_ID", "Reference", "COMMENT"], new_lineages)
    sheet("Sequences", ["LINEAGE_NAME", "SEQUENCE"], sequences)

    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


@pytest.fixture
def taken_name(project):
    """MalAvi already holds TUMIG50; the submission proposes it for a different sequence."""
    store = read_store(project["store"])
    store["lineages"] = [{
        "LINEAGE_NAME": "TUMIG50", "GENBANK_ACC": "KF314763", "SEQ_LENGTH": "Partial",
        "GENUS_NAME": "Haemoproteus", "SPECIES_NAME": "", "SEQUENCE": WINDOW,
        "RECORD_ID": "LIN-000001", "_source": "seed", "_added": "2026-03-23"}]
    write_store(project["store"], store)

    lineage_workbook(project["inbox"] / DIRECTORY / "ImportMalavi_A_Person.xlsx",
                     new_lineages=[["TUMIG50", "PQ118836", "Haemoproteus", "",
                                    "Ellis 2026", ""]],
                     sequences=[["TUMIG50", OTHER_WINDOW]],
                     hosts_rows=[host_row(lineage="TUMIG50")])
    return project


def test_a_taken_lineage_name_is_refused_and_nothing_is_written(cli, taken_name, capsys):
    """Two sequences under one name breaks every join, duplicates a tip label in the
    alignment, and cannot be untangled afterwards. It must not reach the store."""
    code = cli.main(["--release", "2026-08-14", "--apply"])
    output = capsys.readouterr().out

    assert code == 2, output
    assert "REFUSED" in output
    assert "already a lineage in MalAvi (KF314763)" in output

    store = read_store(taken_name["store"])
    assert [row["LINEAGE_NAME"] for row in store["lineages"]] == ["TUMIG50"]
    assert [row["_source"] for row in store["lineages"]] == ["seed"]
    assert not [row for row in store["host_records"] if row["_source"] == SUBMISSION]


def test_the_name_agreed_at_approval_is_what_the_store_receives(cli, taken_name, registry):
    """The other half: with the rename agreed, the submission goes through under it."""
    entry = taken_name["entry"]
    entry.name_corrections = {"TUMIG50": "TUMIG51"}
    ledger.save(taken_name["inbox"], {SUBMISSION: entry})

    code = cli.main(["--release", "2026-08-14", "--apply"])
    store = read_store(taken_name["store"])
    assert code == 0

    names = sorted(row["LINEAGE_NAME"] for row in store["lineages"])
    assert names == ["TUMIG50", "TUMIG51"]
    # The records follow the rename, or they would point at a lineage that is not theirs.
    assert [row["LINEAGE_NAME"] for row in store["host_records"]
            if row["_source"] == SUBMISSION] == ["TUMIG51"]
    # And MalAvi's own TUMIG50 is untouched.
    held = [row for row in store["lineages"] if row["LINEAGE_NAME"] == "TUMIG50"]
    assert held[0]["GENBANK_ACC"] == "KF314763"
    assert held[0]["_source"] == "seed"


def test_the_rename_is_reported_rather_than_done_silently(cli, taken_name, capsys):
    entry = taken_name["entry"]
    entry.name_corrections = {"TUMIG50": "TUMIG51"}
    ledger.save(taken_name["inbox"], {SUBMISSION: entry})

    cli.main(["--release", "2026-08-14", "--apply"])
    output = capsys.readouterr().out
    assert "TUMIG50 -> TUMIG51" in output
    assert "agreed when the submission was approved" in output


# ------------------------------------------------------------------ taking rows back out
#
# The deadlock these pin. Ingest writes a submission's rows into the store while it is
# merely APPROVED, before any release exists. release_gate.admissibility then refuses any
# _source whose entry is not approved or released, and build_release refuses the whole
# build on any violation. So a submitter emailing "please withdraw it" the day after
# ingest -- an allowed transition -- stopped MalAvi publishing ANY release, on any
# subject, until five CSVs were edited by hand. The only escape offered,
# --i-am-overriding-the-approval-gate, publishes the withdrawn submitter's records.

def withdraw(project, state="withdrawn"):
    """Move the ingested submission out of 'approved', as a real withdrawal would."""
    entries = ledger.load(project["inbox"])
    ledger.transition(entries[SUBMISSION], state, "vaellis@udel.edu",
                      at="2026-08-15T00:00:00+00:00", reason="withdrawn_by_submitter")
    ledger.save(project["inbox"], entries)
    return entries


def test_a_withdrawal_after_ingest_blocks_every_release_until_it_is_retracted(cli,
                                                                             project):
    cli.main(["--release", "2026-08-14", "--apply"])
    entries = withdraw(project)

    blocked = release_gate.check(read_store(project["store"]), entries)
    assert not blocked.ok, ("this is the deadlock: with the rows still in the store, no "
                            "release can be built at all")

    code = cli.main(["--release", "2026-08-14", "--retract", SUBMISSION, "--apply"])
    assert code == 0
    assert mine(project) == []

    freed = release_gate.check(read_store(project["store"]), ledger.load(project["inbox"]))
    assert freed.ok, "with the rows gone the gate has nothing left to object to"


def test_a_retraction_leaves_everybody_elses_rows_alone(cli, project):
    cli.main(["--release", "2026-08-14", "--apply"])
    withdraw(project)
    cli.main(["--release", "2026-08-14", "--retract", SUBMISSION, "--apply"])
    seeded = [row for row in host_records(project) if row["_source"] == "seed"]
    assert seeded == [SEED_HOST]


def test_a_retraction_writes_nothing_without_apply(cli, project, capsys):
    cli.main(["--release", "2026-08-14", "--apply"])
    withdraw(project)
    code = cli.main(["--release", "2026-08-14", "--retract", SUBMISSION])
    assert code == 0
    assert "Nothing was written" in capsys.readouterr().out
    assert len(mine(project)) == 1


def test_an_approved_submission_is_not_retractable(cli, project, capsys):
    """Rows the gate is happy to publish belong in the store. Removing them here would
    lose them silently -- close or hold the submission first, which is the decision."""
    cli.main(["--release", "2026-08-14", "--apply"])
    code = cli.main(["--release", "2026-08-14", "--retract", SUBMISSION, "--apply"])
    assert code == 3
    assert "Close or hold the submission first" in capsys.readouterr().err
    assert len(mine(project)) == 1


def test_retracting_and_ingesting_in_one_run_is_refused(cli, project, capsys):
    code = cli.main(["--release", "2026-08-14", "--retract", SUBMISSION,
                     "--submission", SUBMISSION, "--apply"])
    assert code == 1
    assert "Run them separately" in capsys.readouterr().err
