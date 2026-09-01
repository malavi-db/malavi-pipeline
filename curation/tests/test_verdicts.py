"""Tests for reading curator verdict form responses (verdicts.py).

Built against a real response captured on 2026-08-06 — the first live submission through
the form, made from vaellis@udel.edu while the form was owned by malaviadmin@gmail.com.
That row is reproduced verbatim in ``LIVE_ROW`` below, including the submission id the
curator actually typed, which was "testing1,2,3". That is not a contrived bad input: it is
what a stable identifier looks like when a human types it, and it is the reason prefilled
links exist.
"""
from datetime import datetime, timedelta, timezone

import pytest

from malavi_curation import verdicts
from malavi_curation.verdicts import (
    Action, Rejected, parse_row, prefill_url,
)

# The real row, exactly as openpyxl read it back out of the downloaded sheet.
LIVE_ROW = {
    "Timestamp": datetime(2026, 8, 6, 13, 21, 26, 570000),
    "Email Address": "vaellis@udel.edu",
    "Submission id": "testing1,2,3",
    "Revision": "testing",
    "What are you recording?": "Record a verdict on a submission",
    "Your verdict on this revision": "Accept",
    "Why?": "i accepted",
    "What did you check?": "Proposed names, Sequences, Host and locality, Supporting material",
}


def good(**overrides):
    """A well-formed verdict row: the live one with the two typed fields corrected."""
    row = dict(LIVE_ROW)
    row["Submission id"] = "MALAVI-SUB-2026-000123"
    row["Revision"] = "2"
    row.update(overrides)
    return row


# ------------------------------------------------------------------ the live response

def test_the_live_row_is_refused_for_its_typed_submission_id():
    """The captured response, unchanged. It must not be acted on."""
    result = parse_row(LIVE_ROW)
    assert isinstance(result, Rejected)
    assert "testing1,2,3" in result.reason
    assert not result.ok


def test_the_live_row_carries_the_submitting_curators_address_not_the_form_owners():
    """The property the whole authorization model rests on, proven on real data."""
    assert LIVE_ROW["Email Address"] == "vaellis@udel.edu"
    parsed = parse_row(good())
    assert parsed.address == "vaellis@udel.edu"


def test_the_live_rows_branch_isolation_held():
    """Only the verdict branch answered; the other two branches' columns were absent."""
    for column in (verdicts.COL_HOLD_ID, verdicts.COL_CORRECTION_KIND,
                   verdicts.COL_CHANGE):
        assert not LIVE_ROW.get(column)


# ------------------------------------------------------------------------- verdicts

def test_a_well_formed_accept_parses():
    action = parse_row(good())
    assert isinstance(action, Action)
    assert (action.kind, action.verdict, action.revision) == ("verdict", "approve", 2)
    assert action.reason_text == "i accepted"


def test_the_three_verdict_labels_map_to_ledger_vocabulary():
    for label, expected in (("Accept", "approve"),
                            ("Flag for further review", "hold"),
                            ("Reject", "decline")):
        row = good(**{verdicts.COL_VERDICT: label,
                      verdicts.COL_WHY: "a stated reason"})
        assert parse_row(row).verdict == expected


def test_a_hold_without_reasoning_is_refused():
    row = good(**{verdicts.COL_VERDICT: "Flag for further review",
                  verdicts.COL_WHY: "  "})
    assert "no written reasoning" in parse_row(row).reason


def test_an_accept_without_reasoning_is_still_accepted():
    """Only blocking verdicts require prose; the form asks for it, the parser does not."""
    assert parse_row(good(**{verdicts.COL_WHY: ""})).verdict == "approve"


def test_a_non_numeric_revision_is_refused():
    assert "not a number" in parse_row(good(Revision="testing")).reason


def test_checkbox_answers_are_split():
    action = parse_row(good())
    assert action.checked == ["Proposed names", "Sequences", "Host and locality",
                              "Supporting material"]


