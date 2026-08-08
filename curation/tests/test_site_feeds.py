"""Tests for the public-feed leak guard (build_site_feeds.py).

These exist because an independent review on 2026-08-06 found the submitter's own name
being published in ``docs/assets/data/queue.json`` for every submission in the queue — the
intake directory is named after the person who sent it, and the feed used that name as the
public ``id``. The committed file carried a real one.

The guard is therefore shape-based rather than substring-based. A substring search cannot
recognize a person's name, because a name looks like any other text; requiring the public
identifier to match the opaque minted form catches names nobody has thought of yet.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

# build_site_feeds.py is a script beside the package, not a module inside it.
_SPEC = importlib.util.spec_from_file_location(
    "build_site_feeds",
    Path(__file__).resolve().parents[1] / "build_site_feeds.py")
build_site_feeds = importlib.util.module_from_spec(_SPEC)
sys.modules["build_site_feeds"] = build_site_feeds
_SPEC.loader.exec_module(build_site_feeds)

_refuse_to_leak = build_site_feeds._refuse_to_leak


def queue(**item):
    base = {"id": "MALAVI-SUB-2026-000123", "submitted": "2026-07-27", "status": "In the queue"}
    base.update(item)
    return {"generated": "2026-08-06T00:00:00Z", "n_submissions": 1, "items": [base]}


def test_an_opaque_id_is_allowed():
    _refuse_to_leak(queue())


def test_the_intake_directory_name_is_refused():
    """The exact leak that was live: a directory named after the submitter."""
    with pytest.raises(SystemExit, match="not an opaque identifier"):
        _refuse_to_leak(queue(id="20260727T233146_Vincenzo_Ellis"))


def test_any_non_opaque_id_is_refused():
    """Shape-based, so a name nobody anticipated is caught too."""
    for bad in ("row001", "submission-2", "Ada Lovelace", "MALAVI-SUB-26-1"):
        with pytest.raises(SystemExit, match="not an opaque identifier"):
            _refuse_to_leak(queue(id=bad))


def test_an_email_address_anywhere_is_refused():
    with pytest.raises(SystemExit, match="email address"):
        _refuse_to_leak(queue(detail="ask someone@example.edu"))


def test_an_email_in_a_second_payload_is_refused():
    """Contributors and queue are checked together; neither may carry an address."""
    board = {"items": [{"name": "Given Family", "email": "given@example.edu"}]}
    with pytest.raises(SystemExit, match="email address"):
        _refuse_to_leak(queue(), board)


def test_the_guard_survives_python_dash_o():
    """It must not be an `assert`: -O strips those, and this guards the public web."""
    source = (Path(__file__).resolve().parents[1] / "build_site_feeds.py").read_text()
    body = source.split("def _refuse_to_leak")[1].split("\ndef ")[0]
    assert "assert " not in body, "the leak guard must not rely on assert"


def test_the_published_queue_carries_no_intake_directory_name():
    """Guards the real artifact, not just the function: this file goes to the public web."""
    published = (Path(__file__).resolve().parents[2]
                 / "docs" / "assets" / "data" / "queue.json")
    if not published.is_file():
        pytest.skip("queue.json has not been generated in this checkout")
    text = published.read_text(encoding="utf-8")
    assert "@" not in text
    import json
    for item in json.loads(text).get("items", []):
        assert build_site_feeds.ID_PATTERN.match(item["id"]), \
            f"{item['id']!r} is not an opaque identifier"
