#!/usr/bin/env python3
# @title Report the differences between the last Lund release and the Lund database
# @purpose Compare the flat tables exported from MALAVI.sqlite with the last release that
#          database produced, and write one short document listing what was added,
#          changed and removed at Lund in between, table by table, in the terms the
#          database is curated in.
# @why The import is the hand-over of MalAvi from Lund to the rebuild. Staffan Bensch is
#      the one person who can say whether what we read out of his database is what he
#      put in, and he should be able to do that from a few pages rather than a
#      spreadsheet diff.
# @input data/master_exports/<release>/*.csv
# @input docs/assets/downloads/tables/*_<previous release>.csv
# @input data/master_imports/import_<release>.json
# @output data/releases/lund_changes_<release>.html (+ .pdf, .json)
# @program python
# @program weasyprint
"""Write the document that shows Staffan what changed between his release and his database.

    .venv/bin/python curation/lund_changes_report.py --release 2026-09-15 \
        --previous 2026-03-23

Two things Staffan produced are compared: the previous release's tables and the database
he sent, as our import reads it. Every difference is therefore an edit made at Lund in
between -- or a mistake in how we read the database, which he can tell apart and we
cannot. The document says only what differs; unchanged rows are not listed.

The corrections we made on top of his data are a separate, last section, so that they are
not mistaken for his changes.

The document goes into ``data/releases/``, which is gitignored: it names unpublished
studies and their contributors, and is for Staffan, not for the site.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation.config import repo_root  # noqa: E402
from malavi_curation.master_db import merge_table, normalize_text  # noqa: E402
from malavi_curation.release_notes import write_pdf  # noqa: E402
from malavi_curation.release_seed import (  # noqa: E402
    _SOURCE_FILES, read_release_csv, release_table_path,
)
from malavi_curation.release_store import (  # noqa: E402
    TABLES, read_store, store_dir,
)
from malavi_curation.store_corrections import log_path, read_log  # noqa: E402

# The tables, in the order the document presents them, with the name a reader knows.
ORDER = ("lineages", "references", "host_records", "vector_records", "morpho_species",
         "alt_names")
TITLES = {
    "lineages": "Lineages",
    "references": "Studies",
    "host_records": "Host records",
    "vector_records": "Vector records",
    "morpho_species": "Morphospecies links",
    "alt_names": "Alternative lineage names",
}
# Rows listed in any one table before the rest is summarized by a count.
ROW_CAP = 120


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", required=True,
                        help="The import's release tag; names data/master_exports/<tag>/ "
                             "and data/master_imports/import_<tag>.json.")
    parser.add_argument("--previous", required=True,
                        help="The release the database last produced, whose tables sit "
                             "in docs/assets/downloads/tables/.")
    parser.add_argument("--destination", type=Path, default=None,
                        help="Directory for the document. Defaults to data/releases/.")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Reading and comparing
# ---------------------------------------------------------------------------

def read_export(directory: Path) -> Dict[str, List[Dict[str, str]]]:
    out = {}
    for name, spec in TABLES.items():
        path = Path(directory) / spec.filename
        with open(path, newline="", encoding="utf-8") as handle:
            out[name] = [dict(row) for row in csv.DictReader(handle)]
    return out


def read_previous(downloads: Path, release: str) -> Dict[str, List[Dict[str, str]]]:
    out = {}
    for name, spec in TABLES.items():
        rows = read_release_csv(release_table_path(downloads, _SOURCE_FILES[name], release))
        # Only the store's columns; the summary's derived columns are not Staffan's data.
        out[name] = [{column: row.get(column, "") for column in spec.columns}
                     for row in rows]
    return out


def compare(previous: Dict[str, List[Dict[str, str]]],
            current: Dict[str, List[Dict[str, str]]]) -> Dict[str, Dict[str, Any]]:
    """Per table: added / changed / removed rows, by the import's own pairing.

    The previous release's rows are given throwaway ids so that :func:`merge_table` can
    pair edited rows and report which columns changed.
    """
    out = {}
    for name in ORDER:
        spec = TABLES[name]
        old = [dict(row, RECORD_ID=f"{spec.prefix}-{i + 1:06d}", _source="seed",
                    _added="previous")
               for i, row in enumerate(previous[name])]
        merged, report = merge_table(spec, old, current[name], "current")
        by_id = {row["RECORD_ID"]: row for row in old}
        for change in report["changed"]:
            change["row"] = by_id[change["record_id"]]
        removed_ids = {entry["record_id"] for entry in report["removed"]}
        report["removed_rows"] = [row for row in old if row["RECORD_ID"] in removed_ids]
        report["added_rows"] = [row for row in merged if row.get("_added") == "current"]
        out[name] = report
    return out


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape(normalize_text(value))


def sci(value: Any) -> str:
    return f"<i>{esc(value)}</i>"


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
          widths: Optional[Sequence[str]] = None, cap: int = ROW_CAP,
          raw: bool = False) -> str:
    """A fixed-layout table that never runs off the page. ``raw`` cells are HTML."""
    if not rows:
        return "<p class='none'>none</p>"
    colgroup = ""
    if widths:
        colgroup = "<colgroup>" + "".join(f"<col style='width:{w}'>" for w in widths) + \
                   "</colgroup>"
    cell = (lambda c: str(c)) if raw else esc
    body = "".join("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in row) + "</tr>"
                   for row in rows[:cap])
    more = (f"<p class='more'>… and {len(rows) - cap:,} more rows, in the JSON beside "
            f"this document.</p>" if len(rows) > cap else "")
    return (f"<table>{colgroup}<thead><tr>" +
            "".join(f"<th>{esc(h)}</th>" for h in headers) +
            "</tr></thead><tbody>" + body + "</tbody></table>" + more)


def sequence_change(was: str, now: str) -> str:
    """Describe a changed sequence by where it differs rather than printing it twice."""
    if len(was) != len(now):
        return f"length {len(was)} → {len(now)}"
    positions = [i + 1 for i, (a, b) in enumerate(zip(was, now)) if a != b]
    if not positions:
        return "no difference"
    shown = ", ".join(f"{p} ({was[p - 1] or '-'}→{now[p - 1] or '-'})"
                      for p in positions[:8])
    more = f" and {len(positions) - 8} more" if len(positions) > 8 else ""
    return f"{len(positions)} position(s) differ: {shown}{more}"


def field_change(column: str, was: str, now: str) -> str:
    if column == "SEQUENCE":
        return sequence_change(was, now)
    return f"{esc(was) or '<span class=none>blank</span>'} → " \
           f"{esc(now) or '<span class=none>blank</span>'}"


# Column names as a reader knows them; the raw name is fine for anything not listed.
FIELD_LABELS = {
    "PARASITE_GENUS": "Parasite genus", "GENUS_NAME": "Genus", "SPECIES_NAME": "Species",
    "ALT_NAME": "Alternative names", "SEQUENCE": "Sequence", "GENBANK_ACC": "GenBank",
    "SEQ_LENGTH": "Sequence length", "SITE_COORDINATES": "Coordinates",
    "COUNTRY_NAME": "Country", "COUNTRY_REGION_NAME": "Region", "SITE_NAME": "Site",
    "NUMBER_FOUND": "Number found", "NUMBER_TESTED": "Number tested",
    "HOST_STATUS": "Host status", "HOST_AGE": "Host age", "REFERENCE_NAME": "Study",
    "ORDER_NAME": "Order", "FAMILY_NAME": "Family", "TITLE": "Title",
    "JOURNAL_NAME": "Journal", "PUBLICATION_YEAR": "Year", "STUDY_TYPE": "Study type",
    "VOLUME_PAGES": "Volume, pages", "MORPHOLOGY_COMMENT": "Comment", "COMMENT": "Comment",
    "SUB_SPECIES_NAME": "Subspecies", "HOST_ENVIRONMENT": "Environment",
    "CONTINENT_NAME": "Continent", "VECTOR_SPECIES": "Vector", "VECTOR_METHOD": "Method",
}


def label(column: str) -> str:
    return FIELD_LABELS.get(column, column)


def grouped_changes(changed: Sequence[Dict[str, Any]], describe) -> List[List[str]]:
    """One table row per distinct (field, old, new) change, naming every row it hit.

    Twenty-one host records that all went from genus N/A to Haemoproteus are one edit,
    not twenty-one; listing them one per line was the part of the first draft that ran
    off the page. ``describe(row)`` names one changed row (HTML).
    """
    groups: Dict[tuple, List[str]] = defaultdict(list)
    order: List[tuple] = []
    for change in changed:
        for column, v in change["columns"].items():
            key = (column, v["was"], v["now"])
            if key not in groups:
                order.append(key)
            groups[key].append(describe(change["row"]))
    out = []
    for column, was, now in order:
        names = groups[(column, was, now)]
        # de-duplicate while keeping order (the same lineage under several studies)
        seen = list(dict.fromkeys(names))
        out.append([esc(label(column)), field_change(column, was, now),
                    f"{len(names)} row{'s' if len(names) != 1 else ''}: " +
                    ", ".join(seen[:40]) + (f" … and {len(seen) - 40} more"
                                            if len(seen) > 40 else "")])
    return out


def _count(n: int, singular: str, plural: Optional[str] = None) -> str:
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def glance(previous: str, release: str, comparison: Dict[str, Dict[str, Any]]) -> str:
    rows = []
    for name in ORDER:
        r = comparison[name]
        rows.append([TITLES[name], f"{r['n_existing']:,}", f"{r['n_exported']:,}",
                     f"{r['n_added']:,}" if r["n_added"] else "",
                     f"{r['n_changed']:,}" if r["n_changed"] else "",
                     f"{r['n_removed']:,}" if r["n_removed"] else ""])
    return ("<h2>What changed</h2>" +
            table(["Table", f"Rows in {previous}", f"Rows in {release}", "Added", "Changed",
                   "Removed"], rows, widths=["30%", "16%", "16%", "12%", "13%", "13%"]) +
            "<p>Added means rows were added in this latest release relative to the last "
            f"release online ({previous}), changed means something about the row was "
            "changed, and removed means one or more rows were removed since the last "
            "version. The details for each of these changes are listed below.</p>")


def lineage_section(r: Dict[str, Any], current: Dict[str, List[Dict[str, str]]],
                    previous: str, release: str) -> str:
    parts = ["<h2>Lineages</h2>"]
    added = r["added_rows"]
    record_count = Counter(row["LINEAGE_NAME"] for row in current["host_records"])
    vector_count = Counter(row["LINEAGE_NAME"] for row in current["vector_records"])

    # Renames: a removed lineage whose name is now an alternative name of an added one.
    alt_of = defaultdict(set)
    for row in current["alt_names"]:
        alt_of[row["ALT_NAME"]].add(row["LINEAGE_NAME"])
    added_names = {row["LINEAGE_NAME"] for row in added}
    renamed = {}
    for row in r["removed_rows"]:
        targets = alt_of.get(row["LINEAGE_NAME"], set()) & added_names
        if len(targets) == 1:
            renamed[row["LINEAGE_NAME"]] = next(iter(targets))

    # Added, by genus, as a compact list; new names are the thing to recognize.
    truly_new = [row for row in added if row["LINEAGE_NAME"] not in renamed.values()]
    parts.append(f"<h3>{_count(len(truly_new), 'lineage')} added"
                 + (f", {len(renamed)} renamed" if renamed else "") + "</h3>")
    by_genus: Dict[str, List[str]] = defaultdict(list)
    for row in truly_new:
        by_genus[row["GENUS_NAME"] or "no genus"].append(row["LINEAGE_NAME"])
    for genus, names in sorted(by_genus.items()):
        parts.append(f"<p class='list'><b>{sci(genus)} ({len(names)}):</b> " +
                     ", ".join(esc(n) for n in sorted(names)) + "</p>")
    without = sorted(row["LINEAGE_NAME"] for row in truly_new
                     if not record_count.get(row["LINEAGE_NAME"])
                     and not vector_count.get(row["LINEAGE_NAME"]))
    if without:
        parts.append(
            f"<p class='note'>{len(without)} of the added lineages have no host or vector "
            f"records, so they will appear in the Grand Lineage Summary and the alignment "
            f"but won't have host, country or study.<br>" +
            ", ".join(esc(n) for n in without) + "</p>")
    with_morpho = [row for row in truly_new if row["SPECIES_NAME"]]
    if with_morpho:
        parts.append("<p class='list'><b>Added with a morphospecies:</b> " +
                     ", ".join(f"{esc(row['LINEAGE_NAME'])} ({sci(row['SPECIES_NAME'])})"
                               for row in with_morpho) + "</p>")
    for old, new in sorted(renamed.items()):
        parts.append(f"<p class='list'><b>Renamed:</b> {esc(old)} is now {esc(new)}; "
                     f"{esc(old)} is kept as an alternative name.</p>")

    # Changed: one row per changed field.
    changed = r["changed"]
    parts.append(f"<h3>{_count(len(changed), 'lineage')} changed</h3>")
    rows = grouped_changes(changed, lambda row: esc(row["LINEAGE_NAME"]))
    parts.append(table(["Field", f"{previous} → {release}", "Lineages"], rows,
                       widths=["16%", "34%", "50%"], raw=True))

    removed = [row for row in r["removed_rows"] if row["LINEAGE_NAME"] not in renamed]
    parts.append(f"<h3>{_count(len(removed), 'lineage')} removed</h3>")
    parts.append(table(["Lineage", "Genus", "GenBank"],
                       [[row["LINEAGE_NAME"], row["GENUS_NAME"], row["GENBANK_ACC"]]
                        for row in removed], widths=["30%", "35%", "35%"]))
    return "".join(parts)


def studies_section(r: Dict[str, Any], current: Dict[str, List[Dict[str, str]]],
                    previous: str, release: str) -> str:
    parts = ["<h2>Studies</h2>"]
    cited = Counter(row["REFERENCE_NAME"] for row in current["host_records"])
    cited.update(row["REFERENCE_NAME"] for row in current["vector_records"])
    added = r["added_rows"]
    parts.append(f"<h3>{_count(len(added), 'study', 'studies')} added</h3>")
    parts.append(table(["Study", "Year", "Journal", "Records"],
                       [[row["REFERENCE_NAME"], row["PUBLICATION_YEAR"],
                         row["JOURNAL_NAME"], cited.get(row["REFERENCE_NAME"], 0)]
                        for row in added], widths=["30%", "10%", "45%", "15%"]))
    changed = r["changed"]
    if changed:
        parts.append(f"<h3>{_count(len(changed), 'study', 'studies')} changed</h3>")
        rows = grouped_changes(changed, lambda row: esc(row["REFERENCE_NAME"]))
        parts.append(table(["Field", f"{previous} → {release}", "Studies"], rows,
                           widths=["16%", "34%", "50%"], raw=True))
    removed = r["removed_rows"]
    if removed:
        parts.append(f"<h3>{_count(len(removed), 'study', 'studies')} removed</h3>")
        parts.append(table(["Study", "Year", "Journal"],
                           [[row["REFERENCE_NAME"], row["PUBLICATION_YEAR"],
                             row["JOURNAL_NAME"]] for row in removed],
                           widths=["35%", "15%", "50%"]))
    return "".join(parts)


def host_section(r: Dict[str, Any], previous: str, release: str) -> str:
    parts = ["<h2>Host records</h2>"]
    added = r["added_rows"]
    parts.append(f"<h3>{_count(len(added), 'host record')} added</h3>")
    by_study: Dict[str, Dict[str, set]] = defaultdict(lambda: defaultdict(set))
    n_by_study: Counter = Counter()
    for row in added:
        s = row["REFERENCE_NAME"] or "(no study)"
        n_by_study[s] += 1
        by_study[s]["lineages"].add(row["LINEAGE_NAME"])
        by_study[s]["hosts"].add(row["SPECIES_NAME"])
        by_study[s]["countries"].add(row["COUNTRY_NAME"])
    rows = [[s, n, len(by_study[s]["lineages"]), len(by_study[s]["hosts"]),
             ", ".join(sorted(c for c in by_study[s]["countries"] if c))]
            for s, n in n_by_study.most_common()]
    parts.append(table(["Study", "Records", "Lineages", "Host species", "Countries"], rows,
                       widths=["32%", "12%", "12%", "14%", "30%"]))

    changed = r["changed"]
    parts.append(f"<h3>{_count(len(changed), 'host record')} changed</h3>")
    rows = grouped_changes(
        changed, lambda row: f"{esc(row['LINEAGE_NAME'])} in {sci(row['SPECIES_NAME'])} "
                             f"({esc(row['REFERENCE_NAME'])})")
    parts.append(table(["Field", f"{previous} → {release}", "Records"], rows,
                       widths=["14%", "26%", "60%"], raw=True))

    removed = r["removed_rows"]
    parts.append(f"<h3>{_count(len(removed), 'host record')} removed</h3>")
    parts.append(table(["Lineage", "Host", "Country", "Site", "Study"],
                       [[row["LINEAGE_NAME"], row["SPECIES_NAME"], row["COUNTRY_NAME"],
                         row["SITE_NAME"], row["REFERENCE_NAME"]] for row in removed],
                       widths=["14%", "24%", "16%", "24%", "22%"]))
    return "".join(parts)


def simple_section(name: str, r: Dict[str, Any], columns: Sequence[str],
                   headers: Sequence[str], widths: Sequence[str],
                   previous: str, release: str, unit: str, plural: Optional[str] = None,
                   removed_note: str = "") -> str:
    parts = [f"<h2>{esc(TITLES[name])}</h2>"]
    if not (r["n_added"] or r["n_changed"] or r["n_removed"]):
        parts.append(f"<p>Unchanged: the {r['n_existing']:,} rows of {previous} are the "
                     f"{r['n_exported']:,} rows of {release}.</p>")
        return "".join(parts)
    for label, rows in (("added", r["added_rows"]), ("removed", r["removed_rows"])):
        if rows or label == "added":
            parts.append(f"<h3>{_count(len(rows), unit, plural)} {label}</h3>")
            parts.append(table(headers, [[row[c] for c in columns] for row in rows],
                               widths=widths))
            if label == "removed" and rows and removed_note:
                parts.append(f"<p class='list'>{removed_note}</p>")
    if r["changed"]:
        parts.append(f"<h3>{_count(len(r['changed']), unit, plural)} changed</h3>")
        rows = grouped_changes(
            r["changed"], lambda row: esc(" · ".join(row[k] for k in TABLES[name].key)))
        parts.append(table(["Field", f"{previous} → {release}", "Rows"], rows,
                           widths=["16%", "34%", "50%"], raw=True))
    return "".join(parts)


def _record_line(row: Dict[str, str]) -> str:
    """One host record, readably: lineage in host, country, site, study, found/tested."""
    prev = row.get("NUMBER_FOUND", "")
    if row.get("NUMBER_TESTED"):
        prev = f"{prev or '?'} / {row['NUMBER_TESTED']}"
    bits = [f"{esc(row.get('LINEAGE_NAME'))} in {sci(row.get('SPECIES_NAME'))}",
            esc(row.get("COUNTRY_NAME")), esc(row.get("SITE_NAME")),
            esc(row.get("REFERENCE_NAME")), esc(prev)]
    return ", ".join(b for b in bits if b)


def _new_rows(name: str, previous: Dict[str, List[Dict[str, str]]],
              current: Dict[str, List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """The rows of one table whose natural key was not in the previous release.

    By key, not by every cell: a record whose genus was corrected is an edit, not a new
    record, and an edit is already listed in its own section.
    """
    from malavi_curation.release_store import row_key
    spec = TABLES[name]
    before = {row_key(spec, row) for row in previous.get(name, [])}
    return [row for row in current.get(name, []) if row_key(spec, row) not in before]


def things_to_look_at(import_report: Dict[str, Any],
                      current: Dict[str, List[Dict[str, str]]],
                      previous: Dict[str, List[Dict[str, str]]]) -> str:
    """What a curator can act on, among the rows new in this release only.

    A fault the previous release already carried is not news to its author; only what
    arrived with this release is listed, each item with the rows it is about.
    """
    findings = {f["kind"]: f for f in import_report["findings"]}
    hosts = current["host_records"]
    new_hosts = _new_rows("host_records", previous, current)
    # A study is new when its title was not in the previous release; the name alone
    # cannot say, since the second Perrin study arrived under a name March already had.
    old_titles = {normalize_text(r["TITLE"]).lower() for r in previous.get("references", [])}
    new_refs = {r["REFERENCE_NAME"] for r in current["references"]
                if normalize_text(r["TITLE"]).lower() not in old_titles}
    items: List[str] = []

    def examples(rows: Sequence[Dict[str, str]], limit: int = 6) -> str:
        shown = "".join(f"<li>{_record_line(row)}</li>" for row in rows[:limit])
        more = (f"<li class='none'>… and {len(rows) - limit} more</li>"
                if len(rows) > limit else "")
        return f"<ul class='rows'>{shown}{more}</ul>"

    # One reference name, two studies -- when one of them arrived with this release.
    dup = findings.get("reference_names_not_unique")
    for name in (dup["detail"] if dup else []):
        refs = [r for r in current["references"] if r["REFERENCE_NAME"] == name]
        titles = {normalize_text(r["TITLE"]).lower() for r in refs}
        if name not in new_refs or len(titles) < 2:
            continue
        cited = [r for r in hosts if r["REFERENCE_NAME"] == name]
        items.append(
            f"<b>“{esc(name)}” is the name of {len(refs)} different studies:</b>" +
            "".join(f"<br>· {esc(r['TITLE'])} ({esc(r['JOURNAL_NAME'])}"
                    f"{', ' + esc(r['VOLUME_PAGES']) if r['VOLUME_PAGES'] else ''})"
                    for r in refs) +
            (f"<br>{len(cited)} host records cite the name, and nothing says which "
             f"study each belongs to, for example:" + examples(cited, 4) if cited else ""))

    noref = [r for r in new_hosts if not r["REFERENCE_NAME"]]
    if noref:
        items.append(f"<b>{len(noref)} new host records cite no study:</b>" +
                     examples(noref, 8))
    nocountry = [r for r in new_hosts if not r["COUNTRY_NAME"]]
    if nocountry:
        items.append(f"<b>{len(nocountry)} new host records have no country:</b>" +
                     examples(nocountry, 8))

    over = [r for r in new_hosts
            if r["NUMBER_FOUND"].isdigit() and r["NUMBER_TESTED"].isdigit()
            and int(r["NUMBER_FOUND"]) > int(r["NUMBER_TESTED"])]
    if over:
        items.append(f"<b>{len(over)} new host records report more infections than birds "
                     f"tested (found / tested):</b>" + examples(over, len(over)))

    bad = sorted({r["SITE_COORDINATES"] for r in new_hosts if r["SITE_COORDINATES"] and
                  re.search(r"[°º]\\s*([6-9]\\d|\\d{3,})(?:[.,]\\d+)?\\s*['’′´`]?\\s*(,|$)",
                            r["SITE_COORDINATES"])})
    if bad:
        parts = []
        for cell in bad:
            rows = [r for r in hosts if r["SITE_COORDINATES"] == cell]
            site = rows[0]
            parts.append(f"<br>· “{esc(cell)}” at {esc(site['SITE_NAME'])}, "
                         f"{esc(site['COUNTRY_NAME'])} ({esc(site['REFERENCE_NAME'])}, "
                         f"{len(rows)} record{'s' if len(rows) != 1 else ''}): the minutes "
                         f"part is 60 or more, so these records cannot be placed on a map.")
        items.append(f"<b>{len(bad)} new coordinate cells cannot be read:</b>" + "".join(parts))

    if not items:
        return ""
    return ("<h2>Things worth a look</h2><ul>" +
            "".join(f"<li>{item}</li>" for item in items) + "</ul>")


def our_changes(import_report: Dict[str, Any], log_rows: Sequence[Dict[str, str]],
                current: Dict[str, List[Dict[str, str]]],
                store: Dict[str, List[Dict[str, str]]],
                previous: Dict[str, List[Dict[str, str]]]) -> str:
    """Changes made here to rows that arrived with this release, shown as rows.

    A correction to a row the previous release already carried is old news and is left
    out; the section is omitted when nothing new was touched.
    """
    from malavi_curation.master_db import correction_from_log
    from malavi_curation.release_store import row_key
    from malavi_curation.store_corrections import matcher

    def is_new(table: str, row: Dict[str, str]) -> bool:
        spec = TABLES[table]
        before = previous_sigs.setdefault(
            table, {row_key(spec, r) for r in previous.get(table, [])})
        return row_key(spec, row) not in before
    previous_sigs: Dict[str, set] = {}

    reapplied = {e["correction_id"] for e in import_report["corrections_replayed"]
                 if e["status"] == "reapplied"}
    # The Perrin split was made after the import, so it is not in the replay list.
    wanted = reapplied | {"COR-000033", "COR-000034"}
    rows_out = []
    grouped: Dict[tuple, List[Dict[str, str]]] = {}
    order: List[tuple] = []
    for log in log_rows:
        if log["CORRECTION_ID"] not in wanted:
            continue
        key = (log["COLUMN"], log["OLD_VALUE"], log["NEW_VALUE"])
        if key not in grouped:
            order.append(key)
        grouped.setdefault(key, []).append(log)
    for key in order:
        logs = grouped[key]
        log = logs[0]
        hit = []
        shown = []
        for log in logs:
            correction = correction_from_log(log)
            keep = matcher(correction)
            source = store if correction.selector_kind == "record" else current
            found = [r for r in source.get(correction.table, []) if keep(r)]
            if source is current:
                found = [r for r in found if is_new(correction.table, r)]
            hit.extend(found)
            if correction.table == "host_records":
                shown += [_record_line(r) for r in found[:4]]
            elif correction.table == "vector_records":
                shown += [f"{esc(r['LINEAGE_NAME'])} in {sci(r['VECTOR_SPECIES'])}, "
                          f"{esc(r['COUNTRY_NAME'])}, {esc(r['REFERENCE_NAME'])}"
                          for r in found[:4]]
            elif correction.table == "references":
                shown += [f"the reference row: {esc(r['TITLE'])}" for r in found[:4]]
            else:
                shown += [esc(r.get("LINEAGE_NAME")) for r in found[:6]]
        n = len(hit)
        if not n:
            continue
        rows_out.append([
            esc(label(log["COLUMN"])),
            f"{esc(log['OLD_VALUE']) or '<span class=none>blank</span>'} → {esc(log['NEW_VALUE'])}",
            f"{n} row{'s' if n != 1 else ''}<br>" + "<br>".join(shown) +
            (f"<br><span class='none'>… and {n - len(shown)} more</span>" if n > len(shown) else ""),
        ])
    if not rows_out:
        return ""
    return ("<h2>Changes we made on top of your data</h2>" +
            table(["Field", "From → To", "Rows"], rows_out,
                  widths=["16%", "30%", "54%"], raw=True))


STYLE = """
body { font: 10.5pt/1.4 Georgia, 'Times New Roman', serif; color: #1a1a1a; }
h1 { font-size: 18pt; margin: 0 0 0.6em; }
h2 { font-size: 14pt; border-bottom: 1px solid #888; margin: 1.8em 0 0.5em; }
h3 { font-size: 11.5pt; margin: 1.2em 0 0.3em; }
p { margin: 0.4em 0; }
p.list { margin: 0.3em 0; }
p.note { border-left: 4px solid #b5651d; padding-left: 0.8em; margin: 0.8em 0; }
p.none, span.none { color: #777; font-style: italic; }
p.more { color: #777; font-style: italic; font-size: 9pt; }
table { border-collapse: collapse; width: 100%; table-layout: fixed; font-size: 8.5pt;
        margin: 0.3em 0 0.8em; }
th, td { border-bottom: 1px solid #ddd; padding: 2px 5px; text-align: left;
         vertical-align: top; overflow-wrap: anywhere; }
th { background: #eee; font-weight: 600; }
li { margin-bottom: 0.5em; }
ul.rows { margin: 0.2em 0 0.2em 1.2em; padding: 0; font-size: 9.5pt; }
ul.rows li { margin-bottom: 0.1em; }
@page { size: A4; margin: 1.6cm 1.5cm; }
"""


def render(release: str, previous: str, import_report: Dict[str, Any],
           comparison: Dict[str, Dict[str, Any]],
           current: Dict[str, List[Dict[str, str]]],
           log_rows: Sequence[Dict[str, str]],
           store: Dict[str, List[Dict[str, str]]],
           previous_tables: Optional[Dict[str, List[Dict[str, str]]]] = None) -> str:
    previous_tables = previous_tables or {}
    head = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>MalAvi: {esc(previous)} release to {esc(release)} database</title>
<style>{STYLE}</style></head><body>
<h1>MalAvi: {esc(previous)} release to {esc(release)} database</h1>
<p>This document lists the differences between the {esc(previous)} release and the
database sent {esc(release)}.</p>
"""
    body = [
        glance(previous, release, comparison),
        lineage_section(comparison["lineages"], current, previous, release),
        studies_section(comparison["references"], current, previous, release),
        host_section(comparison["host_records"], previous, release),
        simple_section("vector_records", comparison["vector_records"],
                       ("LINEAGE_NAME", "VECTOR_SPECIES", "COUNTRY_NAME", "REFERENCE_NAME"),
                       ("Lineage", "Vector", "Country", "Study"),
                       ("16%", "30%", "22%", "32%"), previous, release, "vector record"),
        simple_section("morpho_species", comparison["morpho_species"],
                       ("LINEAGE_NAME", "SPECIES_NAME", "REFERENCE_NAME"),
                       ("Lineage", "Morphospecies", "Study"),
                       ("20%", "40%", "40%"), previous, release, "link"),
        simple_section("alt_names", comparison["alt_names"],
                       ("LINEAGE_NAME", "ALT_NAME", "REFERENCE_NAME"),
                       ("Lineage", "Alternative name", "Study"),
                       ("25%", "30%", "45%"), previous, release, "name"),
        things_to_look_at(import_report, current, previous_tables),
        our_changes(import_report, log_rows, current, store, previous_tables),
    ]
    return head + "".join(body) + "</body></html>"


def main(argv=None) -> int:
    args = parse_args(argv)
    root = repo_root()
    export_dir = root / "data" / "master_exports" / args.release
    report_path = root / "data" / "master_imports" / f"import_{args.release}.json"
    downloads = root / "docs" / "assets" / "downloads" / "tables"
    for path in (export_dir, report_path):
        if not path.exists():
            print(f"error: {path} does not exist; run import_master_db.py --apply first",
                  file=sys.stderr)
            return 1
    with open(report_path, encoding="utf-8") as handle:
        import_report = json.load(handle)
    current = read_export(export_dir)
    previous = read_previous(downloads, args.previous)
    comparison = compare(previous, current)
    log_rows = read_log(log_path(root))
    store = read_store(store_dir(root))

    destination = args.destination or (root / "data" / "releases")
    destination.mkdir(parents=True, exist_ok=True)
    content = render(args.release, args.previous, import_report, comparison, current,
                     log_rows, store, previous)
    html_path = destination / f"lund_changes_{args.release}.html"
    html_path.write_text(content, encoding="utf-8")
    pdf_path = write_pdf(content, destination / f"lund_changes_{args.release}.pdf")
    # The machine-readable comparison: every added, changed and removed row in full.
    json_path = destination / f"lund_changes_{args.release}.json"
    full = {}
    for name, r in comparison.items():
        full[name] = {
            "n_previous": r["n_existing"], "n_current": r["n_exported"],
            "n_added": r["n_added"], "n_changed": r["n_changed"], "n_removed": r["n_removed"],
            "added": [{c: row[c] for c in TABLES[name].columns} for row in r["added_rows"]],
            "changed": [{"row": {c: ch["row"][c] for c in TABLES[name].columns},
                         "columns": ch["columns"]} for ch in r["changed"]],
            "removed": [{c: row[c] for c in TABLES[name].columns}
                        for row in r["removed_rows"]],
        }
    json_path.write_text(json.dumps(full, indent=1, ensure_ascii=False), encoding="utf-8")

    print("== malavi_rebuild :: lund_changes_report ==")
    for name in ORDER:
        r = comparison[name]
        print(f"  {TITLES[name]:28s} {args.previous} {r['n_existing']:>6,}  "
              f"{args.release} {r['n_exported']:>6,}  added {r['n_added']:>4,}  "
              f"changed {r['n_changed']:>3,}  removed {r['n_removed']:>3,}")
    print(f"\nwrote {html_path}\n      {pdf_path or '(no PDF: WeasyPrint unavailable)'}"
          f"\n      {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
