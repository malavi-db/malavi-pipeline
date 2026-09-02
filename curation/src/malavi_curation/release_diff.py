# @title Compare two editions of MalAvi, table by table
# @purpose Load the previous release's published tables, set them beside the edition about
#          to ship, and produce the complete structured account of what changed: lineages
#          gained and retired, studies added, records added and removed, host species and
#          countries new to the database, and every derived value the rebuild corrected.
# @why A release that ships without a record of how it differs from the last one cannot be
#      checked afterwards, and a correction nobody saw is indistinguishable from a bug
#      nobody caught. This is the data behind the printed edition report.
# @input docs/assets/downloads/tables/*_<previous release>.csv
# @input data/records/*.csv (through release_store)
# @output an in-memory structure; release_notes renders it, build_release stores it
# @program python
# @critical-var PREVIOUS_TABLE_FILES
# @critical-var EXAMPLE_LIMIT
"""What changed between two editions of MalAvi.

``release_build.diff_against_release`` answers one narrow question -- which *derived*
columns of the Grand Lineage Summary the rebuild changed -- and it answers it well. It is
not, however, an account of an edition. A curator signing off a release, and anyone
reading the printed record of it in five years, asks a larger set of questions:

* how much bigger is the database, in each of its tables;
* which lineages are new, and which are gone;
* which studies entered the database, and how much each of them brought;
* which host species and which countries appear in MalAvi for the first time;
* which existing records changed, and in which fields;
* and which derived values the rebuild corrected.

This module answers all of them, from the two editions themselves. It reads nothing about
how the release was produced and makes no judgments about it: the output is a description
of a difference, and every interpretation of that difference belongs to the renderer or to
the person reading it.

**The previous edition is the published one.** MalAvi's releases ship as CSV under
``docs/assets/downloads/tables/`` because the public site serves them as downloads, and
those files carry exactly the columns the store carries (``release_index`` already relies
on this). So the comparison is against what the world actually received, not against an
internal snapshot of what we believe we sent.

**Rows are matched on their natural keys, and matched as multisets.** The store gives each
row a ``RECORD_ID``, but a release strips it -- it is ours, not Staffan's -- so a published
edition has no row identity beyond its own values, and identity has to be reconstructed.
The natural keys are the ones ``release_store.TABLES`` already declares. They are not
unique: 302 of the 18,493 host records in the 2026-03-23 release are byte-identical to
another row. Counting keys as a multiset rather than a set means those 302 rows are
compared as the six-or-so genuine duplicates they are, instead of collapsing into one and
reporting 301 spurious deletions. Where a key does occur more than once, this module
declines to guess which copy became which and says so (``ambiguous_keys``) rather than
inventing an edit.
"""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .release_build import GRAND_LINEAGE_SUMMARY_COLUMNS, derive_summary
from .release_store import TABLES

# Which published CSV holds which store table. The basenames are the site's download
# names and are not ours to restyle; the mapping is here so that a rename on the site is
# one edit rather than a hunt.
#
# `lineages` is deliberately absent: a published edition has no lineages table. Its
# columns live in the Grand Lineage Summary, which carries them alongside the derived
# ones, so the lineage facts are recovered from there (see `_lineages_from_summary`).
PREVIOUS_TABLE_FILES: Dict[str, str] = {
    "host_records": "hosts_and_sites",
    "vector_records": "vector_data",
    "references": "references",
    "morpho_species": "morpho_species",
    "alt_names": "lineage_names",
}

# The published name of the Grand Lineage Summary itself.
SUMMARY_FILE = "grand_lineage_summary"

# How many rows of any per-row listing are carried in the structure. The counts beside
# them are always complete -- this caps the *examples*, and every consumer is told when it
# has been applied. It is generous because the document this feeds is an archival record
# that somebody prints, not a screenful.
EXAMPLE_LIMIT = 300

