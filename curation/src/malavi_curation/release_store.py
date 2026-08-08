"""The canonical record store: MalAvi as diffable text in git.

Until now the authoritative MalAvi has been a release ZIP of spreadsheets, produced
elsewhere and consumed here. Taking release construction in-house means something has to
*be* the database, and this is it: one CSV per table, committed, with provenance columns
the release itself never carries.

**Why plain text in git rather than a database file.** Every release becomes a reviewable
diff, every correction becomes a commit with a reason attached, and the history of the
data becomes auditable in exactly the way the history of the code already is. A binary
store gives none of that, and a scientific database whose past states cannot be inspected
is one nobody can check a published analysis against.

**Primary facts only.** The Grand Lineage Summary is *derived* — its host, genus, family,
order and vector counts, its PASSERIFORMES flag and its twelve region columns are all
computable from the host and vector records. Storing it would let it drift out of
agreement with the records it summarizes, which is a class of defect the current release
already exhibits. Here it is regenerated at build time, so that disagreement cannot exist.

**What the store adds and the release does not carry.** Two provenance columns per row:

* ``_source`` — where the row came from: a submission id, ``seed`` for the rows imported
  from the last externally-produced release, or a correction reference.
* ``_added`` — the release in which the row first appeared.

They make it possible to answer "why is this record here, and since when" years later,
which is the question a curator inheriting a disputed record actually has. They are
stripped on the way out, because the release format is Staffan's and is not ours to
change.
"""
from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Columns the store adds to every table. Stripped when a release is emitted.
#
# RECORD_ID is the identity of a row and is assigned once, at import or insert, then never
# changed. Natural keys were tried first and cannot do this job: measured against the
# 2026-03-23 release, 302 of the 18,493 host records are byte-identical to another row --
# same lineage, host, site, reference, counts, everything. Nothing distinguishes them, so
# no combination of their own columns can identify them.
#
# Two consequences follow, and both are improvements:
#   * identical rows are preserved rather than silently merged. They are somebody's
#     submitted data, and deduplicating them is a curator's decision with a reason
#     attached, not something an importer does on the way past;
#   * a diff between releases is exact -- "this row changed" can be told from "this row
#     went and another arrived", which a natural key cannot distinguish either.
#
# Natural-key uniqueness does not disappear; it becomes a *check* that reports violations
# (see natural_key_violations) instead of a foundation that quietly loses rows.
PROVENANCE_COLUMNS = ("RECORD_ID", "_source", "_added")

# The seed marker: rows imported from the last release MalAvi produced externally. They
# have no submission behind them in our records, and pretending otherwise would be worse
# than saying so.
SEED = "seed"


@dataclass(frozen=True)
class TableSpec:
    """One table in the store: its file, its columns, and what identifies a row."""

    name: str
    columns: Sequence[str]
    key: Sequence[str]          # the natural key -- checked for uniqueness, not relied on
    description: str
    prefix: str = "REC"         # identifier prefix, e.g. HST-000123

    @property
    def filename(self) -> str:
        return f"{self.name}.csv"


