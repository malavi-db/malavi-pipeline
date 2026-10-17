"""The curators' board (malavi_curation.status_board, curation/build_status_board.py).

Three things are held in place here:

* what may appear on the board. It is published, encrypted, in a public repository, so
  it carries only what would be tolerable if the password leaked: no submitter, no reason
  text, no lineage name;
* the encryption: a wrong password does not open it, and the format is the one the page's
  own module reads (the Python envelope is decrypted under Node with docs/assets/js/board.mjs);
* that an unchanged board is not rewritten, since every rewrite is a public commit.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import stat
import subprocess
import sys

import pytest

from malavi_curation import ledger, status_board
from malavi_curation.config import repo_root
from malavi_curation.curators import Curator

REGISTRY = {
    "lead": Curator(id="lead", name="Ada Lead", email="lead@example.org", role="lead",
                    active=True, aliases=()),
    "second": Curator(id="second", name="Ben Second", email="second@example.org",
                      role="curator", active=True, aliases=()),
}

CONFIG = {
    "review": {"publish_hold_hours": 72,
               "verdict_form_url": "https://docs.google.com/forms/d/e/FORM/viewform",
               "verdict_form_entries": {"submission_id": 111, "revision": 222}},
    "submissions": {"inbox_dir": "curation/intake/submissions"},
}

SECRET_REASON = "The trace for TESTUS01 is unreadable and Dr Private should resubmit."


def _entries():
    """Four submissions, one in each situation the board has to describe."""
    entries = {}

    waiting = ledger.Entry("MALAVI-SUB-0000-000001", "A", "2026-09-29T16:59:23+00:00",
                           state="ready_for_review", reserved_names=["TESTUS01", "TESTUS02"],
                           embargoed=True)
    waiting.history = [{"event": "report_published", "at": "2026-09-30T22:14:11+00:00",
                        "url": "https://drive.google.com/file/d/REPORT1/view"}]
    entries[waiting.submission_id] = waiting

    flagged = ledger.Entry("MALAVI-SUB-0000-000002", "A", "2026-09-23T08:00:34+00:00",
                           state="held", reserved_names=["TESTUS03"])
    flagged.verdicts = [ledger.Verdict(id="V1", curator="second", verdict="hold", revision=1,
                                       at="2026-10-01T21:29:09+00:00",
                                       reason_text=SECRET_REASON)]
    flagged.history = [{"event": "report_published", "at": "2026-09-24T02:18:55+00:00",
                        "url": "https://drive.google.com/file/d/REPORT2/view"}]
    entries[flagged.submission_id] = flagged

    accepted = ledger.Entry("MALAVI-SUB-0000-000003", "A", "2026-09-02T17:23:47+00:00",
                            state="approved", approved_at="2026-10-01T21:50:00+00:00")
    accepted.verdicts = [
        ledger.Verdict(id="V1", curator="second", verdict="hold", revision=1,
                       at="2026-09-30T10:00:00+00:00", retracted_at="2026-10-01T09:00:00+00:00",
                       retracted_by="second", reason_text=SECRET_REASON),
        ledger.Verdict(id="V2", curator="lead", verdict="approve", revision=1,
                       at="2026-10-01T21:50:00+00:00", reason_text="fine")]
    entries[accepted.submission_id] = accepted

    done = ledger.Entry("MALAVI-SUB-0000-000004", "A", "2026-08-19T21:38:49+00:00",
                        state="released")
    done.history = [{"event": "state", "to": "released", "at": "2026-09-15T18:03:01+00:00"}]
    entries[done.submission_id] = done
    return entries


def _content(**kwargs):
    return status_board.board_content(_entries(), REGISTRY, CONFIG, **kwargs)


# --------------------------------------------------------------------------------------
# What the board says
# --------------------------------------------------------------------------------------

def test_live_submissions_are_listed_longest_wait_first():
    content = _content()
    assert [row["id"][-1] for row in content["submissions"]] == ["1", "2", "3"]
    assert [row["id"][-1] for row in content["finished"]] == ["4"]


def test_what_each_submission_is_waiting_on():
    by_id = {row["id"][-1]: row for row in _content()["submissions"]}
    # Nothing recorded: it wants a curator, counted from when the report went out.
    assert by_id["1"]["waiting"] == {"code": "verdict", "since": "2026-09-30T22:14:11+00:00"}
    # A standing flag: named by its id and by who placed it.
    assert by_id["2"]["waiting"] == {"code": "flag", "flag": "V1", "by": "Ben Second",
                                     "verdict": "flagged",
                                     "since": "2026-10-01T21:29:09+00:00"}
    # Accepted: the hold, with the moment it ends (72 hours after the approval).
    assert by_id["3"]["waiting"] == {"code": "hold", "since": "2026-10-01T21:50:00+00:00",
                                     "ends": "2026-10-04T21:50:00Z", "hours": 72}


def test_verdicts_are_shown_as_words_with_whether_they_still_stand():
    row = {r["id"][-1]: r for r in _content()["submissions"]}["3"]
    assert [(v["id"], v["curator"], v["verdict"], v["standing"], v["ended"])
            for v in row["verdicts"]] == [
        ("V1", "Ben Second", "flagged", False, "withdrawn"),
        ("V2", "Ada Lead", "accepted", True, "")]


def test_a_correction_waiting_names_the_next_person_to_act():
    entries = _entries()
    flagged = entries["MALAVI-SUB-0000-000002"]
    flagged.corrections = [ledger.Correction(id="C1", by="second",
                                             at="2026-10-02T08:00:00+00:00",
                                             authority="author", change=SECRET_REASON)]
    waiting = status_board.board_content(entries, REGISTRY, CONFIG)["submissions"]
    row = [r for r in waiting if r["id"].endswith("2")][0]
    assert row["waiting"]["code"] == "correction_approval"
    flagged.corrections[0].approved_by = "lead"
    flagged.corrections[0].approved_at = "2026-10-02T09:00:00+00:00"
    row = [r for r in status_board.board_content(entries, REGISTRY, CONFIG)["submissions"]
           if r["id"].endswith("2")][0]
    assert row["waiting"]["code"] == "correction_recording"


def test_the_links_are_the_report_in_drive_and_the_prefilled_verdict_form():
    row = _content()["submissions"][0]
    assert row["report"] == {"sent": "2026-09-30T22:14:11+00:00",
                             "url": "https://drive.google.com/file/d/REPORT1/view"}
    assert row["verdict_link"] == (
        "https://docs.google.com/forms/d/e/FORM/viewform?usp=pp_url"
        "&entry.111=MALAVI-SUB-0000-000001&entry.222=1")


def test_nothing_that_must_stay_private_reaches_the_board():
    """The file is public ciphertext for good; what is in it has to survive a leaked
    password. No reason text, no correction text, no lineage name, no address."""
    entries = _entries()
    entries["MALAVI-SUB-0000-000002"].corrections = [ledger.Correction(
        id="C1", by="second", at="2026-10-02T08:00:00+00:00", authority="author",
        change=SECRET_REASON, approval_note=SECRET_REASON)]
    blob = json.dumps(status_board.board_content(entries, REGISTRY, CONFIG))
    for forbidden in ("unreadable", "Dr Private", "TESTUS", "@", "example.org",
                      "reason", "fine"):
        assert forbidden not in blob, f"{forbidden!r} reached the board"
    # Names are counted, never listed.
    assert '"names": 2' in blob


def test_an_excluded_submission_is_left_off():
    content = _content(excluded_ids=["MALAVI-SUB-0000-000001", "MALAVI-SUB-0000-000004"])
    assert [row["id"][-1] for row in content["submissions"]] == ["2", "3"]
    assert content["finished"] == []


def test_the_content_carries_no_time_of_generation():
    """So that two builds of an unchanged ledger are equal and nothing is rewritten."""
    assert _content() == _content()
    assert "generated" not in _content()


# --------------------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------------------

PASSWORD = "correct-horse-16ch"
FAST = 1000          # iterations for tests; the real count is KDF_ITERATIONS


def test_a_round_trip_returns_the_content():
    envelope = status_board.encrypt(_content(), PASSWORD, iterations=FAST)
    assert status_board.decrypt(envelope, PASSWORD) == json.loads(json.dumps(_content()))


def test_the_envelope_holds_no_plaintext():
    envelope = status_board.encrypt(_content(), PASSWORD, iterations=FAST)
    blob = json.dumps(envelope)
    assert "MALAVI-SUB" not in blob and "Ada Lead" not in blob
    assert set(envelope) == {"v", "kdf", "iterations", "cipher", "salt", "iv", "ciphertext"}


def test_a_wrong_password_does_not_open_it():
    envelope = status_board.encrypt(_content(), PASSWORD, iterations=FAST)
    with pytest.raises(status_board.BoardError, match="does not open"):
        status_board.decrypt(envelope, PASSWORD + "x")


def test_a_tampered_file_is_refused_not_misread():
    envelope = status_board.encrypt(_content(), PASSWORD, iterations=FAST)
    envelope["ciphertext"] = envelope["ciphertext"][:-8] + "AAAAAAA="
    with pytest.raises(status_board.BoardError):
        status_board.decrypt(envelope, PASSWORD)


def test_two_encryptions_of_the_same_board_differ():
    """A fresh salt and IV every time: an IV is never reused under one key."""
    first = status_board.encrypt(_content(), PASSWORD, iterations=FAST)
    second = status_board.encrypt(_content(), PASSWORD, iterations=FAST)
    assert first["salt"] != second["salt"] and first["iv"] != second["iv"]
    assert first["ciphertext"] != second["ciphertext"]


def test_the_real_key_derivation_is_not_weakened():
    assert status_board.KDF_ITERATIONS >= 600_000
    assert status_board.KEY_BYTES == 32 and status_board.IV_BYTES == 12


def test_the_page_s_own_module_opens_what_python_wrote(tmp_path):
    """Cross-language: the envelope is decrypted under Node by docs/assets/js/board.mjs,
    with Node's WebCrypto, exactly as a browser would."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    envelope = status_board.encrypt(_content(), PASSWORD, iterations=FAST)
    (tmp_path / "board.json").write_text(json.dumps(envelope), encoding="utf-8")
    module = repo_root() / "docs" / "assets" / "js" / "board.mjs"
    script = tmp_path / "open.mjs"
    script.write_text(
        'import { readFileSync } from "node:fs";\n'
        'import { webcrypto } from "node:crypto";\n'
        f'import {{ decryptBoard }} from "{module}";\n'
        'const envelope = JSON.parse(readFileSync(process.argv[2], "utf8"));\n'
        'try {\n'
        '  const board = await decryptBoard(envelope, process.argv[3], webcrypto.subtle);\n'
        '  console.log(JSON.stringify({ ok: true, board }));\n'
        '} catch (err) {\n'
        '  console.log(JSON.stringify({ ok: false, code: err.code }));\n'
        '}\n', encoding="utf-8")

    def open_with(password):
        done = subprocess.run([node, str(script), str(tmp_path / "board.json"), password],
                              capture_output=True, text=True, check=True)
        return json.loads(done.stdout)

    opened = open_with(PASSWORD)
    assert opened["ok"] and opened["board"] == json.loads(json.dumps(_content()))
    assert open_with("not the password") == {"ok": False, "code": "password"}