# The tables whose row-level differences are reported. `alt_names` is included because a
# lineage acquiring the name a paper published it under is a real editorial event, and
# `morpho_species` because linking a lineage to a described species is one of the most
# consequential things a MalAvi edition can do.
COMPARED_TABLES: Tuple[str, ...] = (
    "host_records", "vector_records", "morpho_species", "alt_names", "references",
)

# The lineage facts a release publishes. Everything else in the summary is derived and is
# compared separately, by column, as a correction rather than a change of fact.
LINEAGE_FACT_COLUMNS: Tuple[str, ...] = (
    "GENBANK_ACC", "SEQ_LENGTH", "GENUS_NAME", "SPECIES_NAME", "SEQUENCE",
)

# Human names for the tables, for anything that prints them. Kept beside the code that
# knows what they contain rather than in the renderer, so a table added here cannot ship
# with a machine name in a document a curator reads.
TABLE_TITLES: Dict[str, str] = {
    "lineages": "Lineages",
    "host_records": "Host records",
    "vector_records": "Vector records",
    "references": "References (studies)",
    "morpho_species": "Morphospecies assignments",
    "alt_names": "Alternative lineage names",
}


def _text(value: Any) -> str:
    """A cell as a stripped string; ``None`` and missing are both the empty string."""
    return (value or "").strip() if isinstance(value, str) else ("" if value is None
                                                                 else str(value).strip())


def _read_csv(path: Path) -> List[Dict[str, str]]:
    """Read a published CSV. ``utf-8-sig`` because Excel writes a BOM."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


# ---------------------------------------------------------------------------
# An edition
# ---------------------------------------------------------------------------

@dataclass
class Edition:
    """One edition of MalAvi: its tables, its summary, and where they came from.

    ``missing`` names the tables that could not be loaded. It is carried rather than
    raised because a comparison against a partial edition is still worth having -- but it
    must be *visible*, so that "no vector records changed" can never be confused with "the
    vector table was not there to compare".
    """

    label: str
    tables: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    summary: List[Dict[str, str]] = field(default_factory=list)
    sources: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)

    def rows(self, table: str) -> List[Dict[str, str]]:
        return self.tables.get(table, [])


def release_label_from_path(path: Path) -> Optional[str]:
    """The release tag stamped on a published table's filename, if it carries one."""
    match = re.search(r"_(\d{4}-\d{2}-\d{2})\.csv$", Path(path).name)
    return match.group(1) if match else None


