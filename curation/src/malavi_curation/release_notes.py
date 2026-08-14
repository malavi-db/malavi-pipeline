# @title Render the edition report that accompanies a MalAvi release
# @purpose Turn the structured edition comparison into the two documents a release ships
#          with: the full internal record a maintainer prints, signs and files, and the
#          public release notes that can go beside the download.
# @why Every edition of MalAvi should arrive with a readable account of how it differs
#      from the last one. Without it nobody can check afterwards what a release changed,
#      and a correction nobody saw looks exactly like a bug nobody caught.
# @input the structure returned by release_diff.compare
# @output data/releases/release_notes_<release>.html (+ .pdf)
# @output data/releases/release_notes_<release>_public.html (+ .pdf)
# @program python
# @program weasyprint
# @critical-var AUDIENCES
# @critical-var INTERNAL_ONLY_SECTIONS
"""The edition report, in two audiences.

**Why two.** The same comparison serves two readers who must not be given the same
document. The maintainer needs everything: which submissions this release published, on
whose approval, whether the approval gate was overridden, and every data fault the build
found -- faults that name studies and, through them, the people who contributed them. The
public needs to know what changed in the database. A data fault attached to a named study
is public blame attached to a contributor, and MalAvi does not do that; the rule is
enforced here, in the renderer, by ``INTERNAL_ONLY_SECTIONS``, rather than by whoever is
copying files on release day.

**Both documents are built from one structure and compute nothing.** Every number in the
HTML came out of ``release_diff.compare``. A renderer that does its own arithmetic is a
renderer that can disagree with the machine-readable record beside it, and then there are
two answers to "how many lineages did this edition add".

**Printed is the point.** The stylesheet is shared with the curator report, whose paged
rules already handle the things a scrolling page never notices: repeated table headers,
findings that must not split across a page boundary, and a light palette that survives a
printer. The footer's page label is parameterized so this document does not claim to be a
curator report on every one of its pages.

**Everything is escaped.** Reference titles, host names and site names come from
submitted workbooks. A document that renders them into markup is a document that can be
made to carry markup, and this one is opened from ``file://``.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .release_diff import TABLE_TITLES
from .report_html import esc, _stylesheet

# The two documents this module produces.
AUDIENCES = ("internal", "public")

# Sections that exist only in the internal document, named by the key `render` builds its
# section list with. This is the enforcement point for the no-public-blame rule and for
# keeping approval mechanics out of a public file.
#
# It has to be load-bearing to be worth anything. Until 2026-08-11 this tuple was
# referenced by no code at all -- the split was two hard-coded `if audience == "internal"`
# checks and a RUNBOOK sentence claiming the constant enforced it. A review found it. A
# fifth internal-only section added without its own guard would have leaked silently, and
# the one test covering the rule checked four hard-coded strings that a new section would
# not have been among.
#
# `render` now filters on this tuple, and `test_release_notes` iterates it, so adding a
# name here is the whole of what it takes to keep a section out of the public document.
INTERNAL_ONLY_SECTIONS = ("approval", "faults", "signoff")

# How many rows any single table in the document prints before it says how many more
# there were. The counts are always complete; this caps only the listing.
TABLE_ROW_LIMIT = 200

# Style rules this document needs and the curator report has no use for. Appended to the
# shared stylesheet rather than added to it, so the curator report cannot inherit page
# furniture that means nothing to it.
_EXTRA_STYLE = """
/* ---- Edition report ------------------------------------------------------- */
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums;
  white-space:nowrap; }
.delta { font-variant-numeric:tabular-nums; font-weight:700; }
.delta.up { color:var(--ok); }
.delta.down { color:var(--stop); }
.delta.flat { color:var(--ink-3); font-weight:400; }
.delta.none { color:var(--ink-3); font-weight:400; font-style:italic; }
.stamp { display:inline-block; font-size:10.5px; letter-spacing:.08em;
  text-transform:uppercase; font-weight:700; padding:3px 9px; border-radius:999px;
  background:var(--accent-wash); color:var(--accent); }
.stamp.internal { background:var(--warn-wash); color:var(--warn); }
.signoff { border:2px solid var(--rule); border-radius:var(--radius); padding:18px 20px;
  margin:18px 0; background:var(--surface); }
.signoff .rows { display:grid; grid-template-columns:repeat(2, minmax(0,1fr));
  gap:26px 32px; margin-top:16px; }