def test_no_checkbox_option_contains_a_comma():
    """Forms joins with ", " and escapes nothing, so an option with a comma is unsplittable.

    A property of the form definition, pinned here because it is invisible at the point
    where it would break.
    """
    script = (verdicts.__file__.rsplit("/curation/", 1)[0]
              + "/curation/apps_script/create_verdict_form.gs")
    with open(script, encoding="utf-8") as handle:
        source = handle.read()
    import re as _re
    for block in _re.findall(r"setChoiceValues\(\[(.*?)\]\)", source, _re.S):
        for option in _re.findall(r"'([^']*)'", block):
            assert "," not in option, f"choice {option!r} contains a comma"


# ------------------------------------------------------------------------ timestamps

def test_a_naive_timestamp_is_read_in_the_sheets_timezone():
    """Google records no offset, so the sheet's timezone is the whole answer."""
    action = parse_row(good())
    assert action.at == "2026-08-06T13:21:26+00:00"

    eastern = timezone(timedelta(hours=-4))
    shifted = parse_row(good(), sheet_timezone=eastern)
    assert shifted.at == "2026-08-06T17:21:26+00:00"


def test_the_csv_export_formats_parse():
    for text in ("08/06/2026 13:21:26", "2026-08-06 13:21:26",
                 "2026-08-06T13:21:26+00:00"):
        assert parse_row(good(Timestamp=text)).at == "2026-08-06T13:21:26+00:00"


def test_an_unreadable_timestamp_is_filed_not_raised():
    result = parse_row(good(Timestamp="last Tuesday"))
    assert isinstance(result, Rejected) and "unreadable timestamp" in result.reason


def test_a_missing_address_is_filed_with_a_diagnosis():
    result = parse_row(good(**{verdicts.COL_EMAIL: ""}))
    assert "verified addresses" in result.reason


# -------------------------------------------------------------------------- override

def test_a_well_formed_override_parses():
    row = good(**{
        verdicts.COL_ACTION: verdicts.ACTION_OVERRIDE,
        verdicts.COL_HOLD_ID: "V2",
        verdicts.COL_CONSULTED: "Bob Smith; Alice Jones",
        verdicts.COL_CONSULTED_ON: "2026-08-05",
        verdicts.COL_CONSULTED_HOW: "Email",
        verdicts.COL_RESOLVED: "Agreed the host synonym is correct.",
    })
    action = parse_row(row)
    assert action.kind == "override"
    assert action.hold_id == "V2"
    assert action.consulted == ["Bob Smith", "Alice Jones"]


@pytest.mark.parametrize("blank,expected", [
    (verdicts.COL_HOLD_ID, "which hold"),
    (verdicts.COL_CONSULTED, "who was consulted"),
    (verdicts.COL_CONSULTED_ON, "when"),
    (verdicts.COL_CONSULTED_HOW, "how"),
])
def test_an_override_missing_its_consultation_record_is_refused(blank, expected):
    """The consultation fields are the entire point of the override being recorded."""
    row = good(**{
        verdicts.COL_ACTION: verdicts.ACTION_OVERRIDE,
        verdicts.COL_HOLD_ID: "V2", verdicts.COL_CONSULTED: "Bob",
        verdicts.COL_CONSULTED_ON: "2026-08-05", verdicts.COL_CONSULTED_HOW: "Email",
    })
    row[blank] = ""
    assert expected in parse_row(row).reason


# ------------------------------------------------------------------------ correction

def test_a_data_correction_carries_author_authority():
    row = good(**{
        verdicts.COL_ACTION: verdicts.ACTION_CORRECTION,
        verdicts.COL_CORRECTION_KIND: "Data — confirmed with the authors",
        verdicts.COL_CONFIRMED_BY: "R. Bukauskaite",
        verdicts.COL_CONFIRMED_ON: "2026-08-05",
        verdicts.COL_CHANGE: "Row 4: host is Turdus merula, not Turdus migratorius.",
    })
    action = parse_row(row)
    assert (action.kind, action.authority) == ("correction", "author")


def test_a_judgment_correction_carries_curator_authority():
    row = good(**{
        verdicts.COL_ACTION: verdicts.ACTION_CORRECTION,
        verdicts.COL_CORRECTION_KIND: "Judgment — confirmed with another curator",
        verdicts.COL_CONFIRMED_BY: "Alice",
        verdicts.COL_CHANGE: "Country spelling.",
    })
    assert parse_row(row).authority == "curator"