# The primary tables. Column names and order are the release's own, so that emitting a
# release is a projection rather than a translation.
#
# grand_lineage_summary is deliberately absent: it is generated. See the module docstring.
TABLES: Dict[str, TableSpec] = {
    "lineages": TableSpec(
        name="lineages",
        prefix="LIN",
        columns=("LINEAGE_NAME", "GENBANK_ACC", "SEQ_LENGTH", "GENUS_NAME",
                 "SPECIES_NAME", "SEQUENCE"),
        key=("LINEAGE_NAME",),
        description="One row per named lineage: its sequence, its accession, and the "
                    "morphospecies it is assigned to, if any.",
    ),
    "host_records": TableSpec(
        name="host_records",
        prefix="HST",
        columns=("LINEAGE_NAME", "ALT_NAME", "PARASITE_GENUS", "ORDER_NAME",
                 "FAMILY_NAME", "GENUS_NAME", "SPECIES_NAME", "SUB_SPECIES_NAME",
                 "HOST_STATUS", "HOST_AGE", "HOST_ENVIRONMENT", "CONTINENT_NAME",
                 "COUNTRY_NAME", "COUNTRY_REGION_NAME", "SITE_NAME", "SITE_COORDINATES",
                 "NUMBER_FOUND", "NUMBER_TESTED", "REFERENCE_NAME", "COMMENT"),
        # A record is lineage x host x site x reference. The same association reported by
        # two studies is two records, which is the point -- MalAvi records who found what
        # where, not merely that it exists.
        key=("LINEAGE_NAME", "SPECIES_NAME", "SITE_NAME", "REFERENCE_NAME"),
        description="The detections themselves: this lineage, in this host, at this "
                    "site, reported by this study.",
    ),
    "vector_records": TableSpec(
        name="vector_records",
        prefix="VEC",
        columns=("LINEAGE_NAME", "VECTOR_SPECIES", "VECTOR_METHOD", "COUNTRY_NAME",
                 "SITE_NAME", "REFERENCE_NAME"),
        key=("LINEAGE_NAME", "VECTOR_SPECIES", "SITE_NAME", "REFERENCE_NAME"),
        description="Lineages detected in arthropod vectors.",
    ),
    "alt_names": TableSpec(
        name="alt_names",
        prefix="ALT",
        columns=("LINEAGE_NAME", "ALT_NAME", "REFERENCE_NAME"),
        key=("LINEAGE_NAME", "ALT_NAME", "REFERENCE_NAME"),
        description="Names a lineage appeared under in a publication or in GenBank. "
                    "This is how MalAvi keeps synonyms traceable.",
    ),
    "morpho_species": TableSpec(
        name="morpho_species",
        prefix="MSP",
        columns=("LINEAGE_NAME", "GENUS_NAME", "SPECIES_NAME", "REFERENCE_NAME",
                 "MORPHOLOGY_COMMENT"),
        key=("LINEAGE_NAME", "GENUS_NAME", "SPECIES_NAME", "REFERENCE_NAME"),
        description="Morphological species assignments, with the study that made them.",
    ),
    "references": TableSpec(
        name="references",
        prefix="REF",
        columns=("REFERENCE_NAME", "PUBLICATION_YEAR", "TITLE", "JOURNAL_NAME",
                 "VOLUME_PAGES", "STUDY_TYPE"),
        key=("REFERENCE_NAME",),
        description="Every study any record cites.",
    ),
}


def store_dir(repo_root: Path) -> Path:
    """Where the store lives. Tracked in git, unlike anything under curation/intake."""
    return Path(repo_root) / "data" / "records"


def row_key(spec: TableSpec, row: Dict[str, Any]) -> tuple:
    """A row's **natural** key: the columns that ought to identify it.

    Used for sorting, for reporting duplicates, and for rendering a diff in terms a
    curator recognizes. It is NOT the row's identity -- see RECORD_ID above.

    Compared on stripped strings so that whitespace introduced by a spreadsheet round
    trip does not read as a different record.
    """
    return tuple((row.get(column) or "").strip() for column in spec.key)


def record_id(row: Dict[str, Any]) -> str:
    """A row's identity, or an empty string if it has not been assigned one yet."""
    return (row.get("RECORD_ID") or "").strip()


