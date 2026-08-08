"""Tests for reading the rebuilt submission form, against a real response.

Captured 2026-08-06 from the first submission through the form after it was rebuilt under
malaviadmin@gmail.com. The row is reproduced verbatim in ``LIVE_ROW``.

Why pin a real row rather than a synthetic one: every consumer of this data finds its
answers by matching *words in the question text* — "institution", "leaderboard", "sending" —
which survives light rewording of the form but fails silently when a question is renamed or
was never added at all. A synthetic fixture built from the same assumptions as the code
cannot catch that; a row that came out of the live form can. It already caught one: the
"What are you sending us?" question did not exist on the old form, so ``_sending()`` had
returned nothing since the day it was written.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from malavi_curation.form_metadata import submitter_from_metadata

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def feeds():
    """build_site_feeds.py, loaded from the script it lives in."""
    spec = importlib.util.spec_from_file_location(
        "_build_site_feeds", _REPO / "curation" / "build_site_feeds.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_build_site_feeds"] = module
    spec.loader.exec_module(module)
    return module


# The real response, verbatim. The two long titles are exactly as the form asks them.
_MATERIALS = ("Submission PDF and Supplementary Materials (associated with the submission "
              "template file...but if you want you can just upload the PDF, even old PDFs, "
              "and we will try to capture their data)")

LIVE_ROW = {
    "Timestamp": "2026-08-06 18:21:31.556000",
    "Email Address": "vaellis@udel.edu",
    "What is your first and last name?": "Vincenzo Ellis",
    "What institution are you associated with?": "UD",
    "What country are you located in?": "USA",
    "Do you want your name and information added to the website leaderboard?": "No",
    "Are you submitting published or unpublished data?": "Published",
    "Are you submitting a filled out data template file, a PDF + supplementary "
    "materials, or both?": "PDF + supplementary materials (if applicable)",
    "What are you sending us?": "New lineage names and sequences",
    "Please provide any relevant notes or communication here (if applicable).":
        "this is a test! 1, 2, 3",
    "Submission Template File (fill out the template on the website and upload here)": "",
    _MATERIALS: "https://drive.google.com/open?id=1H2uq2-nMkcPhczmn95G_Ukh0VEuUuZX0",
}


def test_what_are_you_sending_is_read(feeds):
    """REGRESSION: this returned "" for the life of the old form.

    _sending() searches for a question containing the word "sending". No question on the
    old form contained it, so the queue's description of an unscreened submission has
    always been blank. The rebuilt form asks it.
    """
    assert feeds._sending(LIVE_ROW) == "names and sequences"


def test_the_three_sending_answers_all_map(feeds):
    """The form's answers must keep starting with the words _sending parses."""
    for answer, expected in (
            ("New lineage names and sequences", "names and sequences"),
            ("Records of known lineages in hosts or vectors", "records for a manuscript"),
            ("Both", "names, sequences and records")):
        row = dict(LIVE_ROW, **{"What are you sending us?": answer})
        assert feeds._sending(row) == expected, answer


def test_publication_stage_is_read(feeds):
    assert feeds._publication_stage(LIVE_ROW) == "post-publication"


def test_submitter_details_are_read(feeds):
    assert feeds._find(LIVE_ROW, "first", "last name") == "Vincenzo Ellis"
    assert feeds._find(LIVE_ROW, "institution") == "UD"


def test_the_name_lookup_does_not_return_the_institution(feeds):
    """Both questions contain "name"-adjacent words; the matcher excludes institution."""
    assert feeds._find(LIVE_ROW, "first", "last name") != "UD"


def test_leaderboard_consent_defaults_to_exclusion(feeds):
    """This response answered No, so the submitter must not reach the contributor board."""
    consent = (feeds._find(LIVE_ROW, "leaderboard") or "").strip().lower()
    assert consent == "no"
    assert consent != feeds.LEADERBOARD_CONSENT_YES


def test_submitter_from_metadata_reads_the_real_row():
    assert submitter_from_metadata(LIVE_ROW) == {
        "name": "Vincenzo Ellis",
        "email": "vaellis@udel.edu",
        "institution": "UD",
    }


def test_the_uploaded_file_id_is_extractable():
    """The fetcher takes file ids out of the response row rather than listing a folder."""
    pattern = re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})")
    match = pattern.search(LIVE_ROW[_MATERIALS])
    assert match and match.group(1) == "1H2uq2-nMkcPhczmn95G_Ukh0VEuUuZX0"


def test_an_absent_upload_is_simply_empty():
    """Both upload questions are optional; this response attached only a PDF."""
    template = LIVE_ROW["Submission Template File (fill out the template on the "
                        "website and upload here)"]
    assert template == ""


def test_the_response_timestamp_is_recorded_in_gmt():
    """The sheet is set to GMT with no daylight saving, and this row proves it.

    Submitted at 14:21 US Eastern (UTC-4 in August); the sheet recorded 18:21. A sheet left
    on local time would have written 14:21 with no offset to say so — and a submission's
    timestamp is what carries its name-reservation priority, which is earliest-wins.
    """
    from datetime import datetime

    recorded = datetime.fromisoformat(LIVE_ROW["Timestamp"])
    assert (recorded.hour, recorded.minute) == (18, 21)


def test_every_question_the_code_looks_for_exists_in_the_form(feeds):
    """The whole class of bug this file exists for: a lookup with nothing behind it.

    Each needle below is one a consumer actually searches for. If a question is renamed or
    dropped in the form editor, the matching lookup starts returning nothing, silently —
    which is exactly what happened to "sending".
    """
    for needles in (("email",), ("first", "last name"), ("institution",),
                    ("leaderboard",), ("published",), ("sending",), ("template", "pdf")):
        assert feeds._find(LIVE_ROW, *needles), f"no question matches {needles}"