.signoff .field { border-bottom:1px solid var(--ink-3); padding-top:26px; }
.signoff .field .k { display:block; font-size:10.5px; letter-spacing:.08em;
  text-transform:uppercase; color:var(--ink-3); }
.empty { color:var(--ink-3); font-style:italic; margin:10px 0 0; }
.count-note { color:var(--ink-3); font-size:12.5px; margin:8px 0 0; }
@media print {
  .signoff, .tile, .stat-table tr { break-inside:avoid; }
  .signoff .field { padding-top:30px; }
}
"""


# ---------------------------------------------------------------------------
# Small rendering helpers
# ---------------------------------------------------------------------------

def _sci(value: Any) -> str:
    """A scientific name, italicized. Escaped first, always."""
    return f'<span class="sci">{esc(value)}</span>' if value else ""


def _mono(value: Any) -> str:
    """A lineage name, accession or other identifier, in the monospace face."""
    return f'<span class="mono">{esc(value)}</span>' if value else ""


def _delta(value: Optional[int]) -> str:
    """A change in a count, signed, colored by direction.

    ``None`` renders as "not compared" rather than as zero. A missing comparison and an
    unchanged count are different facts, and a column of numbers is exactly where the
    difference between them disappears.
    """
    if value is None:
        return '<span class="delta none">not compared</span>'
    if value == 0:
        return '<span class="delta flat">0</span>'
    direction = "up" if value > 0 else "down"
    return f'<span class="delta {direction}">{value:+d}</span>'


def _number(value: Optional[int]) -> str:
    """A count, thousands-separated, or an em dash when there is none to show."""
    return "—" if value is None else f"{value:,}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]],
           numeric: Sequence[int] = (), limit: int = TABLE_ROW_LIMIT,
           total: Optional[int] = None, complete_in: str = "") -> str:
    """One table, with its own truncation notice.

    ``rows`` holds cells that are **already rendered and escaped**; this function adds no
    escaping of its own, because several callers need markup inside a cell. Every caller
    therefore passes cells through ``esc``/``_sci``/``_mono``.

    ``total`` is the true number of rows the data hold. When it exceeds what is printed,
    the table says so and names where the complete list lives -- a truncated table that
    does not admit it is a document that quietly understates a release.
    """
    if not rows:
        return '<p class="empty">Nothing in this category.</p>'

    shown = rows[:limit]
    # Right-aligned, tabular-numeral columns are named by index rather than guessed from
    # the cell contents: a column of counts that happens to hold one em dash is still a
    # column of counts, and should not jump to the left because one row is empty.
    numeric_attribute = ' class="num"'

    head = "".join(
        f"<th{numeric_attribute if index in numeric else ''}>{esc(header)}</th>"
        for index, header in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(
            f"<td{numeric_attribute if index in numeric else ''}>{cell}</td>"
            for index, cell in enumerate(row)) + "</tr>"
        for row in shown)

    total = len(rows) if total is None else total
    note = ""
    if total > len(shown):
        note = (f'<p class="count-note">Showing the first {len(shown):,} of '
                f'{total:,}{esc(complete_in)}.</p>')
    return (f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>{note}')


def _tiles(entries: Sequence[Sequence[Any]]) -> str:
    """The headline numbers, as the row of tiles the site and the curator report use."""
    cells = "".join(
        f'<div class="tile"><span class="n">{esc(value)}</span>'
        f'<span class="k">{esc(label)}</span></div>'
        for value, label in entries)
    return f'<div class="tiles">{cells}</div>'


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------

def _header(diff: Dict[str, Any], audience: str, subtitle: str) -> str:
    """Title block: which two editions, generated when, and from what."""
    editions = diff["editions"]
    previous, current = editions["previous"]["label"], editions["current"]["label"]
    stamp = ('<span class="stamp internal">Internal record — not for publication</span>'
             if audience == "internal"
             else '<span class="stamp">Release notes</span>')

    missing = editions["previous"].get("missing_tables") or []
    warning = ""
    if missing:
        titles = ", ".join(TABLE_TITLES.get(name, name) for name in missing)
        warning = (f'<div class="banner"><b>Incomplete comparison.</b> The previous '
                   f'edition supplied no {esc(titles)} table, so those rows were not '
                   f'compared. Every count below that depends on them is reported as '
                   f'"not compared" rather than as zero.</div>')

    eyebrow = ("MalAvi edition report" if audience == "internal"
               else "MalAvi release notes")
    return f"""
