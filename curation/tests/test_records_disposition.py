"""What the confirmation email tells a submitter will happen to their RECORDS.

The lineage names in that email are settled by a curator. The records are not: whether
they appear in the next release depends on what the submitter selected on the form, and
until 2026-08-09 the email resolved that with an "if you asked us to..." conditional the
reader had to apply to themselves.

The branch is now decided here, in Python, from the form answers. These tests pin the two
readings that would do real harm if they flipped:

* telling somebody their unpublished records are going public when they asked to wait;
* telling somebody their records are in the next release when they never sent any.
"""
from __future__ import annotations

import pytest

from malavi_curation import form_metadata
from malavi_curation.report_delivery import build_notice_payload

# The live form's wording, as it reaches metadata.json.
Q_STAGE = "Are you submitting published or unpublished data?"
Q_SENDING = "What are you sending us?"
Q_EMBARGO = "If your data are unpublished, may we add the records to MalAvi now?"


def meta(stage=None, sending=None, embargo=None):
    out = {}
    if stage is not None:
        out[Q_STAGE] = stage
    if sending is not None:
        out[Q_SENDING] = sending
    if embargo is not None:
        out[Q_EMBARGO] = embargo
    return out


class TestWhetherRecordsAreHeld:
    def test_published_data_are_never_held(self):
        """Whatever was answered to the embargo question, a published study goes in."""
        for embargo in (None, "Hold them until I confirm the study is accepted",
                        "Not applicable - my data are already published"):
            assert not form_metadata.records_are_held(
                meta(stage="Published", sending="Both", embargo=embargo))

    def test_unpublished_and_asked_to_wait_is_held(self):
        assert form_metadata.records_are_held(
            meta(stage="Unpublished", sending="Both",
                 embargo="Hold them until I confirm the study is accepted"))

    def test_unpublished_but_happy_to_publish_is_not_held(self):
        """The whole point of adding the question: 'unpublished' does not mean 'wait'."""
        assert not form_metadata.records_are_held(
            meta(stage="Unpublished", sending="Both",
                 embargo="Add them now, credited as unpublished"))

    def test_an_unanswered_embargo_question_holds(self):
        """Every submission fetched before 2026-08-09 is in exactly this position.

        Silence is not consent. Holding wrongly is fixed by writing to somebody;
        publishing wrongly is fixed by retracting from a release people have downloaded.
        """
        assert form_metadata.records_are_held(meta(stage="Unpublished", sending="Both"))

    def test_the_new_wording_of_the_stage_question_still_holds(self):
        """The question was reworded once already, and both vocabularies are in the data."""
        assert form_metadata.records_are_held(
            {"Is this submission pre-publication or post-publication?": "Pre-publication",
             Q_SENDING: "Both"})


class TestTheTwoQuestionsDoNotCollide:
    """"unpublished" contains "published", so the two questions can match each other.

    These pin the separation regardless of what order the questions sit in on the form.
    Getting it wrong is silent: publication_stage() would return "" and every submission
    would read as neither pre- nor post-publication.
    """

    def test_stage_is_read_correctly_with_the_embargo_question_first(self):
        ordered = {Q_EMBARGO: "Add them now, credited as unpublished",
                   Q_STAGE: "Unpublished"}
        assert form_metadata.publication_stage(ordered) == "pre-publication"

    def test_stage_is_read_correctly_with_the_stage_question_first(self):
        ordered = {Q_STAGE: "Unpublished",
                   Q_EMBARGO: "Add them now, credited as unpublished"}
        assert form_metadata.publication_stage(ordered) == "pre-publication"

    def test_the_embargo_answer_is_never_mistaken_for_a_stage(self):
        """With no stage question at all, the embargo answer must not stand in for one."""
        assert form_metadata.publication_stage(
            {Q_EMBARGO: "Add them now, credited as unpublished"}) == ""

    def test_the_quoted_answer_is_order_independent_too(self):
        """The email quotes the raw answer, and used to look it up without the exclusion.

        publication_stage() was ruled safe by these tests while notify_submitters did its
        own unguarded lookup, so with the embargo question sitting first a submitter would
        have been told they selected "Add them now, credited as unpublished" as their
        publication stage. Both orderings now go through stage_answer().
        """
        embargo_first = {Q_EMBARGO: "Add them now, credited as unpublished",
                         Q_STAGE: "Unpublished"}
        stage_first = {Q_STAGE: "Unpublished",
                       Q_EMBARGO: "Add them now, credited as unpublished"}
        assert form_metadata.stage_answer(embargo_first) == "Unpublished"
        assert form_metadata.stage_answer(stage_first) == "Unpublished"

    def test_the_quoted_answer_keeps_the_submitters_own_words(self):
        """Unlike publication_stage it is not normalized -- the email says "You selected"."""
        assert form_metadata.stage_answer(
            {"Is this submission pre-publication or post-publication?":
                "Pre-publication"}) == "Pre-publication"

    def test_the_leaderboard_question_is_not_the_embargo_question(self):
        """It contains "added", and "add" is a substring of it."""
        assert form_metadata.records_embargo(
            {"Do you want your name and information added to the website leaderboard?":
                "Yes"}) == ""