# --------------------------------------------------------------------------------------
# Writing the file
# --------------------------------------------------------------------------------------

def test_an_unchanged_board_is_not_rewritten(tmp_path, monkeypatch):
    monkeypatch.setattr(status_board, "KDF_ITERATIONS", FAST)
    target = tmp_path / "board.json"
    assert status_board.write_board(target, _content(), PASSWORD) is True
    before = target.read_bytes()
    assert json.loads(before)["written"]                      # stamped when it changed
    assert status_board.write_board(target, _content(), PASSWORD) is False
    assert target.read_bytes() == before

    changed = _content()
    changed["submissions"][0]["state"] = "in_review"
    assert status_board.write_board(target, changed, PASSWORD) is True
    assert target.read_bytes() != before


def test_a_board_under_an_old_password_is_rewritten_under_the_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(status_board, "KDF_ITERATIONS", FAST)
    target = tmp_path / "board.json"
    status_board.write_board(target, _content(), "the old password")
    assert status_board.write_board(target, _content(), PASSWORD) is True
    assert status_board.decrypt(json.loads(target.read_text()), PASSWORD)


# --------------------------------------------------------------------------------------
# The password file
# --------------------------------------------------------------------------------------

def test_the_password_is_generated_private_and_never_replaced(tmp_path, monkeypatch):
    location = tmp_path / "config" / "board_password.txt"
    monkeypatch.setenv(status_board.PASSWORD_ENV, str(location))
    assert status_board.load_password() is None               # not set up: not an error
    created = status_board.create_password_file()
    assert created == location.resolve()
    assert stat.S_IMODE(location.stat().st_mode) == 0o600
    password = status_board.load_password()
    assert len(password) == 16
    with pytest.raises(status_board.BoardError, match="already exists"):
        status_board.create_password_file()
    assert status_board.load_password() == password


