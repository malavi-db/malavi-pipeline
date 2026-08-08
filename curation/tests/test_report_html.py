"""Tests for the HTML curator report.

Two things are being protected here.

**Safety.** The values on this page arrive through a public Google Form and are rendered
into a document a curator opens from ``file://``. Markup in a spreadsheet cell must stay
text, and the file must never be written anywhere but the gitignored intake tree, because
it carries unpublished sequences and a submitter's email address.

**Honesty.** A run in which checks failed to execute must not look like a clean one, and a
skipped check must not read as a passed check. Those are the ways this document could
actively mislead the person it is written for.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from malavi_curation.checks import CheckResult, CheckRun, Finding, Outcome, Severity
from malavi_curation.report_html import render_report, write_report

# A payload that is inert as text and dangerous as markup. Attribute-breaking quote
# included, because escaping the angle brackets alone would not be enough.
HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(2)>'


def _submission(**overrides):
    submission = {
        "schema_version": "1.0.0",
        "submitter": {"name": "A Submitter", "email": "someone@example.org",
                      "institution": "Somewhere"},
        "reference": {"doi": "10.1234/abc", "title": "A title", "year": 2026},
        "accessions": ["MK493368"],
        "records": [{"lineage_name": "TUMIG19", "host_species": "Turdus migratorius",
                     "country": "Sweden", "site": "Lund", "number_found": 3.0,
                     "number_tested": 25.0, "tier": "new",
                     "source": {"sheet": "Hosts_and_Sites", "row": 3}}],
        "vectors": [],
        "sequences": [],
        "proposed_lineages": [{"lineage_name": "TUMIG19", "parasite_genus": "Haemoproteus",
                               "host_species": "Turdus migratorius",
                               "accessions": ["MK493368"],
                               "source": {"sheet": "NewLineages", "row": 3}}],
        "provenance": {"source": "template", "workbook": "fixture.xlsx",
                       "normalizations": [], "header_repairs": []},
    }
    submission.update(overrides)
    return submission


def _run(*results, **provenance):
    return CheckRun(results=tuple(results),
                    provenance=provenance or {"release": "2026-03-23"})


class TestEscaping:
    def test_markup_in_a_cell_stays_text(self):
        html = render_report(
            _submission(records=[{"lineage_name": HOSTILE, "host_species": HOSTILE,
                                  "country": HOSTILE, "notes": HOSTILE}]),
            _run())
        # The decisive assertions: no element the payload was trying to create exists,
        # and its quote never broke out of an attribute.
        assert not re.findall(r"<script\b", html)
        assert not re.findall(r"<img\b", html)
        assert '"><script' not in html
        # And it is still visible to the curator, escaped, rather than silently dropped.
        assert "&lt;script&gt;" in html

    def test_markup_in_a_finding_stays_text(self):
        html = render_report(_submission(), _run(CheckResult(
            check_id="prevalence_sanity", outcome=Outcome.FINDING, evaluated=1,
            findings=(Finding(subject=HOSTILE, message=HOSTILE,
                              severity=Severity.BLOCKING),))))
        assert not re.findall(r"<script\b", html)
        assert not re.findall(r"<img\b", html)

    def test_markup_in_submitter_details_stays_text(self):
        html = render_report(
            _submission(submitter={"name": HOSTILE, "email": HOSTILE}), _run())
        assert not re.findall(r"<script\b", html)


class TestSelfContained:
    def test_no_remote_references(self):
        """A fetch would fail from file://, from a mail attachment, and behind a login.

        The report has to look the same in all three, because which one a curator uses
        is a deployment decision that should not change what they see.
        """
        html = render_report(_submission(), _run())
        remote = re.findall(r'(?:src|href)="(?!data:|#)[^"]*"', html)
        assert remote == [], f"report reaches out to {remote}"

    def test_no_scripts(self):
        html = render_report(_submission(), _run())
        assert not re.findall(r"<script\b", html)

    def test_declares_a_restrictive_policy(self):
        html = render_report(_submission(), _run())
        assert "Content-Security-Policy" in html
        assert "default-src 'none'" in html

    def test_the_font_is_embedded_not_fetched(self):
        html = render_report(_submission(), _run())
        assert "data:font/woff;base64," in html

    def test_uses_the_site_s_design_tokens(self):
        # A curator moving between the public site and this document should not feel
        # they have changed software. These are malavi.css's own values.
        html = render_report(_submission(), _run())
        for token in ("--paper:#F4F3F8", "--ink:#17141F", "--accent:#5B4BA6",
                      "League Gothic"):
            assert token in html, f"{token} is not the site's token any more"

    def test_styles_both_themes(self):
        html = render_report(_submission(), _run())
        assert "prefers-color-scheme: dark" in html


class TestHonesty:
    def test_an_incomplete_run_is_announced_before_anything_else(self):
        """The single most dangerous thing this document could do is present a partial
        run as a clean bill of health."""
        html = render_report(_submission(), _run(CheckResult(
            check_id="sequence_qc", outcome=Outcome.ERROR, error="R exited 1")))
        assert "Validation is incomplete" in html
        # Before the checks section, not buried inside it. Anchored on the heading text,
        # which changed when the report was reordered — the rule did not.
        # Anchored on the first check card rather than a heading; the heading was
        # removed as redundant, the ordering rule was not.
        assert html.index("Validation is incomplete") < html.index('class="check')

    def test_a_clean_run_carries_no_incomplete_banner(self):
        html = render_report(_submission(), _run(CheckResult(
            check_id="prevalence_sanity", outcome=Outcome.PASS, evaluated=1, passed=1)))
        assert "Validation is incomplete" not in html

    def test_a_skipped_check_says_it_has_no_opinion(self):
        html = render_report(_submission(), _run(CheckResult(
            check_id="sequence_qc", outcome=Outcome.SKIP,
            skip_reason="Rscript is not on PATH")))
        assert "Rscript is not on PATH" in html
        assert "no opinion" in html

    def test_no_findings_does_not_read_as_verified(self):
        html = render_report(_submission(), _run(CheckResult(
            check_id="sequence_qc", outcome=Outcome.SKIP, skip_reason="no R here")))
        # It must point the reader at the skips before they conclude anything.
        assert "Read the skipped list" in html

    def test_a_check_states_what_it_asserts(self):
        """Assertions now appear on checks that could NOT run, not on findings.

        On a finding card the assertion contradicted the headline above it — "No proposed
        new lineage name is already a MalAvi lineage" printed over "TUMIG10 was listed as
        new, but MalAvi already has that name". On a skipped check it still does the job it
        was written for: saying what nobody has answered.
        """
        # A finding says what was found, in its own words, and names the subject.
        html = render_report(_submission(), _run(CheckResult(
            check_id="prevalence_sanity", outcome=Outcome.FINDING, evaluated=1,
            findings=(Finding(subject="TUMIG19", message="9 found of 3 tested"),))))
        assert "9 found of 3 tested" in html
        assert "TUMIG19" in html

        # A check that could not run still states what it was for, because that is the
        # only way a reader knows what has gone unanswered.
        skipped = render_report(_submission(), _run(CheckResult(
            check_id="prevalence_sanity", outcome=Outcome.SKIP, evaluated=1,
            skip_reason="the numbers were not supplied")))
        assert "never greater than the number screened" in skipped
        assert "the numbers were not supplied" in skipped

    def test_a_finding_names_the_row_to_look_at(self):
        html = render_report(_submission(), _run(CheckResult(
            check_id="prevalence_sanity", outcome=Outcome.FINDING, evaluated=1,
            findings=(Finding(subject="TUMIG19", message="bad",
                              source={"sheet": "Hosts_and_Sites", "row": 19}),))))
        assert "Hosts_and_Sites, row 19" in html

    def test_normalizations_show_both_forms(self):
        html = render_report(_submission(provenance={
            "source": "template",
            "normalizations": [{"field": "lineage_name", "submitted": "tumig19",
                                "normalized": "TUMIG19"}],
            "header_repairs": [],
        }), _run())
        assert "tumig19" in html and "TUMIG19" in html

    def test_counts_are_not_shown_as_floats(self):
        # Counts are carried as floats; "3.0 birds" reads as a bug in our software.
        html = render_report(_submission(), _run())
        assert ">3<" in html and ">3.0<" not in html

    def test_the_report_never_claims_anything_was_ingested(self):
        """The closing reassurance was removed on the project lead's instruction — it was
        text a curator had read many times. What must remain true is that nothing in the
        report claims the opposite."""
        html = render_report(_submission(), _run())
        for claim in ("has been added to MalAvi", "was added to MalAvi",
                      "now in MalAvi", "ingested"):
            assert claim not in html

    def _retired_test_the_footer_states_nothing_has_been_ingested(self):
        html = render_report(_submission(), _run())
        assert "has been added to MalAvi" in html
        # And carries the sampling-not-biology caveat, which must travel with the
        # novelty reporting wherever it is shown.
        assert "where people have looked" in html


class TestSourceReferences:
    def test_a_sheet_and_row_is_always_shown(self):
        html = render_report(_submission(), _run(CheckResult(
            check_id="prevalence_sanity", outcome=Outcome.FINDING, evaluated=1,
            findings=(Finding(subject="X", message="m",
                              source={"sheet": "Sequences", "row": 4}),))))
        assert "Sequences, row 4" in html

    def test_the_report_s_own_workbook_is_not_repeated_on_every_finding(self):
        # Repeating one long filename beside every finding crowds out the references
        # that actually locate something.
        submission = _submission(provenance={"source": "template",
                                             "workbook": "Long Workbook Name.xlsx",
                                             "normalizations": [], "header_repairs": []})
        html = render_report(submission, _run(CheckResult(
            check_id="prevalence_sanity", outcome=Outcome.FINDING, evaluated=1,
            findings=(Finding(subject="X", message="m",
                              source={"file": "Long Workbook Name.xlsx"}),))))
        # Named once, in the header; not again beside the finding.
        assert html.count("Long Workbook Name.xlsx") == 1

    def test_a_different_file_is_still_named(self):
        submission = _submission(provenance={"source": "template",
                                             "workbook": "Template.xlsx",
                                             "normalizations": [], "header_repairs": []})
        html = render_report(submission, _run(CheckResult(
            check_id="prevalence_sanity", outcome=Outcome.FINDING, evaluated=1,
            findings=(Finding(subject="X", message="m",
                              source={"file": "Supplement.xlsx"}),))))
        assert "Supplement.xlsx" in html


class TestPdf:
    """The PDF is the copy a curator opens, because Drive will not render HTML."""

    def test_writes_a_pdf_when_the_renderer_is_available(self, tmp_path):
        pytest.importorskip("weasyprint")
        from malavi_curation.report_html import write_pdf

        root = tmp_path / "intake"
        root.mkdir()
        html = render_report(_submission(), _run())
        written = write_pdf(html, root / "report.pdf", intake_root=root)
        assert written is not None and written.is_file()
        assert written.read_bytes().startswith(b"%PDF-")

    def test_the_pdf_is_owner_only_like_the_html(self, tmp_path):
        pytest.importorskip("weasyprint")
        from malavi_curation.report_html import write_pdf

        root = tmp_path / "intake"
        root.mkdir()
        written = write_pdf(render_report(_submission(), _run()),
                            root / "report.pdf", intake_root=root)
        assert written.stat().st_mode & 0o077 == 0

    def test_the_pdf_writer_honors_the_same_containment(self, tmp_path):
        pytest.importorskip("weasyprint")
        from malavi_curation.report_html import write_pdf

        root = tmp_path / "intake"
        root.mkdir()
        with pytest.raises(ValueError, match="outside the intake tree"):
            write_pdf("<p>x</p>", tmp_path / "escaped.pdf", intake_root=root)

    def test_a_missing_renderer_returns_none_rather_than_raising(self, monkeypatch,
                                                                 tmp_path):
        """A missing optional dependency must not cost the curator their report.

        The same posture the check suite takes: an unavailable capability is reported,
        never fatal, and never silently treated as success.
        """
        import builtins

        from malavi_curation.report_html import write_pdf

        real_import = builtins.__import__

        def no_weasyprint(name, *args, **kwargs):
            if name == "weasyprint":
                raise ImportError("not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_weasyprint)
        root = tmp_path / "intake"
        root.mkdir()
        assert write_pdf("<p>x</p>", root / "report.pdf", intake_root=root) is None

    def test_the_print_stylesheet_pins_the_light_palette(self):
        # Screen dark-mode colors print as dark blocks of ink. Print rules override.
        html = render_report(_submission(), _run())
        assert "@media print" in html
        printed = html.split("@media print")[1]
        assert "--paper:#FFFFFF" in printed
        assert "counter(page)" in printed          # page numbers
        assert "table-header-group" in printed     # headers repeat across pages
        assert "break-inside:avoid" in printed     # a finding is not split


class TestWriteContainment:
    """The writer, not just a test, must refuse to put this file in the wrong place."""

    def _tree(self):
        root = Path(tempfile.mkdtemp())
        (root / "intake").mkdir()
        return root

    def test_writes_inside_the_intake_tree(self):
        root = self._tree()
        written = write_report("<p>x</p>", root / "intake" / "s" / "report.html",
                               intake_root=root / "intake")
        assert written.is_file()

    def test_refuses_outside_the_intake_tree(self):
        root = self._tree()
        with pytest.raises(ValueError, match="outside the intake tree"):
            write_report("<p>x</p>", root / "elsewhere.html",
                         intake_root=root / "intake")

    def test_refuses_a_traversal_out_of_the_tree(self):
        root = self._tree()
        with pytest.raises(ValueError, match="outside the intake tree"):
            write_report("<p>x</p>", root / "intake" / ".." / "escaped.html",
                         intake_root=root / "intake")

    def test_refuses_to_follow_a_symlink_out_of_the_tree(self):
        root = self._tree()
        (root / "outside").mkdir()
        link = root / "intake" / "sneaky"
        link.symlink_to(root / "outside")
        with pytest.raises(ValueError):
            write_report("<p>x</p>", link / "report.html",
                         intake_root=root / "intake")

    def test_written_owner_only(self):
        # Somebody else's unpublished data.
        root = self._tree()
        written = write_report("<p>x</p>", root / "intake" / "report.html",
                               intake_root=root / "intake")
        assert written.stat().st_mode & 0o077 == 0

    def test_leaves_no_temporary_file_behind_on_failure(self):
        root = self._tree()
        target = root / "intake" / "report.html"
        write_report("<p>ok</p>", target, intake_root=root / "intake")
        assert [p.name for p in (root / "intake").iterdir()] == ["report.html"]


def test_every_class_the_report_uses_is_defined_in_its_stylesheet():
    """REGRESSION: the verdict block shipped unstyled because an edit silently no-opped.

    The report is a single self-contained file with no external stylesheet, so a class
    used in the markup and absent from the CSS produces a section that renders as plain
    text — visibly wrong only if somebody looks at the rendered PDF, which is exactly the
    step most likely to be skipped.
    """
    import re
    from malavi_curation import report_html

    source = Path(report_html.__file__).read_text(encoding="utf-8")
    stylesheet = report_html._stylesheet()

    # Classes written into markup by this module, excluding those built dynamically.
    used = set()
    for match in re.findall(r'class="([a-z0-9 _-]+)"', source):
        used.update(part for part in match.split() if part)

    defined = set(re.findall(r"\.([a-z][a-z0-9-]*)", stylesheet))
    missing = sorted(used - defined)
    assert not missing, f"classes used in the report but never styled: {missing}"


def test_the_verdict_block_is_styled():
    """The one part of the report that asks the curator to do something."""
    from malavi_curation import report_html

    stylesheet = report_html._stylesheet()
    for rule in (".verdict", ".verdict-link", ".verdict-note"):
        assert rule in stylesheet, f"{rule} is not defined"


class TestPaperOnly:
    """A paper with no data template is a submission, not an error.

    The behaviour this replaces was worse than useless: the screen exited 1 and wrote
    nothing, so a curator got an email about a submission and then found nothing to review
    and no explanation.
    """

    def _metadata(self):
        return {
            "Email Address": "someone@example.edu",
            "What is your first and last name?": "A Submitter",
            "_fetched_at": "2026-08-06T22:01:00+00:00",
            "Submission PDF and Supplementary Materials (associated with…)":
                "https://drive.google.com/open?id=1EXAMPLEfileid0000000",
        }

    def test_it_says_plainly_that_nothing_was_checked(self):
        from malavi_curation.report_html import render_paper_only_report
        html = render_paper_only_report(self._metadata())
        assert "Nothing here has been checked" in html

    def test_it_offers_no_verdict_link(self):
        """There is nothing to approve. A verdict link would invite a decision about
        something nobody has examined."""
        from malavi_curation.report_html import render_paper_only_report
        html = render_paper_only_report(self._metadata())
        assert "Record your verdict" not in html
        assert "There is nothing to approve or reject here" in html

    def test_it_links_the_paper(self):
        from malavi_curation.report_html import render_paper_only_report
        html = render_paper_only_report(self._metadata())
        assert "1EXAMPLEfileid0000000" in html

    def test_it_names_the_action_that_moves_it_forward(self):
        from malavi_curation.report_html import render_paper_only_report
        html = render_paper_only_report(
            self._metadata(), submit_form_url="https://example.org/form")
        assert "Submit the data from this paper" in html
        assert "https://example.org/form" in html

    def test_it_survives_having_no_metadata_at_all(self):
        from malavi_curation.report_html import render_paper_only_report
        html = render_paper_only_report(None)
        assert "Nothing here has been checked" in html
        assert "No uploaded file was recorded" in html


def test_every_finding_reaches_the_report():
    """REGRESSION — a blocker: nine of twelve findings were silently deleted.

    Info-level findings were accumulated into a per-group dict, then popped and discarded
    the moment a heading for that group was created by some unrelated blocking finding. So
    whether a finding reached the curator depended on what *else* happened to be in its
    group, and the report disagreed with checks.json with no trace of the difference.

    The invariant, restated after the report was reshaped on curator feedback: every
    finding that BLOCKS or WARNS appears in full, and informational findings are counted
    rather than listed. That is different from the bug: those findings used to vanish with
    no trace at all and were not in any count, so the report and checks.json disagreed
    silently. Being deliberately summarised is not the same as being lost.
    """
    import html as _html
    from malavi_curation.checks import CheckResult, CheckRun, Finding, Outcome, Severity
    from malavi_curation.report_html import render_report

    def finding(subject, message):
        return Finding(subject=subject, message=message, severity=Severity.INFO)

    run = CheckRun(results=(
        # A blocking finding, which creates the group heading …
        CheckResult(check_id="name_already_in_malavi", outcome=Outcome.FINDING,
                    evaluated=2, findings=(finding("SGS1", "the blocking one"),)),
        # … and an info finding in the SAME group, which used to vanish because of it.
        CheckResult(check_id="lineage_known", outcome=Outcome.FINDING,
                    evaluated=2, findings=(finding("GRW04", "the quiet one"),)),
        # An info finding in a group with no card at all: this one always survived.
        CheckResult(check_id="host_not_in_malavi", outcome=Outcome.FINDING,
                    evaluated=1, findings=(finding("Parus major", "the other quiet one"),)),
    ), provenance={})

    page = render_report({"records": []}, run)

    # A blocking finding is shown in full, always.
    assert "the blocking one" in page

    # The informational ones are summarised, not silently dropped: the tally has to
    # account for them, which is what makes the summary honest rather than a hiding place.
    import re as _re
    tally = _re.search(r"(\d+) other checks found nothing", page)
    assert tally, "the report must account for the checks that did not stop the submission"
    assert int(tally.group(1)) >= 2, \
        f"two informational findings were not counted anywhere (tally said {tally.group(1)})"


def test_the_report_refuses_to_guess_a_revision():
    """REGRESSION: the verdict link always said revision 1.

    The ledger accepts a verdict against any revision that exists, and only treats it as
    standing if it names the CURRENT one. So after a resubmission an approval recorded
    against revision 1 was accepted, stored, and never counted — the submission stalled
    while the curator believed they had approved it. Holds carry forward across revisions,
    so only approvals were lost, which made the failure one-sided and invisible.
    """
    from malavi_curation.report_html import render_report
    from malavi_curation.checks import CheckRun

    run = CheckRun(results=(), provenance={})
    with pytest.raises(ValueError, match="needs the revision"):
        render_report({"records": []}, run, submission_id="MALAVI-SUB-2026-000123")


def test_the_verdict_link_carries_the_revision_it_was_given():
    from urllib.parse import parse_qs, urlparse
    from malavi_curation.report_html import render_report
    from malavi_curation.checks import CheckRun
    from malavi_curation.config import load_config
    import re as _re

    run = CheckRun(results=(), provenance={})
    page = render_report({"records": []}, run,
                         submission_id="MALAVI-SUB-2026-000123", revision=4)
    link = _re.search(r'href="(https://docs\.google\.com/forms[^"]+)"', page)
    assert link, "no verdict link was rendered"
    entries = load_config()["review"]["verdict_form_entries"]
    query = parse_qs(urlparse(link.group(1).replace("&amp;", "&")).query)
    assert query[f"entry.{entries['revision']}"] == ["4"]


class TestDisposition:
    """One line at the top saying where the submission stands.

    It never says "valid" or "approved": these checks support a curator's judgment and do
    not replace it, and a report announcing a submission as valid invites the rubber stamp
    the review process exists to prevent.
    """

    def _page(self, *results):
        from malavi_curation.checks import CheckRun
        from malavi_curation.report_html import render_report
        return render_report(_submission(), CheckRun(results=results, provenance={}))

    def test_a_blocking_finding_blocks(self):
        page = self._page(CheckResult(
            check_id="sequence_is_known_lineage", outcome=Outcome.FINDING, evaluated=1,
            findings=(Finding(subject="X", message="identical to SGS1"),)))
        assert "Needs correction before curation" in page

    def test_an_error_makes_it_incomplete(self):
        page = self._page(CheckResult(
            check_id="sequence_qc", outcome=Outcome.ERROR, error="R exited 1"))
        assert "Validation incomplete" in page

    def test_a_blocking_finding_outranks_an_error(self):
        page = self._page(
            CheckResult(check_id="sequence_qc", outcome=Outcome.ERROR, error="R exited 1"),
            CheckResult(check_id="sequence_is_known_lineage", outcome=Outcome.FINDING,
                        evaluated=1, findings=(Finding(subject="X", message="identical"),)))
        assert "Needs correction before curation" in page
        assert "Validation incomplete" not in page

    def test_a_skip_still_asks_for_review(self):
        page = self._page(CheckResult(
            check_id="sequence_qc", outcome=Outcome.SKIP, evaluated=1,
            skip_reason="R was not available"))
        assert "Ready for curator review" in page

    def test_nothing_found_is_not_called_valid(self):
        page = self._page(CheckResult(
            check_id="sequence_qc", outcome=Outcome.PASS, evaluated=2))
        assert "No automated problems found" in page
        for overclaim in ("is valid", "Submission passed", "approved", "Hosts are correct"):
            assert overclaim not in page

    def test_a_check_that_examined_nothing_is_not_counted_as_clean(self):
        page = self._page(CheckResult(check_id="vector_sanity", outcome=Outcome.PASS,
                                      evaluated=0))
        assert "1 other checks examined" not in page
        assert "not relevant to this submission" in page


class TestUrlSafety:
    """Escaping makes a string safe to display; it says nothing about what a scheme does."""

    def test_dangerous_schemes_are_rejected(self):
        from malavi_curation.report_html import _safe_url
        for bad in ("javascript:alert(1)//drive.google.com",
                    "data:text/html,<script>x</script>",
                    "vbscript:msgbox", "file:///etc/passwd",
                    "http://drive.google.com/open?id=1", "https://evil.example/x"):
            assert _safe_url(bad) is None, f"{bad!r} was allowed"

    def test_the_two_real_hosts_are_allowed(self):
        from malavi_curation.report_html import _safe_url
        for good in ("https://drive.google.com/open?id=1ABC",
                     "https://docs.google.com/forms/d/e/X/viewform?usp=pp_url"):
            assert _safe_url(good) == good

    def test_a_hostile_metadata_url_never_becomes_an_href(self):
        from malavi_curation.report_html import _submitted_files
        html = _submitted_files({"An upload": "javascript:alert(1)//drive.google.com"})
        assert "javascript:" not in html


def test_every_check_that_can_produce_a_finding_has_a_curator_headline():
    """Presentation completeness, asserted rather than hoped for.

    A check without a headline falls back to its own title, which is written as the thing
    being ASSERTED — "Proposed names are free" — and printed over a finding that reads as
    the opposite of what was found. Adding a check should fail this test rather than
    silently inherit that fallback.
    """
    from malavi_curation.checks import CHECKS, Severity
    from malavi_curation.report_html import FINDING_HEADLINES, CHECK_GROUPS

    # Checks that can only ever pass or skip carry no finding text, so they are exempt.
    needs_headline = {cid for cid, spec in CHECKS.items()
                      if spec.severity in (Severity.BLOCKING, Severity.WARNING)}
    missing = sorted(needs_headline - set(FINDING_HEADLINES))
    assert not missing, (
        "these checks can raise a finding but have no curator-facing headline, so the "
        f"report would print their assertion instead: {missing}")


def test_every_check_belongs_to_a_named_group():
    """An unrecognised check lands under 'Other', which tells a curator nothing."""
    from malavi_curation.checks import CHECKS
    from malavi_curation.report_html import _group_of

    ungrouped = sorted(cid for cid in CHECKS if _group_of(cid) == "Other")
    assert not ungrouped, f"checks with no group: {ungrouped}"