def assign_ids(spec: TableSpec, rows: Sequence[Dict[str, Any]],
               start: int = 1) -> List[Dict[str, Any]]:
    """Give every row without a RECORD_ID a new one, leaving existing ids untouched.

    Existing ids are never reissued: a curator decision, a correction and a published
    record may all point at one, and renumbering would detach every reference at once.
    """
    used = {record_id(row) for row in rows if record_id(row)}
    number = start
    out: List[Dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        if not record_id(row):
            candidate = f"{spec.prefix}-{number:06d}"
            while candidate in used:
                number += 1
                candidate = f"{spec.prefix}-{number:06d}"
            row["RECORD_ID"] = candidate
            used.add(candidate)
            number += 1
        out.append(row)
    return out


def natural_key_violations(spec: TableSpec, rows: Sequence[Dict[str, Any]]
                           ) -> List[Dict[str, Any]]:
    """Rows sharing a natural key: a data-quality finding, not a loading error.

    The seed release contains these, so reporting beats refusing. Each entry carries the
    record ids involved, which is what a curator needs to act on one.
    """
    groups: Dict[tuple, List[str]] = {}
    for row in rows:
        groups.setdefault(row_key(spec, row), []).append(record_id(row))
    return [{"key": list(key), "record_ids": ids}
            for key, ids in sorted(groups.items()) if len(ids) > 1]


def read_table(directory: Path, spec: TableSpec) -> List[Dict[str, str]]:
    """Read one table from the store. A missing file is an empty table, not an error."""
    path = Path(directory) / spec.filename
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_table(directory: Path, spec: TableSpec, rows: Iterable[Dict[str, Any]],
                with_provenance: bool = True) -> Path:
    """Write one table, with a stable column order and a stable row order.

    **Sorted on write, always.** A store whose row order depends on the order records
    happened to be added produces a meaningless diff -- every release would appear to
    change every line. Sorting by the row key makes a diff show exactly what changed and
    nothing else, which is the whole reason for keeping the data as text.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    columns = list(spec.columns) + (list(PROVENANCE_COLUMNS) if with_provenance else [])

    # Sorted by natural key so a diff reads in terms a curator recognizes, then by
    # id so the order is total even among rows the natural key cannot separate.
    ordered = sorted(rows, key=lambda row: (row_key(spec, row), record_id(row)))
    path = directory / spec.filename

    # Written to a temporary file and renamed, never opened over the top of the existing
    # one. This is the authoritative MalAvi: opening the real path with "w" truncates it
    # before a single row is written, so an interruption, a full disk or a Ctrl-C midway
    # leaves a half-written authoritative table and no copy of what it replaced. The
    # rename is atomic, so a reader sees either the old table or the new one.
    handle_fd, temporary = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore",
                                    quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            writer.writeheader()
            for row in ordered:
                writer.writerow({column: (row.get(column) if row.get(column) is not None
                                          else "") for column in columns})
            handle.flush()
            os.fsync(handle.fileno())
        # Preserve the existing mode; mkstemp creates 0600, which would progressively
        # narrow the store's permissions on every write.
        try:
            os.chmod(temporary, path.stat().st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def read_store(directory: Path) -> Dict[str, List[Dict[str, str]]]:
    """The whole store, table name -> rows."""
    return {name: read_table(directory, spec) for name, spec in TABLES.items()}


def store_is_populated(directory: Path) -> bool:
    """Whether a store already holds records."""
    directory = Path(directory)
    if not directory.is_dir():
        return False
    return any(read_table(directory, spec) for spec in TABLES.values())


def write_store(directory: Path, store: Dict[str, Iterable[Dict[str, Any]]],
                allow_overwrite: bool = True) -> List[Path]:
    """Write every table in the store.

    ``allow_overwrite=False`` refuses to write over a store that already holds records.
    The seed path passes it: seeding assigns fresh record ids from 1 and stamps every row
    ``_source=seed``, so running it a second time over a store that curators have since
    added to would discard their record identities and their provenance, and would do it
    silently. Prose in a runbook saying "runs once" is not a guard.
    """
    directory = Path(directory)
    if not allow_overwrite and store_is_populated(directory):
        raise ValueError(
            f"{directory} already holds records. Seeding again would reassign every "
            f"record id from 1 and overwrite the provenance of anything added since. "
            f"If that is genuinely intended, move the existing store aside first.")
    return [write_table(directory, TABLES[name], rows)
            for name, rows in store.items() if name in TABLES]


def stamp(rows: Iterable[Dict[str, Any]], source: str, release: str
          ) -> List[Dict[str, Any]]:
    """Attach provenance to rows that have none, leaving existing provenance alone.

    Existing provenance is never overwritten: the whole value of ``_added`` is that it
    records when a row *first* appeared, and rewriting it on every build would turn it
    into a copy of the current release date and answer nothing.
    """
    out = []
    for row in rows:
        row = dict(row)
        row.setdefault("_source", source)
        row.setdefault("_added", release)
        if not row.get("_source"):
            row["_source"] = source
        if not row.get("_added"):
            row["_added"] = release
        out.append(row)
    return out


def duplicate_keys(spec: TableSpec, rows: Sequence[Dict[str, Any]]) -> List[tuple]:
    """Row keys that appear more than once.

    The release this store is seeded from is known to contain duplicates, so this
    reports them rather than refusing to load. What to do about each is a curator's
    decision and belongs in a correction, not in an importer.
    """
    seen: Dict[tuple, int] = {}
    for row in rows:
        key = row_key(spec, row)
        seen[key] = seen.get(key, 0) + 1
    return sorted(key for key, count in seen.items() if count > 1)