<header>
  <p class="eyebrow">{esc(eyebrow)}</p>
  <h1 class="display">MalAvi {esc(current)}</h1>
  <p class="lead">{esc(subtitle)}</p>
  <p class="sub">{stamp}</p>
  <dl class="meta">
    <dt>This edition</dt><dd>{esc(current)}</dd>
    <dt>Compared with</dt><dd>{esc(previous)}</dd>
    <dt>Prepared</dt><dd>{esc(diff["generated"])} UTC</dd>
  </dl>
  {warning}
</header>
"""


def _at_a_glance(diff: Dict[str, Any]) -> str:
    """The headline counts: what grew, by how much, in every table."""
    lineages = diff["lineages"]
    references = diff["references"]
    hosts = diff["hosts"]
    host_table = diff["tables"]["host_records"]
    # Every other tile guards on `compared`; this one did not, so a missing
    # references_<date>.csv printed "0 studies added" for an edition that added thirty,
    # while the totals table beside it correctly said "not compared".
    references_compared = diff["tables"]["references"]["compared"]

    tiles = _tiles([
        (f'{lineages["added_count"]:,}', "lineages added"),
        (f'{len(references["added"]):,}' if references_compared else "—",
         "studies added"),
        (f'{host_table["added"]:,}' if host_table["compared"] else "—",
         "host records added"),
        (f'{len(hosts["new_species"]):,}' if hosts["compared"] else "—",
         "host species new to MalAvi"),
        (f'{len(hosts["new_countries"]):,}' if hosts["compared"] else "—",
         "countries new to MalAvi"),
    ])

    rows = [[esc(entry["entity"]), _number(entry["previous"]), _number(entry["current"]),
             _delta(entry["delta"])] for entry in diff["totals"]]
    table = _table(["", "Previous edition", "This edition", "Change"], rows,
                   numeric=(1, 2, 3), limit=len(rows))

    return f"""
<section>
  <h2 class="display">At a glance</h2>
  {tiles}
  {table}
</section>
"""


def _new_lineages(diff: Dict[str, Any]) -> str:
    """Every lineage this edition adds, with what MalAvi now knows about it."""
    added = diff["lineages"]["added"]
    rows = [[
        _mono(entry["lineage"]),
        _sci(entry["genus"]),
        _sci(entry["species"]) or '<span class="delta flat">not assigned</span>',
        _mono(entry["accession"]),
        f'{entry["host_records"]:,}',
        esc(", ".join(entry["countries"])),
        esc("; ".join(entry["references"])),
    ] for entry in added]

    body = _table(["Lineage", "Parasite genus", "Morphospecies", "GenBank", "Records",
                   "Countries", "Reported by"], rows, numeric=(4,),
                  complete_in=" (the complete list is in the release report JSON)")

    no_sequence = [entry["lineage"] for entry in added if not entry["has_sequence"]]
    note = ""
    if no_sequence:
        note = (f'<p class="count-note">{len(no_sequence)} of these carry no sequence and '
                f'are therefore absent from the alignment: '
                f'{esc(", ".join(no_sequence[:20]))}'
                f'{", and more" if len(no_sequence) > 20 else ""}.</p>')

    return f"""
<section>
  <h2 class="display">Lineages added</h2>
  <p>{len(added):,} lineage(s) appear in MalAvi for the first time in this edition.</p>
  {body}
  {note}
</section>
"""


def _retired_lineages(diff: Dict[str, Any]) -> str:
    """Lineages the previous edition held and this one does not."""
    removed = diff["lineages"]["removed"]
    if not removed:
        return ""
    rows = [[_mono(entry["lineage"]), _sci(entry["genus"]), _sci(entry["species"]),
             _mono(entry["accession"])] for entry in removed]
    return f"""
<section>
  <h2 class="display">Lineages no longer present</h2>
  <p>{len(removed):,} lineage(s) in the previous edition are not in this one. A lineage
     leaving MalAvi is always an editorial decision, so each of these should be one
     somebody made deliberately.</p>
  {_table(["Lineage", "Parasite genus", "Morphospecies", "GenBank"], rows)}