def test_a_correction_naming_nobody_is_refused():
    row = good(**{
        verdicts.COL_ACTION: verdicts.ACTION_CORRECTION,
        verdicts.COL_CORRECTION_KIND: "Judgment — confirmed with another curator",
        verdicts.COL_CONFIRMED_BY: "",
        verdicts.COL_CHANGE: "Country spelling.",
    })
    assert "who confirmed it" in parse_row(row).reason


def test_an_unknown_action_is_filed():
    assert "unknown action" in parse_row(good(**{verdicts.COL_ACTION: "Other"})).reason


# ---------------------------------------------------------------------- prefill links

def test_prefill_url_carries_both_values():
    url = prefill_url("https://docs.google.com/forms/d/e/ABC/viewform",
                      {"submission_id": 1321287799, "revision": 443144444},
                      "MALAVI-SUB-2026-000123", 2)
    assert "entry.1321287799=MALAVI-SUB-2026-000123" in url
    assert "entry.443144444=2" in url
    assert url.startswith("https://docs.google.com/forms/d/e/ABC/viewform?usp=pp_url")


def test_prefill_url_round_trips_through_the_parser():
    """The link produces exactly the values the parser demands."""
    from urllib.parse import parse_qs, urlparse
    from malavi_curation.config import load_config

    review = load_config()["review"]
    url = prefill_url(review["verdict_form_url"], review["verdict_form_entries"],
                      "MALAVI-SUB-2026-000123", 3)
    query = parse_qs(urlparse(url).query)
    entries = review["verdict_form_entries"]
    row = good(**{
        verdicts.COL_SUBMISSION: query[f"entry.{entries['submission_id']}"][0],
        verdicts.COL_REVISION: query[f"entry.{entries['revision']}"][0],
    })
    action = parse_row(row)
    assert (action.submission_id, action.revision) == ("MALAVI-SUB-2026-000123", 3)


def test_the_configured_sheet_timezone_matches_what_the_parser_assumes():
    """The sheet is set to GMT/no-DST. If that changes, this fails rather than drifting.

    Google records no offset on a response timestamp, so the spreadsheet's timezone is the
    only thing that makes it unambiguous — and it drives the publish hold and the timeout.
    A silent disagreement between the sheet and the parser is an hour of error that shows
    up nowhere in the data.
    """
    from malavi_curation.config import load_config

    assert load_config()["review"]["verdict_sheet_timezone"] == "UTC"


def test_the_parser_does_not_judge_whether_a_flag_stands():
    """The "must accompany a flag" rule belongs to the ledger, not to this parser.

    It used to be asked on the form and enforced here, which was a second copy of a rule
    ledger.record_correction already enforces from its own record of what is flagged --
    see test_a_correction_requires_a_standing_flag. The duplicate was worse than
    redundant: the form's honest answer, "No, I will flag it now", made this parser
    discard the correction the curator had just written out.

    So a well-formed correction row parses. Whether it is allowed to land is decided at
    the write, against the ledger's state rather than the curator's recollection.
    """
    row = good(**{
        verdicts.COL_ACTION: verdicts.ACTION_CORRECTION,
        verdicts.COL_CORRECTION_KIND: "Judgment — confirmed with another curator",
        verdicts.COL_CONFIRMED_BY: "Alice",
        verdicts.COL_CHANGE: "Country spelling.",
    })
    action = parse_row(row)
    assert action.kind == "correction"
    assert not hasattr(verdicts, "COL_FLAGGED")


# ------------------------------------------------- closing a submission for good

def _close_row(reason="A flag on it was never answered", note="", who="lead@example.edu"):
    return {
        verdicts.COL_TIMESTAMP: "2026-08-13 09:00:00",
        verdicts.COL_EMAIL: who,
        verdicts.COL_SUBMISSION: "MALAVI-SUB-2026-000001",
        verdicts.COL_REVISION: "2",
        verdicts.COL_ACTION: verdicts.ACTION_CLOSE,
        verdicts.COL_CLOSE_REASON: reason,
        verdicts.COL_CLOSE_NOTE: note,
    }