def _lineages_from_summary(summary: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    """Recover the lineages table from a Grand Lineage Summary.

    The summary's first five columns and its last are primary facts copied out of the
    lineages table (``release_build.derive_summary`` puts them there), so projecting the
    summary onto the store's lineage columns reconstructs it exactly.
    """
    columns = TABLES["lineages"].columns
    return [{column: _text(row.get(column)) for column in columns} for row in summary]


def load_previous_edition(summary_csv: Path) -> Edition:
    """Load the published edition that ``summary_csv`` belongs to.

    The sibling tables are found beside it, by the release tag in its own filename, so a
    caller points at one file and gets the whole edition. A sibling that is not there is
    recorded in ``missing`` and the comparison simply reports that table as uncompared.
    """
    summary_csv = Path(summary_csv)
    directory = summary_csv.parent
    label = release_label_from_path(summary_csv) or summary_csv.stem

    summary = _read_csv(summary_csv)
    edition = Edition(label=label,
                      summary=summary,
                      tables={"lineages": _lineages_from_summary(summary)},
                      sources={"lineages": str(summary_csv),
                               "grand_lineage_summary": str(summary_csv)})

    for table, stem in PREVIOUS_TABLE_FILES.items():
        candidate = directory / f"{stem}_{label}.csv"
        if candidate.is_file():
            edition.tables[table] = _read_csv(candidate)
            edition.sources[table] = str(candidate)
        else:
            edition.missing.append(table)
    return edition


def current_edition(store: Dict[str, List[Dict[str, Any]]], label: str,
                    region_map: Optional[Dict[str, str]] = None,
                    summary: Optional[Sequence[Dict[str, str]]] = None) -> Edition:
    """The edition about to ship, taken from the record store.

    ``summary`` may be passed in when the caller has already derived it -- ``build_release``
    has -- because deriving it a second time is a wasted pass over 18,000 records.
    """
    derived = list(summary) if summary is not None else derive_summary(store, region_map)
    return Edition(
        label=label,
        # Projected onto the release's columns, exactly as the release itself is, so the
        # comparison never sees a provenance column the published edition cannot have.
        tables={name: [{column: _text(row.get(column)) for column in spec.columns}
                       for row in store.get(name, [])]
                for name, spec in TABLES.items()},
        summary=list(derived),
        sources={name: "(record store)" for name in TABLES},
    )


# ---------------------------------------------------------------------------
# Matching rows across editions
# ---------------------------------------------------------------------------

def _key_of(row: Dict[str, str], key_columns: Sequence[str]) -> Tuple[str, ...]:
    return tuple(_text(row.get(column)) for column in key_columns)


def _row_tuple(row: Dict[str, str], columns: Sequence[str]) -> Tuple[str, ...]:
    return tuple(_text(row.get(column)) for column in columns)


@dataclass
class TableDiff:
    """The difference between one table's rows in two editions."""

    table: str
    previous_rows: int
    current_rows: int
    added: List[Dict[str, str]] = field(default_factory=list)
    removed: List[Dict[str, str]] = field(default_factory=list)
    modified: List[Dict[str, Any]] = field(default_factory=list)
    ambiguous_keys: int = 0
    compared: bool = True
    note: str = ""


def diff_table(table: str, previous: Sequence[Dict[str, str]],
               current: Sequence[Dict[str, str]]) -> TableDiff:
    """Compare one table's rows across two editions.

    Three outcomes per row, and the boundaries between them are the whole point:

    * **added** -- a key the previous edition did not hold, or held fewer copies of;
    * **removed** -- the reverse;
    * **modified** -- the same key on both sides, with a different value in some other
      column. This is the interesting one, because it is a *correction to an existing
      record* rather than a growth of the database, and the two must never be added
      together into one number.

    Modification is only reported where the key identifies exactly one row on each side.
    Where it does not, the rows are counted in ``ambiguous_keys`` and left alone: pairing
    one of three identical-keyed rows with one of three others would manufacture an edit
    out of an arbitrary choice.
    """
    spec = TABLES[table]
    columns = list(spec.columns)
    key_columns = list(spec.key)

    previous_by_key: Dict[Tuple[str, ...], List[Dict[str, str]]] = defaultdict(list)
    current_by_key: Dict[Tuple[str, ...], List[Dict[str, str]]] = defaultdict(list)
    for row in previous:
        previous_by_key[_key_of(row, key_columns)].append(row)
    for row in current:
        current_by_key[_key_of(row, key_columns)].append(row)

    diff = TableDiff(table=table, previous_rows=len(previous), current_rows=len(current))

    for key, rows in current_by_key.items():
        was = previous_by_key.get(key, [])
        if not was:
            diff.added.extend(rows)
            continue
        # Same key on both sides. Rows that are byte-identical to a row on the other side
        # are unchanged and drop out; what remains is either an edit or a change in how
        # many copies of an identical record the edition holds.
        previous_shapes = Counter(_row_tuple(row, columns) for row in was)
        current_shapes = Counter(_row_tuple(row, columns) for row in rows)
        surplus_current = current_shapes - previous_shapes
        surplus_previous = previous_shapes - current_shapes
        if not surplus_current and not surplus_previous:
            continue
        if len(was) == 1 and len(rows) == 1:
            changed = {column: {"was": _text(was[0].get(column)),
                                "now": _text(rows[0].get(column))}
                       for column in columns
                       if _text(was[0].get(column)) != _text(rows[0].get(column))}
            if changed:
                diff.modified.append({"key": dict(zip(key_columns, key)),
                                      "changed": changed, "row": rows[0]})
            continue
        # More than one row under this key on at least one side: report the surplus as
        # added/removed rows and refuse to pair anything up.
        diff.ambiguous_keys += 1
        for shape, count in surplus_current.items():
            row = dict(zip(columns, shape))
            diff.added.extend([row] * count)
        for shape, count in surplus_previous.items():
            row = dict(zip(columns, shape))
            diff.removed.extend([row] * count)

    for key, rows in previous_by_key.items():
        if key not in current_by_key:
            diff.removed.extend(rows)

    return diff


# ---------------------------------------------------------------------------
# Deriving the readable account
# ---------------------------------------------------------------------------

def _host_binomial(row: Dict[str, str]) -> str:
    """``Genus species`` for a host record, from the release's two columns.

    ``SPECIES_NAME`` in MalAvi's host table already holds the binomial in the great
    majority of rows, so the genus is prepended only when it is not already there. This
    is presentation, not identity: nothing is matched on it.
    """
    genus, species = _text(row.get("GENUS_NAME")), _text(row.get("SPECIES_NAME"))
    if not species:
        return genus
    if genus and not species.lower().startswith(genus.lower()):
        return f"{genus} {species}"
    return species


def _distinct(rows: Iterable[Dict[str, str]], column: str) -> set:
    return {_text(row.get(column)) for row in rows if _text(row.get(column))}


def _totals(previous: Edition, current: Edition) -> List[Dict[str, Any]]:
    """The headline counts, previous beside current, in the order a reader wants them.

    Tables the previous edition did not supply report ``None`` for the previous count and
    for the delta rather than zero, because "we could not compare" is not "nothing
    changed" and a table of numbers is exactly where that distinction gets lost.
    """
    rows: List[Dict[str, Any]] = []
    for table in ("lineages", "host_records", "vector_records", "references",
                  "morpho_species", "alt_names"):
        was = None if table in previous.missing else len(previous.rows(table))
        now = len(current.rows(table))
        rows.append({"entity": TABLE_TITLES[table], "table": table,
                     "previous": was, "current": now,
                     "delta": None if was is None else now - was})

    # Two counts nobody stores but everybody asks for.
    for entity, table, column, transform in (
            ("Host species", "host_records", None, _host_binomial),
            ("Countries", "host_records", "COUNTRY_NAME", None)):
        if table in previous.missing:
            was = None
        else:
            was = len({transform(r) for r in previous.rows(table) if transform(r)}
                      if transform else _distinct(previous.rows(table), column))
        now = len({transform(r) for r in current.rows(table) if transform(r)}
                  if transform else _distinct(current.rows(table), column))
        rows.append({"entity": entity, "table": table, "previous": was, "current": now,
                     "delta": None if was is None else now - was})
    return rows


def _lineage_context(current: Edition) -> Dict[str, Dict[str, Any]]:
    """For every lineage in the new edition, what its records say about it."""
    context: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"hosts": set(), "countries": set(), "references": set(),
                 "host_records": 0, "vector_records": 0})
    for row in current.rows("host_records"):
        entry = context[_text(row.get("LINEAGE_NAME"))]
        entry["host_records"] += 1
        if _host_binomial(row):
            entry["hosts"].add(_host_binomial(row))
        if _text(row.get("COUNTRY_NAME")):
            entry["countries"].add(_text(row.get("COUNTRY_NAME")))
        if _text(row.get("REFERENCE_NAME")):
            entry["references"].add(_text(row.get("REFERENCE_NAME")))
    for row in current.rows("vector_records"):
        entry = context[_text(row.get("LINEAGE_NAME"))]
        entry["vector_records"] += 1
        if _text(row.get("COUNTRY_NAME")):
            entry["countries"].add(_text(row.get("COUNTRY_NAME")))
        if _text(row.get("REFERENCE_NAME")):
            entry["references"].add(_text(row.get("REFERENCE_NAME")))
    return context