</section>
"""


def _lineage_fact_changes(diff: Dict[str, Any]) -> str:
    """Changes to what MalAvi records about a lineage it already held.

    Renders whenever there is either a change **or** an uncomparable name. A section that
    disappears when there is something it cannot compare is the failure mode this document
    exists to avoid: the reader would see nothing and conclude nothing changed.
    """
    changes = diff["lineages"]["fact_changes"]
    ambiguous = diff["lineages"].get("ambiguous") or []
    if not changes and not ambiguous:
        return ""

    ambiguity = ""
    if ambiguous:
        listed = ", ".join(
            f'{_mono(entry["lineage"])} ({entry["previous_rows"]} row(s) before, '
            f'{entry["current_rows"]} now)' for entry in ambiguous)
        ambiguity = (
            f'<div class="note"><b>{len(ambiguous)} lineage name(s) could not be compared '
            f'row by row</b>, because more than one row carries the name in one of the two '
            f'editions and there is no way to say which row became which: {listed}. '
            f'Their derived values are excluded from the corrections below as well. A '
            f'repeated lineage name also breaks any join that treats the name as a key, '
            f'so each of these wants a decision.</div>')

    if not changes:
        return f"""
<section>
  <h2 class="display">Changes to existing lineages</h2>
  <p>No lineage MalAvi already held records anything different in this edition.</p>
  {ambiguity}
</section>
"""

    rows = []
    for entry in changes:
        for column, change in sorted(entry["changed"].items()):
            rows.append([
                _mono(entry["lineage"]),
                esc(column),
                f'<span class="was">{esc(change["was"]) or "—"}</span>',
                esc(change["now"]) or "—",
            ])
    return f"""
<section>
  <h2 class="display">Changes to existing lineages</h2>
  <p>{len(changes):,} lineage(s) MalAvi already held now record something different.
     These are corrections to primary facts — an accession, a morphospecies assignment,
     a sequence — not recomputed summaries.</p>
  {_table(["Lineage", "Field", "Was", "Now"], rows)}
  {ambiguity}
</section>
"""


def _new_references(diff: Dict[str, Any]) -> str:
    """The studies this edition adds, and how much each of them brought."""
    if not diff["tables"]["references"]["compared"]:
        return f"""
<section>
  <h2 class="display">Studies added</h2>
  <div class="note">Not compared: {esc(diff["tables"]["references"]["note"])}.</div>
</section>
"""
    added = diff["references"]["added"]
    rows = [[
        _mono(entry["reference"]),
        esc(entry["year"]),
        f'<span class="wrapcell">{esc(entry["title"])}</span>',
        esc(entry["journal"]),
        f'{entry["records"]:,}',
    ] for entry in added]
    return f"""
<section>
  <h2 class="display">Studies added</h2>
  <p>{len(added):,} study/studies enter MalAvi with this edition. "Records" counts the
     host and vector records this edition credits to each of them.</p>
  {_table(["Reference", "Year", "Title", "Journal", "Records"], rows, numeric=(4,))}
