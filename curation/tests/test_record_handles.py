"""The short handle a curator quotes to name one record.

The property that matters: the label on the page and the label the correction path
resolves must come from one function. Generated in two places they could disagree, and a
correction would land on a different row from the one the curator was looking at --
silently, because both rows are plausible.
"""
from __future__ import annotations

from malavi_curation import record_handles


def _submission(records=(), vectors=()):
    return {"records": list(records), "vectors": list(vectors)}


def _record(lineage="TUMIG31", host="Turdus migratorius", site="White Clay Creek", row=3):
    return {"lineage_name": lineage, "host_species": host, "site": site,
            "source": {"row": row}}


def _vector(lineage="TUMIG31", species="Culex pipiens", row=2):
    return {"lineage_name": lineage, "vector_species": species, "source": {"row": row}}


def test_handles_number_from_one_in_report_order():
    entries = record_handles.handles(_submission([_record(), _record(), _record()]))
    assert [entry.handle for entry in entries] == ["R1", "R2", "R3"]


def test_one_sequence_runs_across_records_and_vectors():
    """A curator quoting R4 should never have to say which table they meant."""
    entries = record_handles.handles(
        _submission([_record(), _record()], [_vector(), _vector()]))
    assert [entry.handle for entry in entries] == ["R1", "R2", "R3", "R4"]
    assert [entry.kind for entry in entries] == [
        "records", "records", "vectors", "vectors"]


def test_a_handle_knows_which_store_table_it_becomes():
    entries = record_handles.handles(_submission([_record()], [_vector()]))
    assert entries[0].table == "host_records"
    assert entries[1].table == "vector_records"


def test_a_handle_resolves_back_to_its_record():
    submission = _submission([_record(row=3), _record(lineage="TUMIG10", row=5)])
    entry = record_handles.resolve(submission, "R2")
    assert entry.workbook_row == 5
    assert "TUMIG10" in entry.summary


def test_a_handle_a_person_typed_still_resolves():
    """A form is typed by a person. Refusing 'r2 ' would cost a correction."""
    submission = _submission([_record(), _record()])
    for typed in ("R2", "r2", " R2 ", "R 2"):
        assert record_handles.resolve(submission, typed).handle == "R2", typed


def test_an_unknown_handle_resolves_to_nothing_rather_than_a_guess():
    """Most likely the curator is reading a report from an earlier revision. That needs a
    person, not the nearest match."""
    submission = _submission([_record()])
    assert record_handles.resolve(submission, "R9") is None
    assert record_handles.resolve(submission, "") is None
    assert record_handles.resolve(submission, "C1") is None


def test_the_summary_lets_a_curator_confirm_the_right_row():
    entry = record_handles.handles(_submission([_record()]))[0]
    assert entry.summary == "TUMIG31 / Turdus migratorius / White Clay Creek"


def test_the_report_and_the_resolver_agree():
    """The whole point: one function, two readers."""
    from malavi_curation import report_html

    submission = {"records": [_record(), _record(lineage="TUMIG10")],
                  "vectors": [_vector()], "reference": {"title": "A study"}}
    rendered = report_html._records_section(submission)
    for entry in record_handles.handles(submission):
        assert f">{entry.handle}<" in rendered, f"{entry.handle} is not on the page"