def _describe_lineage(row: Dict[str, str], context: Dict[str, Dict[str, Any]]
                      ) -> Dict[str, Any]:
    """One new (or retired) lineage, as a reader needs to see it."""
    name = _text(row.get("LINEAGE_NAME"))
    entry = context.get(name, {})
    return {
        "lineage": name,
        "genus": _text(row.get("GENUS_NAME")),
        "species": _text(row.get("SPECIES_NAME")),
        "accession": _text(row.get("GENBANK_ACC")),
        "seq_length": _text(row.get("SEQ_LENGTH")),
        "has_sequence": bool(_text(row.get("SEQUENCE"))),
        "host_records": entry.get("host_records", 0),
        "hosts": sorted(entry.get("hosts", ())),
        "countries": sorted(entry.get("countries", ())),
        "references": sorted(entry.get("references", ())),
    }


def _index_lineages(rows: Sequence[Dict[str, str]]
                    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, int]]:
    """Index lineage rows by name, and report the names that are not unique.

    Returns ``(by_name, duplicates)``, where ``duplicates`` maps a repeated name to how
    many rows carry it. The first row wins in ``by_name`` so that the index is stable
    rather than dependent on file order, but no caller should compare a duplicated name
    on the strength of it -- ``compare`` excludes them and says so.
    """
    by_name: Dict[str, Dict[str, str]] = {}
    counts: Counter = Counter()
    for row in rows:
        name = _text(row.get("LINEAGE_NAME"))
        counts[name] += 1
        by_name.setdefault(name, row)
    return by_name, {name: count for name, count in counts.items() if count > 1}