</section>
"""


def _records_section(diff: Dict[str, Any], audience: str) -> str:
    """What happened to the records themselves, table by table.

    The per-study grouping is the view that stays readable when one submission brings a
    thousand rows, and it is the shape a release note gets written in. Row-level detail
    of *removals* is internal only: a removed record is somebody's published observation
    being taken out of the database, and the explanation belongs with the curator who
    made the decision, not in a public list.
    """
    blocks: List[str] = []
    # `references` is included so that a corrected study -- a title typo, a wrong year, a
    # study no longer cited -- is reported somewhere. It was computed and rendered nowhere
    # until 2026-08-11, so the totals row would read 0 for an edition that fixed one. Its
    # ADDITIONS are not repeated here; they have their own section above.
    for table in ("host_records", "vector_records", "morpho_species", "alt_names",
                  "references"):
        entry = diff["tables"][table]
        title = entry["title"]

        if not entry["compared"]:
            blocks.append(f'<h3>{esc(title)}</h3>'
                          f'<div class="note">Not compared: {esc(entry["note"])}.</div>')
            continue

        headline = (f'<p>{entry["added"]:,} added, {entry["removed"]:,} removed, '
                    f'{entry["modified"]:,} changed — '
                    f'{_number(entry["previous_rows"])} rows before, '
                    f'{_number(entry["current_rows"])} now.'
                    + (' The studies added are listed under <b>Studies added</b> above.'
                       if table == "references" and entry["added"] else "")
                    + '</p>')

        ambiguity = ""
        if entry["ambiguous_keys"]:
            ambiguity = (
                f'<p class="count-note">{entry["ambiguous_keys"]:,} key(s) identify more '
                f'than one row in one of the two editions. Rows under those keys are '
                f'reported as added or removed and are never paired up into an edit, '
                f'because which copy became which cannot be known.</p>')

        by_reference = ""
        if entry["by_reference"] and table != "references":
            rows = [[
                _mono(group["reference"]) or '<span class="delta flat">(none cited)</span>',
                f'{group["records"]:,}',
                f'{group["lineages"]:,}',
                f'{group["hosts"]:,}' if table == "host_records" else "—",
                f'{group["countries"]:,}',
            ] for group in entry["by_reference"]]
            by_reference = ("<h4>Added records, by study</h4>"
                            + _table(["Reference", "Records", "Lineages", "Host species",
                                      "Countries"], rows, numeric=(1, 2, 3, 4)))

        # Records that were corrected: same record, different values. This is the section
        # a data-correction pass has to appear in -- a longitude with its sign fixed, a
        # misspelled vector method -- because a correction that is not visible in the
        # edition report is a change to published data with no record that it happened.
        # It is in BOTH audiences: a user of the previous edition needs to know that a
        # value they downloaded has changed, and which one.
        corrected_detail = ""
        if entry["modified_rows"]:
            rows = []
            for record in entry["modified_rows"]:
                key = record["key"]
                subject = (key.get("LINEAGE_NAME", ""),
                           key.get("SPECIES_NAME") or key.get("VECTOR_SPECIES") or "",
                           key.get("REFERENCE_NAME", ""))
                for column, change in sorted(record["changed"].items()):
                    rows.append([
                        _mono(subject[0]),
                        _sci(subject[1]),
                        esc(column),
                        f'<span class="was">{esc(change["was"]) or "blank"}</span>',
                        esc(change["now"]) or "blank",
                        _mono(subject[2]),
                    ])
            corrected_detail = (
                "<h4>Records corrected</h4>"
                + f'<p class="count-note">{entry["modified"]:,} record(s) MalAvi already '
                  f'held now carry a different value. The record itself is the same one — '
                  f'these are corrections, not additions.</p>'
                + _table(["Lineage", "Species", "Field", "Was", "Now", "Reference"], rows,
                         total=entry["modified"],
                         complete_in=" (the complete list is in the release report JSON)"))

        removed_detail = ""
        if audience == "internal" and entry["removed_rows"]:
            rows = [[
                _mono(row.get("LINEAGE_NAME")),
                _sci(row.get("SPECIES_NAME") or row.get("VECTOR_SPECIES")),
                esc(row.get("COUNTRY_NAME")),
                esc(row.get("SITE_NAME")),
                _mono(row.get("REFERENCE_NAME")),
            ] for row in entry["removed_rows"]]
            removed_detail = ("<h4>Records removed</h4>"
                              + _table(["Lineage", "Species", "Country", "Site",
                                        "Reference"], rows, total=entry["removed"],
                                       complete_in=" (the complete list is in the "
                                                   "release report JSON)"))

        blocks.append(f'<h3>{esc(title)}</h3>{headline}{ambiguity}'
                      f'{by_reference}{corrected_detail}{removed_detail}')

    return f"""
<section>
  <h2 class="display">Records</h2>
  {"".join(blocks)}
</section>
"""


def _new_hosts(diff: Dict[str, Any]) -> str:
    """Host species and countries appearing in MalAvi for the first time."""
    hosts = diff["hosts"]
    if not hosts["compared"]:
        return ('<section><h2 class="display">Hosts and geography</h2>'
                '<div class="note">The previous edition supplied no host table, so no '
                'comparison of hosts or countries was possible.</div></section>')

    species = hosts["new_species"]
    countries = hosts["new_countries"]
    retired = hosts["retired_species"]

    species_block = (
        "<p>" + ", ".join(_sci(name) for name in species[:TABLE_ROW_LIMIT]) + "</p>"
        + (f'<p class="count-note">and {len(species) - TABLE_ROW_LIMIT:,} more.</p>'
           if len(species) > TABLE_ROW_LIMIT else "")
        if species else '<p class="empty">No host species is new to MalAvi in this '
                        'edition.</p>')

    countries_block = (
        "<p>" + esc(", ".join(countries)) + "</p>" if countries
        else '<p class="empty">No country is new to MalAvi in this edition.</p>')

    retired_block = ""
    if retired:
        retired_block = (
            f'<h3>Host species no longer recorded</h3><p>'
            + ", ".join(_sci(name) for name in retired[:TABLE_ROW_LIMIT]) + "</p>")

    return f"""