class TestWhetherRecordsWereSentAtAll:
    @pytest.mark.parametrize("answer,expected", [
        ("New lineage names and sequences", False),
        ("Records of known lineages in hosts or vectors", True),
        ("Both", True),
    ])
    def test_each_answer_maps(self, answer, expected):
        assert form_metadata.records_were_included(meta(sending=answer)) is expected

    def test_an_unanswered_question_reads_as_no_records(self):
        assert not form_metadata.records_were_included({})


class TestWhatThePayloadCarries:
    def _payload(self, **selections):
        return build_notice_payload(
            "20260101T000000_X", to="a@example.edu", submitter_name="A Researcher",
            names=["TUMIG25"], corrections={}, reference="A paper 2026",
            selections=selections)

    def test_the_selections_reach_the_email(self):
        payload = self._payload(stage="Unpublished", sending="Both",
                                records_included=True, records_held=True)
        assert payload["selections"]["stage"] == "Unpublished"
        assert payload["selections"]["sending"] == "Both"
        assert payload["selections"]["records_held"] is True

    def test_the_answers_are_quoted_verbatim_not_normalized(self):
        """"You selected X" is a quotation. Normalizing puts words in their mouth.

        It matters because the stage question has been asked two ways, and a submitter
        who clicked "Unpublished" should not be told they selected "pre-publication".
        """
        payload = self._payload(stage="Unpublished", sending="Both")
        assert payload["selections"]["stage"] == "Unpublished"
        assert "pre-publication" not in str(payload["selections"]).lower()

    def test_a_submission_with_no_selections_still_builds(self):
        """An old submission has none of these answers, and must still get its names."""
        payload = build_notice_payload(
            "20260101T000000_X", to="a@example.edu", submitter_name="",
            names=["TUMIG25"], corrections={})
        assert payload["selections"] == {}
        assert payload["names"] == ["TUMIG25"]


# =====================================================================================
# THE WIRING: form answer -> Entry.embargoed -> the release gate
#
# Until 2026-08-10 nothing ever wrote Entry.embargoed, so the field the whole
# unpublished-records design turns on was permanently False and the release gate's
# embargo branch could not fire. These test the path end to end.
# =====================================================================================

import json as _json

from malavi_curation import enrollment, ledger, release_gate


HELD = {"Are you submitting published or unpublished data?": "Unpublished",
        "If your data are unpublished, may we add the records to MalAvi now?":
            "Hold them until I confirm the study is accepted"}

ADD_NOW = {"Are you submitting published or unpublished data?": "Unpublished",
           "If your data are unpublished, may we add the records to MalAvi now?":
               "Add them now, credited as unpublished"}

PUBLISHED = {"Are you submitting published or unpublished data?": "Published",
             "If your data are unpublished, may we add the records to MalAvi now?":
                 "Not applicable - my data are already published"}


def _submission(tmp_path, metadata, name="20260810T120000_someone"):
    sub_dir = tmp_path / name
    sub_dir.mkdir()
    if metadata is not None:
        (sub_dir / "metadata.json").write_text(_json.dumps(metadata), encoding="utf-8")
    return sub_dir


def _entry():
    return ledger.ensure_entry({}, "MALAVI-SUB-2026-000123", "A",
                               "2026-08-01T00:00:00+00:00")


def test_hold_is_written_to_the_entry(tmp_path):
    entry = _entry()
    assert enrollment.apply_embargo(entry, _submission(tmp_path, HELD)) is True
    assert entry.embargoed is True


def test_add_now_leaves_the_records_publishable(tmp_path):
    entry = _entry()
    assert enrollment.apply_embargo(entry, _submission(tmp_path, ADD_NOW)) is None
    assert entry.embargoed is False


def test_a_published_study_is_never_embargoed(tmp_path):
    entry = _entry()
    enrollment.apply_embargo(entry, _submission(tmp_path, PUBLISHED))
    assert entry.embargoed is False


