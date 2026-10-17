"""Tests for the edition report -- the document that accompanies a release.

The bug class this file is written against is not "the wrong number". It is **a section
that degrades to nothing rather than to an error**, which happened four separate times in
the curator report and was visible only to somebody who opened the rendered PDF. So the
tests here assert on the *document*: that every section is present, that a section with
nothing to say says so rather than vanishing, and that every class the markup uses is
actually styled.

The second property is a rule about people. The internal document names studies, their
data faults, and the submissions a release published. The public one must not, because a
fault attached to a study is public blame attached to the people who contributed the
records. That is enforced here rather than by whoever copies files on release day.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from malavi_curation import release_notes
from malavi_curation.release_diff import Edition, compare


def _lineage(name, acc="AB123456", genus="Haemoproteus", species="", sequence="ACGT"):
    return {"LINEAGE_NAME": name, "GENBANK_ACC": acc, "SEQ_LENGTH": "Full",
            "GENUS_NAME": genus, "SPECIES_NAME": species, "SEQUENCE": sequence}


def _host(lineage, species="Turdus migratorius", country="United States",
          site="Newark", reference="Smith et al 2020"):
    return {"LINEAGE_NAME": lineage, "ALT_NAME": "", "PARASITE_GENUS": "Haemoproteus",
            "ORDER_NAME": "Passeriformes", "FAMILY_NAME": "Turdidae",
            "GENUS_NAME": "Turdus", "SPECIES_NAME": species, "SUB_SPECIES_NAME": "",
            "HOST_STATUS": "", "HOST_AGE": "", "HOST_ENVIRONMENT": "",
            "CONTINENT_NAME": "North America", "COUNTRY_NAME": country,
            "COUNTRY_REGION_NAME": "", "SITE_NAME": site, "SITE_COORDINATES": "",
            "NUMBER_FOUND": "1", "NUMBER_TESTED": "10", "REFERENCE_NAME": reference,
            "COMMENT": ""}


def _reference(name, year="2020", title="A study", journal="Journal"):
    return {"REFERENCE_NAME": name, "PUBLICATION_YEAR": year, "TITLE": title,
            "JOURNAL_NAME": journal, "VOLUME_PAGES": "", "STUDY_TYPE": ""}


def _store(lineages=(), hosts=(), references=()):
    return {"lineages": list(lineages), "host_records": list(hosts),
            "references": list(references), "vector_records": [],
            "morpho_species": [], "alt_names": []}


def _edition(label, store):
    return Edition(label=label, tables=dict(store), summary=list(store["lineages"]),
                   sources={name: "(test)" for name in store})


def _diff(previous_store=None, current_store=None):
    """A comparison of two small editions, defaulting to one that changed nothing."""
    previous_store = previous_store if previous_store is not None else _store(
        lineages=[_lineage("TURDUS01")], hosts=[_host("TURDUS01")],
        references=[_reference("Smith et al 2020")])
    current_store = current_store if current_store is not None else previous_store
    return compare(_edition("2026-03-23", previous_store),
                   _edition("2026-08-14", current_store))


def _changed_diff():
    """An edition that adds a lineage, a study and a record."""
    previous = _store(lineages=[_lineage("TURDUS01")], hosts=[_host("TURDUS01")],
                      references=[_reference("Smith et al 2020")])
    current = _store(
        lineages=[_lineage("TURDUS01"), _lineage("TURDUS02", acc="PQ118834")],
        hosts=[_host("TURDUS01"),
               _host("TURDUS02", species="Turdus merula", country="Sweden",
                     reference="Jones et al 2026")],
        references=[_reference("Smith et al 2020"),
                    _reference("Jones et al 2026", year="2026", title="New work")])
    return compare(_edition("2026-03-23", previous), _edition("2026-08-14", current))


# The headings every edition report carries, whatever the edition did. A heading that
# disappears when its section has nothing to say is the failure this list guards.
REQUIRED_HEADINGS = (
    "At a glance", "Lineages added", "Studies added", "Records",
    "Hosts and geography", "Corrections to the Grand Lineage Summary",
)


class TestEverySectionIsPresent:

    @pytest.mark.parametrize("audience", release_notes.AUDIENCES)
    @pytest.mark.parametrize("heading", REQUIRED_HEADINGS)
    def test_a_section_with_nothing_to_report_still_appears(self, audience, heading):
        document = release_notes.render(_diff(), audience)
        assert heading in document, (
            f"{heading!r} is missing from the {audience} document. A section that "
            f"vanishes when it has nothing to say reads as 'nothing changed'.")

    def test_an_empty_category_says_so_rather_than_rendering_blank(self):
        document = release_notes.render(_diff(), "internal")
        assert "Nothing in this category." in document

    def test_the_internal_document_carries_the_sections_only_it_has(self):
        document = release_notes.render(
            _diff(), "internal",
            approval={"seed_rows": 10, "submissions_published": ["MALAVI-SUB-2026-000004"],
                      "violations": [], "gate_overridden": False},
            warnings=["3 host record(s) report more infections than birds tested"])
        for heading in ("Where these records came from", "Faults to look at", "Sign-off"):
            assert heading in document

    def test_the_headline_numbers_appear_as_tiles(self):
        document = release_notes.render(_changed_diff(), "public")
        assert "lineages added" in document
        assert 'class="tile"' in document


class TestTheAudiences:
    """The public document must carry nothing that names a fault or a submission."""

    def test_no_data_fault_reaches_the_public_document(self):
        fault = "3 host record(s) report more infections than birds tested: HST-000867"
        document = release_notes.render(
            _diff(), "public",
            approval={"seed_rows": 10, "submissions_published": ["MALAVI-SUB-2026-000004"],
                      "violations": ["a violation"], "gate_overridden": True},
            warnings=[fault])
        assert fault not in document
        assert "Faults to look at" not in document
        assert "MALAVI-SUB-2026-000004" not in document
        assert "Where these records came from" not in document
        assert "Sign-off" not in document
        assert "overridden" not in document.lower()

    def test_the_same_faults_do_reach_the_internal_document(self):
        """The other half of the rule: internal must not quietly drop them either."""
        fault = "3 host record(s) report more infections than birds tested: HST-000867"
        document = release_notes.render(
            _diff(), "internal",
            approval={"seed_rows": 10, "submissions_published": ["MALAVI-SUB-2026-000004"],
                      "violations": [], "gate_overridden": False},
            warnings=[fault])
        assert fault in document
        assert "MALAVI-SUB-2026-000004" in document

    def test_an_overridden_approval_gate_is_stated_plainly(self):
        document = release_notes.render(
            _diff(), "internal",
            approval={"seed_rows": 10, "submissions_published": [], "violations": [],
                      "gate_overridden": True})
        assert "The approval gate was overridden." in document

    def test_removed_record_detail_is_internal_only(self):
        previous = _store(lineages=[_lineage("TURDUS01")],
                          hosts=[_host("TURDUS01"), _host("TURDUS01", site="Dover")])
        current = _store(lineages=[_lineage("TURDUS01")], hosts=[_host("TURDUS01")])
        diff = compare(_edition("2026-03-23", previous), _edition("2026-08-14", current))
        assert "Records removed" in release_notes.render(diff, "internal")
        assert "Records removed" not in release_notes.render(diff, "public")

    def test_an_unknown_audience_is_refused(self):
        with pytest.raises(ValueError):
            release_notes.render(_diff(), "everyone")


class TestWhatTheDocumentSays:

    def test_a_new_lineage_appears_with_its_accession_and_study(self):
        document = release_notes.render(_changed_diff(), "public")
        assert "TURDUS02" in document
        assert "PQ118834" in document
        assert "Jones et al 2026" in document

    def test_an_uncomparable_lineage_name_is_named_rather_than_dropped(self):
        """REGRESSION: the section vanished when there were no comparable changes."""
        previous = _store(lineages=[_lineage("TURDUS01", acc="KF314763")])
        current = _store(lineages=[_lineage("TURDUS01", acc="KF314763"),
                                   _lineage("TURDUS01", acc="PQ118836")])
        diff = compare(_edition("2026-03-23", previous), _edition("2026-08-14", current))
        document = release_notes.render(diff, "public")
        assert "could not be compared row by row" in document
        assert "TURDUS01" in document

    def test_an_uncompared_table_says_so_instead_of_showing_a_zero(self):
        previous = Edition(label="2026-03-23",
                           tables={"lineages": [_lineage("TURDUS01")]},
                           summary=[_lineage("TURDUS01")],
                           missing=["host_records", "vector_records", "references",
                                    "morpho_species", "alt_names"])
        current = _edition("2026-08-14", _store(lineages=[_lineage("TURDUS01")]))
        document = release_notes.render(compare(previous, current), "internal")
        assert "Incomplete comparison." in document
        assert "not compared" in document

    def test_the_corrections_section_explains_that_it_is_a_correction(self):
        """"277 lineages changed region" reads as a broken build without this.

        The explanation belongs with the corrections themselves, so this builds an
        edition that actually has one: the summary's derived columns differ while the
        lineage facts do not, which is exactly what a rebuild of a stale summary does.
        """
        lineage = _lineage("TURDUS01")
        previous = Edition(label="2026-03-23", tables=_store(lineages=[lineage]),
                           summary=[dict(lineage, SUM_HOST="1", NORTH_AMERICA="")])
        current = Edition(label="2026-08-14", tables=_store(lineages=[lineage]),
                          summary=[dict(lineage, SUM_HOST="2", NORTH_AMERICA="1")])
        document = release_notes.render(compare(previous, current), "public")

        assert "recomputed from the host and vector records" in document
        assert "These are corrections, not new data" in document
        assert "NORTH_AMERICA" in document

    def test_no_corrections_is_stated_rather_than_left_blank(self):
        # Probed on whitespace-collapsed text: the source wraps its prose across lines,
        # so a literal phrase probe fails on a document that is perfectly correct.
        document = " ".join(release_notes.render(_diff(), "public").split())
        assert "Corrections to the Grand Lineage Summary" in document
        assert "None. All 1 lineages present in both editions carry the same derived " \
               "tallies" in document

    def test_a_delta_is_signed(self):
        document = release_notes.render(_changed_diff(), "public")
        assert "+1" in document


class TestEscaping:
    """Reference titles and site names come from submitted workbooks."""

    def test_markup_in_a_submitted_value_is_escaped(self):
        current = _store(
            lineages=[_lineage("TURDUS01")],
            hosts=[_host("TURDUS01", reference="Evil et al 2026")],
            references=[_reference("Evil et al 2026",
                                   title="<script>alert('x')</script>")])
        diff = compare(_edition("2026-03-23", _store(lineages=[_lineage("TURDUS01")])),
                       _edition("2026-08-14", current))
        for audience in release_notes.AUDIENCES:
            document = release_notes.render(diff, audience)
            assert "<script>" not in document
            assert "&lt;script&gt;" in document


class TestTheStylesheet:

    def test_every_class_the_document_uses_is_styled(self):
        """REGRESSION (curator report): a class used and never styled renders as plain
        text -- visibly wrong only to somebody who opens the rendered PDF."""
        source = Path(release_notes.__file__).read_text(encoding="utf-8")
        stylesheet = (release_notes._stylesheet("test")
                      + release_notes._EXTRA_STYLE)

        used = set()
        for match in re.findall(r'class="([a-z0-9 _-]+)"', source):
            used.update(part for part in match.split() if part)
        defined = set(re.findall(r"\.([a-z][a-z0-9-]*)", stylesheet))
        missing = sorted(used - defined)
        assert not missing, f"classes used but never styled: {missing}"

    def test_the_printed_page_label_is_the_documents_own(self):
        """The stylesheet is shared with the curator report, which this is not."""
        internal = release_notes.render(_diff(), "internal")
        public = release_notes.render(_diff(), "public")
        assert "MalAvi edition report — page" in internal
        assert "MalAvi curator report" not in internal
        assert "release notes — page" in public

    def test_a_lost_placeholder_raises_instead_of_shipping_silently(self):
        """An unasserted str.replace that matches nothing is how the unstyled verdict
        block shipped. The substitution must fail loudly instead."""
        from malavi_curation import report_html

        original = report_html._STYLE
        try:
            report_html._STYLE = "body { color: red; }"      # no placeholders at all
            with pytest.raises(AssertionError):
                report_html._stylesheet("anything")
        finally:
            report_html._STYLE = original

    def test_the_print_rules_survive_into_the_document(self):
        document = release_notes.render(_diff(), "internal")
        assert "@media print" in document
        printed = document.split("@media print")[1]
        assert "table-header-group" in printed      # headers repeat across pages
        assert "counter(page)" in document


class TestWriting:

    def test_both_documents_are_written_in_both_formats(self, tmp_path):
        written = release_notes.write_documents(_diff(), tmp_path, "2026-08-14")
        names = sorted(path.name for path in tmp_path.iterdir())
        assert "release_notes_2026-08-14.html" in names
        assert "release_notes_2026-08-14_public.html" in names
        assert written["internal"]["html"].endswith("release_notes_2026-08-14.html")

    def test_a_missing_pdf_renderer_costs_the_html_nothing(self, tmp_path, monkeypatch):
        """WeasyPrint pulls in system libraries that are not everywhere. A missing
        renderer must degrade visibly, not cost the maintainer their record."""
        import builtins
        real_import = builtins.__import__

        def no_weasyprint(name, *args, **kwargs):
            if name == "weasyprint":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_weasyprint)
        written = release_notes.write_documents(_diff(), tmp_path, "2026-08-14")
        assert written["pdf_unavailable"] is True
        assert written["internal"]["pdf"] is None
        assert (tmp_path / "release_notes_2026-08-14.html").is_file()

    def test_only_the_requested_audience_is_written(self, tmp_path):
        release_notes.write_documents(_diff(), tmp_path, "2026-08-14",
                                      audiences=("public",))
        names = sorted(path.name for path in tmp_path.iterdir())
        assert not any(name == "release_notes_2026-08-14.html" for name in names)
        assert any(name.endswith("_public.html") for name in names)


# ------------------------------------------------------------- the 2026-08-11 review fixes

class TestNotComparedIsNeverPrintedAsZero:
    """The document must never state a number it did not compute."""

    def _summary_only(self):
        """A previous edition supplying nothing but its Grand Lineage Summary."""
        previous = Edition(label="2026-03-23",
                           tables={"lineages": [_lineage("TURDUS01")]},
                           summary=[_lineage("TURDUS01")],
                           missing=["host_records", "vector_records", "references",
                                    "morpho_species", "alt_names"])
        current = _edition("2026-08-14", _store(lineages=[_lineage("TURDUS01")]))
        return compare(previous, current)

    def test_the_studies_tile_says_not_compared_rather_than_zero(self):
        """REGRESSION: every other tile guarded on `compared` and this one did not, so a
        missing references CSV printed '0 studies added' for an edition that added
        thirty — while the totals table beside it said 'not compared'."""
        document = release_notes.render(self._summary_only(), "public")
        assert "0</span><span class=\"k\">studies added" not in document.replace(" ", "")
        assert "Studies added" in document
        assert "Not compared" in document

    def test_the_studies_section_does_not_claim_none_were_added(self):
        document = " ".join(release_notes.render(self._summary_only(), "public").split())
        assert "0 study/studies enter MalAvi" not in document


class TestFaultsNotChecked:
    """"The build flagged nothing" and "nothing looked" are different statements."""

    def test_no_build_says_not_checked(self):
        document = " ".join(release_notes.render(_diff(), "internal", warnings=None).split())
        assert "Not checked." in document
        assert "The build flagged nothing" not in document
        assert "clean bill of health" in document

    def test_a_build_that_found_nothing_says_so(self):
        document = release_notes.render(_diff(), "internal", warnings=[])
        assert "The build flagged nothing." in document

    def test_the_approval_section_never_vanishes(self):
        """REGRESSION: _approval returned "" and render dropped empty sections, so the
        whole heading disappeared from any report not produced by a build."""
        document = release_notes.render(_diff(), "internal", approval=None)
        assert "Where these records came from" in document
        assert "Not recorded." in document


def test_every_internal_only_section_is_absent_from_the_public_document():
    """REGRESSION: INTERNAL_ONLY_SECTIONS was referenced by no code at all — the split was
    two hard-coded `if audience == "internal"` checks and a RUNBOOK sentence claiming the
    constant enforced it. A fifth section added without a guard would have leaked.

    This test iterates the tuple, so a new entry is covered the moment it is added.
    """
    approval = {"seed_rows": 10, "submissions_published": ["MALAVI-SUB-2026-000004"],
                "violations": ["a violation"], "gate_overridden": True}
    warnings = ["3 host record(s) report more infections than birds tested: HST-000867"]

    internal = release_notes.render(_diff(), "internal", approval=approval,
                                    warnings=warnings)
    public = release_notes.render(_diff(), "public", approval=approval, warnings=warnings)

    # Each internal-only section must contribute something to the internal document...
    headings = {"approval": "Where these records came from",
                "faults": "Faults to look at",
                "signoff": "Sign-off"}
    for name in release_notes.INTERNAL_ONLY_SECTIONS:
        assert name in headings, (
            f"{name!r} is in INTERNAL_ONLY_SECTIONS but this test does not know its "
            f"heading; add it so the rule stays covered")
        assert headings[name] in internal, f"{name} missing from the internal document"
        assert headings[name] not in public, f"{name} LEAKED into the public document"


class TestRecordsCorrected:
    """A correction to published data must be visible in the edition report.

    This is how a data-correction pass is tracked: the flipped-sign longitudes and the
    misspelled vector methods are corrections to records MalAvi already holds, and a
    correction that does not appear in the report is a change to published data with no
    record that it happened.
    """

    def _corrected(self):
        previous = _store(lineages=[_lineage("TURDUS01")],
                          hosts=[_host("TURDUS01")],
                          references=[_reference("Smith et al 2020")])
        fixed = dict(_host("TURDUS01"))
        fixed["SITE_COORDINATES"] = "-14°50', -043°59'"
        before = dict(_host("TURDUS01"))
        before["SITE_COORDINATES"] = "-14°50', 043°59'"
        previous["host_records"] = [before]
        current = _store(lineages=[_lineage("TURDUS01")], hosts=[fixed],
                         references=[_reference("Smith et al 2020")])
        return compare(_edition("2026-03-23", previous), _edition("2026-08-14", current))

    @pytest.mark.parametrize("audience", release_notes.AUDIENCES)
    def test_a_corrected_field_is_shown_with_both_values(self, audience):
        document = release_notes.render(self._corrected(), audience)
        assert "Records corrected" in document
        assert "SITE_COORDINATES" in document
        assert "043°59&#x27;" in document or "043°59'" in document

    def test_a_correction_is_not_reported_as_an_addition(self):
        """The distinction the section exists to make: the record is the same record."""
        diff = self._corrected()
        entry = diff["tables"]["host_records"]
        assert (entry["added"], entry["removed"], entry["modified"]) == (0, 0, 1)
        document = " ".join(release_notes.render(diff, "public").split())
        assert "these are corrections, not additions" in document

    def test_a_corrected_study_is_reported(self):
        """REGRESSION: reference modifications were computed and rendered nowhere."""
        previous = _store(lineages=[_lineage("TURDUS01")],
                          references=[_reference("Smith et al 2020", title="Typo")])
        current = _store(lineages=[_lineage("TURDUS01")],
                         references=[_reference("Smith et al 2020", title="Corrected")])
        diff = compare(_edition("2026-03-23", previous), _edition("2026-08-14", current))
        document = release_notes.render(diff, "public")
        assert "References (studies)" in document
        assert "Corrected" in document