<section>
  <h2 class="display">Hosts and geography</h2>
  <h3>Host species new to MalAvi ({len(species):,})</h3>
  {species_block}
  <h3>Countries new to MalAvi ({len(countries):,})</h3>
  {countries_block}
  {retired_block}
</section>
"""


def _corrections(diff: Dict[str, Any]) -> str:
    """The derived columns of the Grand Lineage Summary that this build corrected.

    Explained rather than merely listed, because the first reaction to "277 lineages
    changed region" is that the build is broken. It is not: the summary is regenerated
    from the records every time, and where the published summary had gone stale relative
    to its own records, rebuilding corrects it.
    """
    summary = diff["summary_columns"]
    if not summary["by_column"]:
        return f"""
<section>
  <h2 class="display">Corrections to the Grand Lineage Summary</h2>
  <p>None. All {summary["lineages_compared"]:,} lineages present in both editions carry
     the same derived tallies, Passeriformes flag and region flags.</p>
</section>
"""

    rows = []
    for column, entry in summary["by_column"].items():
        example = entry["examples"][0]
        rows.append([
            esc(column),
            f'{entry["changed"]:,}',
            (f'{_mono(example["lineage"])}: '
             f'<span class="was">{esc(example["was"]) or "blank"}</span> → '
             f'{esc(example["now"]) or "blank"}'),
        ])

    return f"""
<section>
  <h2 class="display">Corrections to the Grand Lineage Summary</h2>
  <p>The summary's tallies, its Passeriformes flag and its twelve region flags are
     recomputed from the host and vector records every time a release is built, so this
     section is where the records and the previously published summary disagreed.
     {summary["changed_lineages"]:,} of the {summary["lineages_compared"]:,} lineages
     present in both editions carry at least one corrected value.</p>
  <p>These are corrections, not new data: the records supporting them were already in
     MalAvi. A large number here means the published summary had drifted from its own
     records, which is what regenerating it is for.</p>
  {_table(["Column", "Lineages corrected", "Example"], rows, numeric=(1,),
          limit=len(rows))}
</section>
"""


def _approval(diff: Dict[str, Any], approval: Optional[Dict[str, Any]],
              build: Optional[Dict[str, Any]]) -> str:
    """Internal only: what this release was allowed to carry, and on whose approval."""
    if not approval:
        # Rendered as a stated absence rather than dropped. An empty section is filtered
        # out by render(), so returning "" here silently removed the whole "where these
        # records came from" heading from any report not produced by a build -- the exact
        # vanishing-section failure this document exists to avoid.
        return """
<section>
  <h2 class="display">Where these records came from</h2>
  <div class="note">Not recorded. This report was produced without building a release, so
    there is no approval record to show. The release build writes one.</div>
</section>
"""
    published = approval.get("submissions_published") or []
    violations = approval.get("violations") or []
    overridden = approval.get("gate_overridden")

    banner = ""
    if overridden:
        banner = ('<div class="banner"><b>The approval gate was overridden.</b> This '
                  'release was built although records in it could not be shown to have '
                  'been approved. Nothing was marked released.</div>')

    published_block = (
        "<ul>" + "".join(f"<li>{_mono(name)}</li>" for name in published) + "</ul>"
        if published else '<p class="empty">This release publishes no submission: every '
                          'record in it was already in the store.</p>')

    violation_block = ""
    if violations:
        violation_block = ('<h3>Provenance problems</h3><ul>'
                           + "".join(f"<li>{esc(line)}</li>" for line in violations)
                           + "</ul>")

    archive = (build or {}).get("archive", "")
    return f"""
<section>
  <h2 class="display">Where these records came from</h2>
  {banner}
  <dl class="meta">
    <dt>Rows from the seeded release</dt><dd>{_number(approval.get("seed_rows"))}</dd>
    <dt>Submissions published</dt><dd>{len(published)}</dd>
    <dt>Archive</dt><dd class="mono">{esc(archive) or "—"}</dd>
  </dl>
  <h3>Submissions this release publishes</h3>
  {published_block}
  {violation_block}