def test_a_submission_predating_the_question_is_held(tmp_path):
    """Silence is not consent. Every submission fetched before 2026-08-09 answers blank."""
    old = {"Are you submitting published or unpublished data?": "Unpublished"}
    entry = _entry()
    assert enrollment.apply_embargo(entry, _submission(tmp_path, old)) is True


def test_an_explicit_lift_survives_the_next_intake_run(tmp_path):
    """The bug this guard exists for.

    The submitter emails "the paper is out"; a maintainer lifts the embargo. Their form
    answer still says hold, and intake runs nightly. Without the guard the lift would be
    silently undone and the records would go on being withheld from every release.
    """
    sub_dir = _submission(tmp_path, HELD)
    entry = _entry()
    enrollment.apply_embargo(entry, sub_dir)
    assert entry.embargoed is True

    ledger.set_embargo(entry, False, actor="maintainer", note="Author says it is out.")
    assert entry.embargoed is False

    assert enrollment.apply_embargo(entry, sub_dir) is None
    assert entry.embargoed is False


def test_a_corrected_form_answer_still_takes_effect_before_anybody_decides(tmp_path):
    """Re-reading is the point: only an explicit decision freezes it."""
    sub_dir = _submission(tmp_path, HELD)
    entry = _entry()
    enrollment.apply_embargo(entry, sub_dir)
    assert entry.embargoed is True

    (sub_dir / "metadata.json").write_text(_json.dumps(ADD_NOW), encoding="utf-8")
    assert enrollment.apply_embargo(entry, sub_dir) is False
    assert entry.embargoed is False


def test_missing_metadata_invents_nothing(tmp_path):
    entry = _entry()
    assert enrollment.apply_embargo(entry, _submission(tmp_path, None)) is None
    assert entry.embargoed is False


def test_an_embargo_cannot_be_recorded_after_release():
    """Terminal, and already published: the ledger cannot un-publish a downloaded ZIP."""
    entry = _entry()
    entry.state = "released"
    with pytest.raises(ledger.LedgerError, match="already published"):
        ledger.set_embargo(entry, True, actor="maintainer")


def test_the_release_gate_now_sees_what_intake_wrote(tmp_path):
    """End to end: the answer on the form is what keeps the records out of the release."""
    entry = _entry()
    enrollment.apply_embargo(entry, _submission(tmp_path, HELD))
    entry.state = "approved"

    store = {"host_records": [{"LINEAGE_NAME": "TUMIG99",
                               "_source": entry.submission_id}]}
    result = release_gate.check(store, {entry.submission_id: entry})
    assert not result.ok
    assert "embargoed" in result.violations[0].reason


# ------------------------------------------------- an unreadable stage question holds
#
# publication_stage returns "" when the stage question is missing, reworded again, or
# answered with something the two prefix tests do not match. records_are_held returned
# False for all of those until 2026-08-10 -- publishable -- while its own docstring said
# the default was to hold.

class TestAnUnreadableStageQuestionHolds:

    def test_metadata_with_no_stage_question_holds(self):
        assert form_metadata.records_are_held(
            {"What is your first and last name?": "A Submitter"}) is True

    def test_an_answer_neither_pre_nor_post_holds(self):
        assert form_metadata.records_are_held(
            {"Is this submission pre-publication or post-publication?": "maybe"}) is True

    def test_a_third_rewording_of_the_question_holds(self):
        """The question has already been reworded once, on 2026-08-05."""
        assert form_metadata.records_are_held(
            {"Publication status?": "Not yet published"}) is True

    def test_a_readable_published_answer_still_does_not_hold(self):
        """The change must not hold every published study, only unreadable ones."""
        assert form_metadata.records_are_held({Q_STAGE: "Published"}) is False

    def test_a_readable_unpublished_answer_that_said_add_now_still_publishes(self):
        assert form_metadata.records_are_held(
            {Q_STAGE: "Unpublished",
             Q_EMBARGO: "Add them now, credited as unpublished"}) is False


def test_reading_the_form_leaves_an_audit_line_but_not_a_decision(tmp_path):
    """The one path that could un-embargo a submission left no trace at all.

    It must be visible in the history, and it must NOT count as a decision -- otherwise
    intake would stop re-reading a corrected metadata.json.
    """
    entry = _entry()
    enrollment.apply_embargo(entry, _submission(tmp_path, HELD))
    events = [event["event"] for event in entry.history]
    assert "embargo_from_form" in events
    assert not ledger.embargo_decided(entry)
