"""The privacy scanner (curation/privacy_scan.py, 2026-10-16).

Every test plants a private value in a made-up checkout (config, custody file, intake
tree, ledger) and a public tree, and checks that the scanner finds it, names the class
and the file, never prints the value, and honors the per-destination rules and the allow
file. The last test runs the scanner on the real docs/ tree and the real feeds, which is
the check CI makes before anything is pushed.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from malavi_curation.config import repo_root


@pytest.fixture(scope="module")
def scanner():
    path = repo_root() / "curation" / "privacy_scan.py"
    spec = importlib.util.spec_from_file_location("_privacy_scan", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses resolve their postponed annotations
    # through sys.modules.
    import sys
    sys.modules["_privacy_scan"] = module
    spec.loader.exec_module(module)
    return module


SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"
FORM_ID = "1FAIpQLSeXAMPLEpublicFORMid000000000000000000000"
DRIVE_ID = "1DriveFileIdXXXXXXXXXXXXXXXXXXXX9"
SEQUENCE = "ATGC" * 40          # 160 bp
# Assembled at run time so this file carries no address-shaped literal of its own.
REAL_LOOKING = "jane" + "@" + "uni-somewhere.ac.uk"
RELEASED = "GATTACA" * 20       # 140 bp, in the release


@pytest.fixture
def checkout(tmp_path):
    """A pretend private checkout with one of everything."""
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "project.yml").write_text(
        f'submissions:\n  responses_sheet: "{SHEET_ID}"\n  form_url: "https://docs.google.com/forms/d/e/{FORM_ID}/viewform"\n'
        f'  inbox_dir: intake\nprivacy:\n  public_addresses: [malaviadmin@gmail.com]\n'
        f'  public_ids: [{FORM_ID}, 20260101T000000_DEMO_Test]\n', encoding="utf-8")
    (root / "CUSTODY_PRIVATE.md").write_text(
        "status sheet: 1StatusSheetIdZZZZZZZZZZZZZZZZZ7\n", encoding="utf-8")
    (root / "config" / "curators.yml").write_text(
        yaml.safe_dump({"curators": [{"id": "v", "name": "Vera Maintainer",
                                      "email": "v@example.edu", "role": "lead"}]}),
        encoding="utf-8")
    inbox = root / "intake"
    for name in ("20260102T000000_Jane_Smith", "20260101T000000_DEMO_Test",
                 "20260103T000000_Vera_Maintainer"):
        (inbox / name).mkdir(parents=True)
    (inbox / "20260102T000000_Jane_Smith" / "submission.json").write_text(json.dumps({
        "submitter": {"name": "Jane  Smith", "email": "jane@example.edu"},
        "sequences": [{"name": "X01", "sequence": SEQUENCE}, {"name": "Y", "sequence": RELEASED}],
    }), encoding="utf-8")
    (inbox / "submission_ids.json").write_text(json.dumps({"ids": {
        "20260102T000000_Jane_Smith": {"id": "MALAVI-SUB-2026-000001"}}}), encoding="utf-8")
    (inbox / "review_ledger.json").write_text(json.dumps({"entries": {
        "MALAVI-SUB-2026-000001": {
            "state": "held",
            "verdicts": [{"reason_text": "The chromatogram at position 240 is ambiguous.",
                          "note_to_submitter": "Please re-read positions 240 and 324 for us."}],
            "history": [{"event": "report_published",
                         "url": f"https://drive.google.com/file/d/{DRIVE_ID}/view"}]}}}),
        encoding="utf-8")
    (inbox / "fetched.json").write_text("{}", encoding="utf-8")
    (root / "docs" / "assets" / "data").mkdir(parents=True)
    (root / "docs" / "assets" / "data" / "lineage_sequences.json").write_text(
        json.dumps({"L1": RELEASED}), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".config" / "malavi").mkdir(parents=True)
    (home / ".config" / "malavi" / "board_password.txt").write_text("hunter2-board\n")
    return root, home


def _forbidden(scanner, checkout):
    root, home = checkout
    config = yaml.safe_load((root / "config" / "project.yml").read_text())
    return scanner.build_forbidden(root, config, home=home)


def _classes(items):
    return {i.cls for i in items}


def test_the_forbidden_list_has_one_of_everything(scanner, checkout):
    items = _forbidden(scanner, checkout)
    by = {}
    for item in items:
        by.setdefault(item.cls, []).append(item.value or item.label)
    assert SHEET_ID in by["google_id"]
    assert "1StatusSheetIdZZZZZZZZZZZZZZZZZ7" in by["google_id"]
    assert FORM_ID not in by["google_id"]                          # public on purpose
    assert by["submitter_name"] == ["Jane Smith"]                  # the curator is not one
    assert "20260102T000000_Jane_Smith" in by["intake_dir"]
    assert "20260101T000000_DEMO_Test" not in by["intake_dir"]
    assert "20260103T000000_Vera_Maintainer" in by["intake_dir"]
    assert any("chromatogram" in r for r in by["reason_text"])
    assert any("re-read positions" in r for r in by["reason_text"])
    assert DRIVE_ID in by["drive_id"]
    assert by["sequence"] == [SEQUENCE]                             # the released one is not
    assert by["board_password"] == ["hunter2-board"]
    assert by["curator_name"] == ["Vera Maintainer"]
    assert "email address" in by["address"]
    assert len(by["credential"]) == 5


def _public(tmp_path, files):
    tree = tmp_path / "public"
    for name, text in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tree


def test_each_class_is_found_and_the_value_is_not_printed(scanner, checkout, tmp_path,
                                                            capsys):
    root, _home = checkout
    items = _forbidden(scanner, checkout)
    tree = _public(tmp_path, {
        "a.md": f"sheet {SHEET_ID}",
        "b.md": "written by jane smith yesterday",
        "c.md": "see 20260102T000000_Jane_Smith",
        "d.md": "The chromatogram at position 240 is ambiguous.",
        "e.md": f"https://drive.google.com/file/d/{DRIVE_ID}/view",
        "f.txt": f">x\n{SEQUENCE[:80]}\n{SEQUENCE[80:]}\n",
        "g.md": "password hunter2-board",
        # Built at run time so this test file carries no token-shaped literal itself.
        "h.md": "token " + "ghp_" + "a" * 30,
        # A real-looking address; example.* domains are exempt by design.
        "i.md": "write to " + REAL_LOOKING + " or malaviadmin@gmail.com or nobody@example.edu",
        "j.md": "Jane Smithson is a different person; so is Janet Smith",
    })
    hits = scanner.scan([tree], "any", items, ["malaviadmin@gmail.com"], base=tree)
    found = {(h.cls, h.path) for h in hits}
    assert ("google_id", "a.md") in found
    assert ("submitter_name", "b.md") in found
    assert ("intake_dir", "c.md") in found
    assert ("reason_text", "d.md") in found
    assert ("drive_id", "e.md") in found
    assert ("sequence", "f.txt") in found
    assert ("board_password", "g.md") in found
    assert ("credential", "h.md") in found
    assert ("address", "i.md") in found
    assert not any(h.path == "j.md" for h in hits), "a name matches whole words only"
    addresses = [h for h in hits if h.cls == "address"]
    # The public one and the example-domain one are allowed; the real-looking one is not.
    assert [h.value for h in addresses] == [REAL_LOOKING]
    for hit in hits:
        text = hit.describe(reveal=False)
        assert hit.value not in text and "#" in text
        if hit.value:
            assert hit.value in hit.describe(reveal=True)


def test_the_site_exempts_release_data_from_the_name_class_but_not_the_feeds(
        scanner, checkout, tmp_path):
    items = _forbidden(scanner, checkout)
    tree = _public(tmp_path, {
        "assets/data/tables/references.json": '{"author": "Jane Smith"}',
        "assets/data/queue.json": '{"items": [{"id": "x", "who": "Jane Smith"}]}',
        "assets/data/contributors.json": '{"people": [{"name": "Jane Smith"}]}',
        "index.html": "<p>Thanks to Jane Smith</p>",
    })
    hits = scanner.scan([tree], "site", items, base=tree)
    paths = sorted({h.path for h in hits if h.cls == "submitter_name"})
    assert paths == ["assets/data/queue.json", "index.html"]
    hits = scanner.scan([tree / "assets" / "data" / "queue.json",
                         tree / "assets" / "data" / "contributors.json"], "feeds", items,
                        base=tree)
    assert sorted({h.path for h in hits}) == ["assets/data/queue.json"]


def test_curator_names_are_forbidden_only_in_the_pipeline_repository(scanner, checkout,
                                                                      tmp_path):
    items = _forbidden(scanner, checkout)
    tree = _public(tmp_path, {"config/project.example.yml": "# Vera Maintainer is excluded\n"})
    assert [h.cls for h in scanner.scan([tree], "pipeline", items, base=tree)] == ["curator_name"]
    assert scanner.scan([tree], "site", items, base=tree) == []
    assert scanner.scan([tree], "malaviR", items, base=tree) == []


def test_the_allow_file_needs_a_reason_and_covers_what_it_says(scanner, checkout, tmp_path):
    items = _forbidden(scanner, checkout)
    tree = _public(tmp_path, {"notes/a.md": "jane smith", "notes/b.md": "jane smith"})
    allow = tmp_path / "allow.txt"
    allow.write_text("site\tsubmitter_name\tnotes/a.md\tshe agreed in writing\n")
    rules = scanner.load_allow_rules(allow)
    hits = scanner.scan([tree], "site", items, rules=rules, base=tree)
    assert [h.path for h in hits] == ["notes/b.md"]
    allow.write_text("site\tsubmitter_name\tnotes/*.md\t\n")
    with pytest.raises(SystemExit, match="reason"):
        scanner.load_allow_rules(allow)
    allow.write_text("# a comment\n\n*\t*\tnotes/*\tall of notes is public\n")
    assert scanner.scan([tree], "site", items, rules=scanner.load_allow_rules(allow),
                        base=tree) == []


def test_binary_files_are_skipped(scanner, checkout, tmp_path):
    items = _forbidden(scanner, checkout)
    tree = tmp_path / "public"
    tree.mkdir()
    (tree / "x.png").write_bytes(b"\x89PNG\0\0" + SHEET_ID.encode())
    assert scanner.scan([tree], "site", items, base=tree) == []


def test_the_command_line_exit_codes_and_silence(scanner, checkout, tmp_path, monkeypatch,
                                                  capsys):
    root, home = checkout
    monkeypatch.setattr(scanner, "repo_root", lambda: root)
    monkeypatch.setattr(scanner, "load_config",
                        lambda: yaml.safe_load((root / "config" / "project.yml").read_text()))
    monkeypatch.setattr(Path, "home", lambda: home)
    tree = _public(tmp_path, {"a.md": f"sheet {SHEET_ID}", "b.md": "fine"})
    assert scanner.main(["--destination", "site", str(tree)]) == 1
    err = capsys.readouterr().err
    assert "REFUSING site" in err and "google_id" in err and SHEET_ID not in err
    assert scanner.main(["--destination", "site", str(tree / "b.md")]) == 0
    assert "clean" in capsys.readouterr().out
    assert scanner.main(["--classes"]) == 0
    assert "google_id" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# The real tree: what CI checks before anything is pushed
# --------------------------------------------------------------------------------------

def site_excludes() -> list:
    """The rsync exclusions publish/push_site.sh applies, read from the script itself."""
    import re
    text = (repo_root() / "publish" / "push_site.sh").read_text(encoding="utf-8")
    return re.findall(r'--exclude "([^"]+)"', text)


def test_the_real_site_tree_and_feeds_are_clean(scanner):
    """docs/ as push_site.sh would publish it, and the four feeds, carry nothing private.

    Runs against the real checkout: the real config, the real intake tree (when present)
    and the real ledger. On CI the ledger is committed, so this is a real check there too.
    """
    import fnmatch
    root = repo_root()
    items = scanner.build_forbidden(root)
    excludes = site_excludes()
    docs = root / "docs"
    files = []
    for path in scanner.files_under([docs]):
        relative = str(path.relative_to(docs))
        if any(fnmatch.fnmatch(relative, pattern.rstrip("/")) or
               relative.startswith(pattern.rstrip("/") + "/") for pattern in excludes):
            continue
        files.append(path)
    public_addresses = [str(a) for a in
                        ((scanner.load_config().get("privacy") or {}).get("public_addresses") or [])]
    rules = scanner.load_allow_rules(root / "publish" / "privacy_allow.txt")
    hits = scanner.scan(files, "site", items, public_addresses, rules, base=docs)
    assert hits == [], "\n".join(h.describe() for h in hits)
    feeds = [docs / "assets" / "data" / name for name in scanner.FEED_FILES
             if (docs / "assets" / "data" / name).is_file()]
    hits = scanner.scan(feeds, "feeds", items, public_addresses, rules, base=docs)
    assert hits == [], "\n".join(h.describe() for h in hits)


def test_a_fragment_of_a_released_sequence_is_not_unpublished(scanner, checkout, tmp_path):
    """Code review 2026-10-16, finding 9: a partial read of a known lineage is public."""
    root, home = checkout
    inbox = root / "intake"
    data = json.loads((inbox / "20260102T000000_Jane_Smith" / "submission.json").read_text())
    data["sequences"].append({"name": "Z", "sequence": RELEASED[7:127].lower()})   # a fragment
    data["sequences"].append({"name": "W", "sequence": RELEASED + "ACGTACGT" * 5})  # longer: new
    (inbox / "20260102T000000_Jane_Smith" / "submission.json").write_text(json.dumps(data))
    items = _forbidden(scanner, checkout)
    sequences = [i.value for i in items if i.cls == "sequence"]
    assert SEQUENCE in sequences
    assert RELEASED[7:127].upper() not in sequences
    assert (RELEASED + "ACGTACGT" * 5) in sequences


def test_a_name_in_author_list_order_is_found(scanner, checkout, tmp_path):
    items = _forbidden(scanner, checkout)
    tree = _public(tmp_path, {"a.md": "Smith, Jane; Doe, John", "b.md": "Smithers, Janet"})
    hits = scanner.scan([tree], "any", items, base=tree)
    assert [h.path for h in hits if h.cls == "submitter_name"] == ["a.md"]


def test_a_failure_to_build_the_list_does_not_print_the_error_text(scanner, tmp_path,
                                                                     monkeypatch, capsys):
    monkeypatch.setattr(scanner, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(scanner, "load_config", lambda: {})

    def boom(*a, **k):
        raise ValueError("secret: 1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd")

    monkeypatch.setattr(scanner, "build_forbidden", boom)
    assert scanner.main(["--classes"]) == 2
    err = capsys.readouterr().err
    assert "ValueError" in err and "secret" not in err