</section>
"""


def _faults(warnings: Optional[Sequence[str]]) -> str:
    """Internal only: everything the build flagged as wrong in the data.

    These name studies, and through them the people who contributed the records. They
    belong in the maintainer's record and in nothing that gets published — which is why
    this section exists only in the internal document and why the public rendering is
    tested for the absence of its heading.
    """
    if warnings is None:
        # No build ran, so nothing looked. Saying "the build flagged nothing" here is the
        # same None-vs-zero conflation that _delta and _totals are careful to avoid, and
        # it would be printed on a document somebody signs and files.
        return """
<section>
  <h2 class="display">Faults to look at</h2>
  <div class="note">Not checked. The data faults are found by the release build, and this
    report was produced without one — so nothing here should be read as a clean bill of
    health. Build the release to get them.</div>
</section>
"""
    if not warnings:
        return """
<section>
  <h2 class="display">Faults to look at</h2>
  <p>The build flagged nothing.</p>
</section>
"""
    items = "".join(f"<li>{esc(line)}</li>" for line in warnings)
    return f"""
<section>
  <h2 class="display">Faults to look at</h2>
  <div class="note">These name studies, and through them the people who contributed the
    records. They stay in this document and in the release report on disk. They are not
    in the public release notes and must not be put on the site.</div>
  <ul class="finds">{items}</ul>
</section>
"""


def _signoff(diff: Dict[str, Any]) -> str:
    """Internal only: the lines somebody signs before the edition ships."""
    current = diff["editions"]["current"]["label"]
    return f"""
<section>
  <h2 class="display">Sign-off</h2>
  <p>A rebuild is also a correction, so this document is the record that somebody read
     the changes before edition {esc(current)} was published.</p>
  <div class="signoff">
    <div class="rows">
      <div class="field"><span class="k">Reviewed by</span></div>
      <div class="field"><span class="k">Date</span></div>
      <div class="field"><span class="k">Approved for release by</span></div>
      <div class="field"><span class="k">Date</span></div>
    </div>
  </div>
</section>
"""


def _footer(diff: Dict[str, Any], audience: str, report_json: str) -> str:
    """How the document was produced, and where the complete data live."""
    sources = diff["editions"]["previous"].get("sources") or {}
    previous_source = sources.get("grand_lineage_summary") or sources.get("lineages", "")
    if audience == "public":
        return f"""
<footer>
  <p>Prepared automatically when edition {esc(diff["editions"]["current"]["label"])} was
     built, by comparing it with edition
     {esc(diff["editions"]["previous"]["label"])} table by table. Corrections and
     additions to MalAvi are welcome through the submission form.</p>
</footer>
"""
    return f"""
<footer>
  <p>Produced by <span class="mono">curation/build_release.py</span> from the record
     store, by comparing this edition with the published tables of edition
     {esc(diff["editions"]["previous"]["label"])}.</p>
  <dl class="meta">
    <dt>Previous edition read from</dt><dd class="mono">{esc(previous_source)}</dd>
    <dt>Machine-readable diff</dt><dd class="mono">{esc(report_json) or "—"}</dd>
    <dt>Row listings capped at</dt><dd>{diff["example_limit"]:,} rows per category</dd>
  </dl>
</footer>
"""


# ---------------------------------------------------------------------------
# The documents
# ---------------------------------------------------------------------------

def render(diff: Dict[str, Any], audience: str = "internal", *,
           approval: Optional[Dict[str, Any]] = None,
           build: Optional[Dict[str, Any]] = None,
           warnings: Optional[Sequence[str]] = None,
           report_json: str = "") -> str:
    """The complete HTML document for one edition, for one audience.

    ``internal`` carries everything: the approval record, the faults the build found, the
    detail of removed records, and a sign-off block. ``public`` carries what changed in
    the database and nothing about who was asked or what was wrong.
    """
    if audience not in AUDIENCES:
        raise ValueError(f"unknown audience {audience!r}; expected one of {AUDIENCES}")

    previous = diff["editions"]["previous"]["label"]
    current = diff["editions"]["current"]["label"]
    subtitle = f"What changed since edition {previous}"

    # Named sections, in reading order. The names are what INTERNAL_ONLY_SECTIONS filters
    # on, so the audience rule is applied in one place to a list rather than restated as
    # an `if` beside each section that happens to need one.
    sections = [
        ("header", _header(diff, audience, subtitle)),
        ("at_a_glance", _at_a_glance(diff)),
        ("new_lineages", _new_lineages(diff)),
        ("retired_lineages", _retired_lineages(diff)),
        ("lineage_fact_changes", _lineage_fact_changes(diff)),
        ("new_references", _new_references(diff)),
        ("records", _records_section(diff, audience)),
        ("new_hosts", _new_hosts(diff)),
        ("corrections", _corrections(diff)),
        ("approval", _approval(diff, approval, build)),
        ("faults", _faults(warnings)),
        ("signoff", _signoff(diff)),
        ("footer", _footer(diff, audience, report_json)),
    ]
    if audience == "public":
        sections = [(name, body) for name, body in sections
                    if name not in INTERNAL_ONLY_SECTIONS]

    label = ("MalAvi edition report" if audience == "internal"
             else f"MalAvi {current} release notes")
    title = (f"MalAvi {current} — edition report" if audience == "internal"
             else f"MalAvi {current} — release notes")

    # A restrictive policy even though this is a local file: reference titles and site
    # names in it came from submitted workbooks, and defense in depth costs one line.
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; img-src data:;">
<title>{esc(title)}</title>
<style>{_stylesheet(label)}{_EXTRA_STYLE}</style>
</head>
<body><div class="wrap">
{"".join(body for _name, body in sections if body)}
</div></body>
</html>
"""