def test_a_password_inside_the_repository_is_refused(monkeypatch):
    monkeypatch.setenv(status_board.PASSWORD_ENV, str(repo_root() / "board_password.txt"))
    with pytest.raises(status_board.BoardError, match="inside the repository"):
        status_board.password_path()


# --------------------------------------------------------------------------------------
# The program
# --------------------------------------------------------------------------------------

@pytest.fixture
def cli(tmp_path, monkeypatch):
    path = repo_root() / "curation" / "build_status_board.py"
    spec = importlib.util.spec_from_file_location("_build_status_board", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_build_status_board"] = module
    spec.loader.exec_module(module)

    inbox = tmp_path / "curation" / "intake" / "submissions"
    inbox.mkdir(parents=True)
    with ledger.open_ledger(inbox, write=True) as entries:
        entries.update(_entries())
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(module, "load_config", lambda: CONFIG)
    monkeypatch.setattr(module.curators, "load_registry", lambda: REGISTRY)
    monkeypatch.setattr(module.status_board, "excluded_submission_ids",
                        lambda inbox, ids, config=None: [])
    monkeypatch.setattr(status_board, "KDF_ITERATIONS", FAST)
    monkeypatch.setenv(status_board.PASSWORD_ENV, str(tmp_path / "secret" / "pw.txt"))
    return module, tmp_path


def test_with_no_password_nothing_is_written_and_it_is_not_an_error(cli, capsys):
    module, root = cli
    assert module.main([]) == 0
    assert not (root / status_board.BOARD_FILE).exists()
    assert "--init-password" in capsys.readouterr().out


def test_init_password_prints_the_path_and_never_the_password(cli, capsys):
    module, root = cli
    assert module.main(["--init-password"]) == 0
    printed = capsys.readouterr().out
    password = status_board.load_password()
    assert password and password not in printed
    assert "pw.txt" in printed
    assert module.main(["--init-password"]) == 1              # refuses to replace it


def test_the_program_writes_a_board_its_password_opens(cli, capsys):
    module, root = cli
    module.main(["--init-password"])
    assert module.main(["--dry-run"]) == 0
    assert not (root / status_board.BOARD_FILE).exists()
    assert module.main([]) == 0
    envelope = json.loads((root / status_board.BOARD_FILE).read_text(encoding="utf-8"))
    board = status_board.decrypt(envelope, status_board.load_password())
    assert len(board["submissions"]) == 3 and len(board["finished"]) == 1
    capsys.readouterr()
    assert module.main([]) == 0
    assert "left as it is" in capsys.readouterr().out
