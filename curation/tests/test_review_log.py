"""The review rule has a mechanism (plan review of 2026-10-16, finding 14).

A change to a guarded file changes one of the two hashes in ops/record_review.py; this
test fails until a review of the new state is on record in curation/REVIEWS.md. It is
also run by the intake-cycle workflow before a live cycle. It is meant to fail during
development: that is the point at which the review is owed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from malavi_curation.config import repo_root


def _recorder():
    path = repo_root() / "ops" / "record_review.py"
    spec = importlib.util.spec_from_file_location("_record_review", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_record_review"] = module
    spec.loader.exec_module(module)
    return module


def test_every_guarded_file_exists():
    recorder = _recorder()
    missing = [r for r in recorder.GUARDED_PYTHON if not (repo_root() / r).is_file()]
    assert missing == [], f"guarded files that do not exist: {missing}"


def test_the_guarded_list_is_sorted_and_names_every_sender():
    recorder = _recorder()
    assert list(recorder.GUARDED_PYTHON) == sorted(recorder.GUARDED_PYTHON)
    # The files that import the delivery client are guarded, whatever else is.
    for path in sorted((repo_root() / "curation").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "report_delivery" in text and "deliver" in text:
            relative = str(path.relative_to(repo_root()))
            assert relative in recorder.GUARDED_PYTHON, f"{relative} sends mail; guard it"


def test_the_current_code_has_a_review_on_record():
    recorder = _recorder()
    bundle, guarded = recorder.bundle_hash(), recorder.guarded_hash()
    assert recorder.is_reviewed(bundle, guarded), (
        f"a guarded file changed since the last recorded review (bundle {bundle}, guarded "
        f"{guarded}). Run an independent review, fix its findings, then record it:\n"
        f"  .venv/bin/python ops/record_review.py --reviewer ... --findings results/...")


def test_recording_a_review_round_trips(tmp_path, monkeypatch):
    recorder = _recorder()
    reviews = tmp_path / "REVIEWS.md"
    reviews.write_text("| date | bundle | guarded | reviewer | findings | note |\n|---|---|---|---|---|---|\n")
    monkeypatch.setattr(recorder, "REVIEWS", reviews)
    monkeypatch.setattr(recorder, "bundle_hash", lambda: "b" * 16)
    monkeypatch.setattr(recorder, "guarded_hash", lambda: "g" * 16)
    assert not recorder.is_reviewed()
    assert recorder.main(["--reviewer", "x", "--findings", "results/r.md", "--note", "n"]) == 0
    assert recorder.is_reviewed()
    assert recorder.main(["--show"]) == 0
    assert recorder.main([]) == 2        # nothing to record without a reviewer and findings


def test_a_review_is_a_pair_on_one_row_and_covers_the_sources(tmp_path, monkeypatch):
    """Code review 2026-10-16, finding 17: the bundle hash is of the .gs SOURCES, and the
    two hashes count only when the same row carries both."""
    recorder = _recorder()
    reviews = tmp_path / "REVIEWS.md"
    reviews.write_text("| date | bundle | guarded | reviewer | findings | note |\n|---|---|---|---|---|---|\n"
                       "| 2026-01-01 | aaaa | gggg | x | r | n |\n| 2026-01-02 | bbbb | hhhh | x | r | n |\n")
    monkeypatch.setattr(recorder, "REVIEWS", reviews)
    assert recorder.is_reviewed("aaaa", "gggg") and recorder.is_reviewed("bbbb", "hhhh")
    assert not recorder.is_reviewed("aaaa", "hhhh")
    text = (repo_root() / "ops" / "record_review.py").read_text(encoding="utf-8")
    assert 'glob("*.gs")' in text
    for name in (".github/workflows/intake_cycle.yml", "curation/apps_script/build_bundle.py",
                 "ops/record_review.py"):
        assert name in recorder.GUARDED_PYTHON