def _summary_column_diff(previous: Edition, current: Edition,
                         ambiguous: Sequence[str] = ()) -> Dict[str, Any]:
    """Every derived value of the Grand Lineage Summary the rebuild changed.

    Restricted to lineages present in both editions, because a lineage that is new has no
    previous value to have corrected and would otherwise swamp the count of corrections
    with what is simply an addition.
    """
    derived_columns = [column for column in GRAND_LINEAGE_SUMMARY_COLUMNS
                       if column not in ("LINEAGE_NAME", *LINEAGE_FACT_COLUMNS)]
    was_by_name, _ = _index_lineages(previous.summary)
    now_by_name, _ = _index_lineages(current.summary)

    # Excluded for the same reason the primary facts are: a name on two summary rows has
    # two sets of derived values, and picking one of them makes a correction up.
    comparable = (set(was_by_name) & set(now_by_name)) - set(ambiguous)

    changes: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for name in sorted(comparable):
        for column in derived_columns:
            was = _text(was_by_name[name].get(column))
            now = _text(now_by_name[name].get(column))
            if was != now:
                changes[column].append({"lineage": name, "was": was, "now": now})

    changed_lineages = {change["lineage"] for entries in changes.values()
                        for change in entries}
    return {
        "lineages_compared": len(comparable),
        "lineages_not_compared": sorted(set(ambiguous)
                                        & set(was_by_name) & set(now_by_name)),
        "changed_lineages": len(changed_lineages),
        "by_column": {column: {"changed": len(entries),
                               "examples": entries[:EXAMPLE_LIMIT],
                               "truncated": len(entries) > EXAMPLE_LIMIT}
                      for column, entries in sorted(changes.items(),
                                                    key=lambda kv: -len(kv[1]))},
    }