def _write_atomically(content: bytes, destination: Path) -> Path:
    """Write bytes to ``destination`` atomically, so no reader sees a half-written file."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(destination.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(content)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination


def write_html(content: str, destination: Path) -> Path:
    """Write the HTML document."""
    return _write_atomically(content.encode("utf-8"), destination)


def write_pdf(content: str, destination: Path) -> Optional[Path]:
    """Render the document to PDF, or return ``None`` when WeasyPrint is not installed.

    Optional for the same reason the curator report's PDF is optional: WeasyPrint pulls in
    system libraries that will not be present everywhere this runs, and a missing renderer
    must degrade visibly rather than cost the maintainer their release record. The caller
    says so when this returns ``None``.
    """
    try:
        from weasyprint import HTML          # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    except Exception as error:               # noqa: BLE001 - see below
        # A broken installation is not a reason to lose a release record either.
        print(f"warning: the PDF renderer could not be loaded ({error}); "
              f"writing HTML only", file=sys.stderr)
        return None

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(destination.parent), suffix=".tmp")
    os.close(handle)
    try:
        HTML(string=content, base_url=str(destination.parent)).write_pdf(temporary)
        os.replace(temporary, destination)
    except Exception as error:               # noqa: BLE001 - deliberate, see below
        # The PDF is the convenient copy; the HTML beside it holds the same content. By
        # the time this runs during a build the ZIP is already on disk and the ledger has
        # recorded submissions as released, so raising here would abort a run that has
        # already published -- and the re-run would then report that the release published
        # no submission at all, because they are marked released now. A failed rendering
        # degrades to "no PDF", exactly as a missing WeasyPrint does.
        Path(temporary).unlink(missing_ok=True)
        print(f"warning: could not render {destination.name} ({error}); "
              f"the HTML was still written", file=sys.stderr)
        return None
    except BaseException:
        # KeyboardInterrupt and SystemExit still propagate, without leaving a temp file.
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination


def write_documents(diff: Dict[str, Any], destination: Path, release: str, *,
                    approval: Optional[Dict[str, Any]] = None,
                    build: Optional[Dict[str, Any]] = None,
                    warnings: Optional[Sequence[str]] = None,
                    report_json: str = "",
                    audiences: Iterable[str] = AUDIENCES) -> Dict[str, Any]:
    """Write both documents, in both formats, and report what was written.

    The internal document is written to ``release_notes_<release>.html``/``.pdf`` and the
    public one gains a ``_public`` suffix. Both belong in ``data/releases/``, which is
    gitignored: the internal one names studies and submissions, and neither should reach
    a tracked directory by accident.
    """
    destination = Path(destination)
    written: Dict[str, Any] = {"pdf_unavailable": False}
    for audience in audiences:
        content = render(diff, audience, approval=approval, build=build,
                         warnings=warnings, report_json=report_json)
        suffix = "" if audience == "internal" else "_public"
        html_path = write_html(content, destination / f"release_notes_{release}{suffix}.html")
        pdf_path = write_pdf(content, destination / f"release_notes_{release}{suffix}.pdf")
        written[audience] = {"html": str(html_path),
                             "pdf": str(pdf_path) if pdf_path else None}
        if pdf_path is None:
            written["pdf_unavailable"] = True
    return written