def test_a_close_parses_to_a_disposition_code():
    """What the lead reads is not what the decision record stores."""
    action = verdicts.parse_row(_close_row())

    assert action.ok and action.kind == "close"
    assert action.reason_code == "unresolved_objection"
    assert action.submission_id == "MALAVI-SUB-2026-000001"
    assert action.revision == 2


def test_every_offered_reason_maps_to_a_real_disposition_code():
    """The form's list and the ledger's vocabulary must not drift apart.

    The code reaches data/decisions.json, so an unmapped one would be refused at the write
    after the curator had already submitted and gone.
    """
    from malavi_curation import ledger

    for label in verdicts.CLOSE_REASONS:
        action = verdicts.parse_row(_close_row(reason=label))
        assert action.ok, f"{label!r} did not parse"
        assert action.reason_code in ledger.DECLINE_REASON_CODES


def test_a_reason_the_form_does_not_offer_is_refused():
    action = verdicts.parse_row(_close_row(reason="I just don't like it"))

    assert not action.ok
    assert "unknown closing reason" in action.reason


def test_the_note_is_kept_apart_from_the_code():
    """Free text stays in the gitignored ledger; only the code is published."""
    action = verdicts.parse_row(_close_row(note="Spoke to the authors on 12 Aug."))

    assert action.reason_code == "unresolved_objection"
    assert action.reason_text == "Spoke to the authors on 12 Aug."


def test_the_apps_script_offers_exactly_the_reasons_python_parses():
    """The live form is built by the .gs file; a word changed in one place breaks the other.

    Forms records the answer as the literal string a curator clicked, so the two lists are
    a single interface with two implementations and nothing but this test holding them
    together.
    """
    import re
    from malavi_curation.config import repo_root

    script = (repo_root() / "curation" / "apps_script"
              / "create_verdict_form.gs").read_text(encoding="utf-8")
    block = re.search(r"var CLOSE_REASONS = \[(.*?)\];", script, re.S)
    assert block, "CLOSE_REASONS is not declared in create_verdict_form.gs"
    offered = set(re.findall(r"'([^']+)'", block.group(1)))

    assert offered == set(verdicts.CLOSE_REASONS)

    action_line = re.search(r"var ACTION_CLOSE = '([^']+)';", script)
    assert action_line and action_line.group(1) == verdicts.ACTION_CLOSE


def test_no_two_questions_on_the_verdict_form_share_a_title():
    """Two questions with one title become two sheet columns with one name.

    Google writes a question title verbatim as the response sheet's column header, and
    csv.DictReader keeps the LAST column of a repeated name. So the later question silently
    answers for the earlier one.

    This is not hypothetical. 'What was resolved?' was the title of BOTH the override
    question (required: the reasoning for setting aside another curator's objection, the
    only durable record of why it did not stand) and the correction-approval question
    (optional). The correction column came second, so every override stored reason_text="",
    and the parser did not complain because it never asked for that field. Found by an
    independent review on 2026-08-14 and confirmed against the live form the same day.

    No fixture can catch it -- the tests build a row as a dict, and a dict cannot hold one
    key twice. This reads the generator instead.
    """
    import re
    from malavi_curation.config import repo_root

    script = (repo_root() / "curation" / "apps_script"
              / "create_verdict_form.gs").read_text(encoding="utf-8")
    # Question titles only. Page titles come from addPageBreakItem().setTitle(...) and are
    # allowed to repeat a question's wording -- they are not columns.
    titles = re.findall(r"\.setTitle\('([^']+)'\)", script)
    pages = re.findall(r"addPageBreakItem\(\)\s*\.setTitle\('([^']+)'\)", script)
    pages += re.findall(r"addPageBreakItem\(\)\s*\n\s*\.setTitle\('([^']+)'\)", script)
    questions = [title for title in titles if title not in set(pages)]

    duplicates = {title for title in questions if questions.count(title) > 1}
    assert not duplicates, (
        f"these question titles appear more than once: {sorted(duplicates)}. Each becomes "
        f"a response-sheet column, and the later one answers for the earlier.")


def test_the_two_reasoning_columns_are_read_from_different_questions():
    """The pair that collided. Stated separately so renaming one back is a test failure."""
    assert verdicts.COL_RESOLVED != verdicts.COL_CONCLUDED