def _by_reference(added_rows: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    """New records grouped by the study that reported them.

    This is the shape a release note is actually written in -- "Fecchio et al 2023b
    contributed 143 host records covering 12 lineages in 3 countries" -- and it is the
    only view of an addition that stays readable when a single submission brings a
    thousand rows.
    """
    grouped: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"records": 0, "lineages": set(), "countries": set(), "hosts": set()})
    for row in added_rows:
        entry = grouped[_text(row.get("REFERENCE_NAME"))]
        entry["records"] += 1
        if _text(row.get("LINEAGE_NAME")):
            entry["lineages"].add(_text(row.get("LINEAGE_NAME")))
        if _text(row.get("COUNTRY_NAME")):
            entry["countries"].add(_text(row.get("COUNTRY_NAME")))
        if _host_binomial(row):
            entry["hosts"].add(_host_binomial(row))
    return sorted(
        ({"reference": name, "records": entry["records"],
          "lineages": len(entry["lineages"]), "countries": len(entry["countries"]),
          "hosts": len(entry["hosts"])}
         for name, entry in grouped.items()),
        key=lambda item: (-item["records"], item["reference"]))


def compare(previous: Edition, current: Edition,
            example_limit: int = EXAMPLE_LIMIT) -> Dict[str, Any]:
    """The complete account of the difference between two editions.

    The returned structure is JSON-serializable and is the single source both renderings
    of the edition report are built from. Nothing is computed in the renderer, so the
    printed document and the machine-readable record cannot disagree about a number.
    """
    context = _lineage_context(current)

    previous_lineages, previous_duplicates = _index_lineages(previous.rows("lineages"))
    current_lineages, current_duplicates = _index_lineages(current.rows("lineages"))

    added_names = sorted(set(current_lineages) - set(previous_lineages))
    removed_names = sorted(set(previous_lineages) - set(current_lineages))

    # A lineage name carried by more than one row in either edition cannot be compared row
    # by row, because there is no way to say which row became which. MalAvi ships one of
    # these already (TUPHI01, the same accession under two species assignments), so this is
    # a live case in every release and not a hypothetical. Reporting the ambiguity is the
    # only honest option: keeping the last row seen and calling the result a change is how
    # a duplicate turns into a fabricated correction.
    ambiguous_names = sorted(set(previous_duplicates) | set(current_duplicates))

    # Primary facts that changed on a lineage MalAvi already held. The sequence is
    # reported as changed-or-not rather than shown: it is 479 characters, the change is
    # usually one of them, and a document nobody can read is not a record.
    fact_changes: List[Dict[str, Any]] = []
    comparable = (set(previous_lineages) & set(current_lineages)) - set(ambiguous_names)
    for name in sorted(comparable):
        was, now = previous_lineages[name], current_lineages[name]
        changed = {}
        for column in LINEAGE_FACT_COLUMNS:
            old, new = _text(was.get(column)), _text(now.get(column))
            if old == new:
                continue
            if column == "SEQUENCE":
                changed[column] = {"was": f"{len(old)} bp", "now": f"{len(new)} bp",
                                   "detail": "sequence replaced"}
            else:
                changed[column] = {"was": old, "now": new}
        if changed:
            fact_changes.append({"lineage": name, "changed": changed})

    # Row-level differences, per table.
    tables: Dict[str, Any] = {}
    _all_added_references: List[Dict[str, str]] = []
    _all_removed_references: List[Dict[str, str]] = []
    for table in COMPARED_TABLES:
        if table in previous.missing:
            tables[table] = {
                "table": table, "title": TABLE_TITLES[table], "compared": False,
                "note": f"the previous edition's {PREVIOUS_TABLE_FILES[table]} table was "
                        f"not found beside its Grand Lineage Summary, so this table was "
                        f"not compared",
                "previous_rows": None, "current_rows": len(current.rows(table)),
                "added": 0, "removed": 0, "modified": 0,
                "added_rows": [], "removed_rows": [], "modified_rows": [],
                "by_reference": [], "ambiguous_keys": 0, "truncated": False,
            }
            continue
        diff = diff_table(table, previous.rows(table), current.rows(table))
        if table == "references":
            _all_added_references = list(diff.added)
            _all_removed_references = list(diff.removed)
        tables[table] = {
            "table": table, "title": TABLE_TITLES[table], "compared": True, "note": "",
            "previous_rows": diff.previous_rows, "current_rows": diff.current_rows,
            "added": len(diff.added), "removed": len(diff.removed),
            "modified": len(diff.modified),
            "added_rows": diff.added[:example_limit],
            "removed_rows": diff.removed[:example_limit],
            "modified_rows": diff.modified[:example_limit],
            "by_reference": _by_reference(diff.added),
            "ambiguous_keys": diff.ambiguous_keys,
            "truncated": max(len(diff.added), len(diff.removed),
                             len(diff.modified)) > example_limit,
        }

    # Host species and countries appearing in MalAvi for the first time. Computed from the
    # whole table rather than from the added rows, because a host is new to the database
    # only if no earlier record anywhere held it.
    hosts_compared = "host_records" not in previous.missing
    previous_hosts = {_host_binomial(r) for r in previous.rows("host_records")}
    current_hosts = {_host_binomial(r) for r in current.rows("host_records")}
    previous_countries = _distinct(previous.rows("host_records"), "COUNTRY_NAME")
    current_countries = _distinct(current.rows("host_records"), "COUNTRY_NAME")

    references_by_name = {_text(row.get("REFERENCE_NAME")): row
                          for row in current.rows("references")}
    # Built from the WHOLE added set, not from tables["references"]["added_rows"], which
    # is capped at example_limit. Taking it from the capped list made the count every
    # consumer prints as "studies added" silently truncate at 300 -- and made
    # example_limit=0 report an edition as having added no studies at all.
    added_reference_names = [_text(row.get("REFERENCE_NAME"))
                             for row in _all_added_references]
    records_per_reference = Counter()
    for table in ("host_records", "vector_records"):
        for row in current.rows(table):
            records_per_reference[_text(row.get("REFERENCE_NAME"))] += 1

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "example_limit": example_limit,
        "editions": {
            "previous": {"label": previous.label, "sources": previous.sources,
                         "missing_tables": list(previous.missing)},
            "current": {"label": current.label, "sources": current.sources},
        },
        "totals": _totals(previous, current),
        "lineages": {
            "added": [_describe_lineage(current_lineages[name], context)
                      for name in added_names],
            "removed": [_describe_lineage(previous_lineages[name], {})
                        for name in removed_names],
            "fact_changes": fact_changes,
            "added_count": len(added_names),
            "removed_count": len(removed_names),
            # Names carried by more than one row, and therefore not compared. Reported
            # with the count on each side so a reader can see which edition introduced
            # the duplicate.
            "ambiguous": [{"lineage": name,
                           "previous_rows": previous_duplicates.get(
                               name, 1 if name in previous_lineages else 0),
                           "current_rows": current_duplicates.get(
                               name, 1 if name in current_lineages else 0)}
                          for name in ambiguous_names],
        },
        "references": {
            "added": [{"reference": name,
                       "year": _text(references_by_name.get(name, {}).get(
                           "PUBLICATION_YEAR")),
                       "title": _text(references_by_name.get(name, {}).get("TITLE")),
                       "journal": _text(references_by_name.get(name, {}).get(
                           "JOURNAL_NAME")),
                       "study_type": _text(references_by_name.get(name, {}).get(
                           "STUDY_TYPE")),
                       "records": records_per_reference.get(name, 0)}
                      for name in sorted(added_reference_names)],
            # From the whole removed set, for the same reason as "added" above: the
            # removed_rows listing is capped at example_limit, and reading the retired
            # studies from it was the bug fixed for the added side and left on this one.
            "removed": [_text(row.get("REFERENCE_NAME"))
                        for row in _all_removed_references],
        },
        "hosts": {
            "compared": hosts_compared,
            "new_species": sorted(current_hosts - previous_hosts) if hosts_compared else [],
            "retired_species": sorted(previous_hosts - current_hosts) if hosts_compared
                               else [],
            "new_countries": sorted(current_countries - previous_countries)
                             if hosts_compared else [],
        },
        "tables": tables,
        "summary_columns": _summary_column_diff(previous, current, ambiguous_names),
    }
