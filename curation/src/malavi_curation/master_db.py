"""Import the Lund master database (``MALAVI.sqlite``) into the record store.

**Where this sits.** MalAvi was curated at Lund in a relational database that Staffan
Bensch exported, by hand, into the flat release tables (``Hosts_and_Sites``,
``GrandLineageSummary`` and so on). The record store in ``data/records/`` was seeded from
the 2026-03-23 export of that database (see :mod:`release_seed`). On 2026-09-15 Staffan
sent the database itself, with every dataset from his waiting list added, and from that
point on the store is where MalAvi is curated. This module is the bridge: it reproduces
Lund's flat export from the relational tables, then folds the result into the store
without discarding the identities and provenance the store has already given its rows.

**The export rules were measured, not designed.** Every rule below was recovered by
exporting the database and comparing the result against the 2026-03-23 release tables
until the only remaining differences were edits Staffan had made since March. The rules
that were not obvious, and would be "simplified" wrongly by someone reading the schema:

* ``DELETED`` is a soft-delete timestamp, but 121 references carry the empty string in
  it rather than NULL. Only a non-empty value is a deletion. Treating ``''`` as deleted
  drops 2,711 host records.
* A reference's ``PUBLICATION_YEAR`` of 9999 means "unpublished", and so does the study
  type "Unpublished" (two rows carry a real year with that type). The References table
  of a release carries only published studies, but the host records that cite an
  unpublished study are still released, so the filter applies to one table only.
* ``ALT_NAME`` in a host record is every alternative name the lineage was given *by that
  record's reference*, comma-joined, ordered by GenBank accession and then
  alphabetically. The database stores some of these already comma-joined in one cell
  (``CATFUS09,CATFUS21``), so cells are split before they are de-duplicated and re-joined.
* The Lineage Names table is not the alternative-names table exported whole: it is the
  set of (lineage, alternative name, reference) triples for which a **host record** of
  that lineage under that reference exists. Exporting the whole table adds 666 rows the
  release never carried.
* The Grand Lineage Summary fans out one row per distinct morphospecies a lineage is
  linked to, which is why ``TUPHI01`` is two rows.
* Free-text fields carry carriage returns and non-breaking spaces from spreadsheet
  pastes (``'Perrin et al 2026\\r\\n'``); all whitespace runs are collapsed to one space
  and trimmed, which is what the release tables show.

**Nothing here judges the data.** Rows that are duplicated, that cite a reference with no
row of its own, or that collide after whitespace normalization are imported as they are
and listed in the findings, so that a curator sees them rather than a program deciding
silently. Correcting them is a decision for the corrections track, with a reason attached.

**Provenance survives the import.** Rows the store already holds keep their
``RECORD_ID``, ``_source`` and ``_added``; a row Staffan edited keeps its identity when
its natural key still identifies it uniquely; new rows are stamped ``_source=seed`` --
they were curated at Lund, outside this project's review loop, exactly like the original
seed -- and ``_added`` is the release they first appear in. Rows Staffan removed are
removed here and named in the report.

**Corrections already made here are replayed.** ``data/corrections.csv`` records
decisions taken over the seeded store (a longitude sign, a misspelled method). The
database does not know about them, so the import would silently undo them. After the
merge each logged correction is planned again and re-applied where its fault is still
present; the report says which were needed and which the database had already fixed.
"""
from __future__ import annotations

import ast
import hashlib
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .release_store import (
    SEED, TABLES, TableSpec, assign_ids, record_id, row_key, stamp,
)
from .store_corrections import Correction, plan as plan_correction

# The store tables this import produces, in the order they are reported.
STORE_TABLE_ORDER = ("lineages", "host_records", "vector_records", "alt_names",
                     "morpho_species", "references")

# Publication year the Lund database uses for "not published". Such references are
# cited by records but are not rows of the release's References table.
UNPUBLISHED_YEAR = 9999
# ... and the study type Lund gives a few unpublished studies that carry a real year.
UNPUBLISHED_STUDY_TYPE = "Unpublished"

# The value ``HOST_ENVIRONMENT`` takes for each state of the ``CAPTIVITY`` flag. A NULL
# flag exports as Wild, which is what the 2026-03-23 release shows for the 119 rows that
# carry one.
ENVIRONMENT_CAPTIVE = "Captivity"
ENVIRONMENT_WILD = "Wild"

# Length of the cytochrome b barcode window every stored sequence is expected to have.
EXPECTED_SEQUENCE_LENGTH = 479


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def normalize_text(value: Any) -> str:
    """A cell as the release shows it: whitespace runs collapsed to one space, trimmed.

    ``re``'s ``\\s`` matches non-breaking spaces and carriage returns, both of which the
    database carries from spreadsheet pastes. ``None`` is the empty string.
    """
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def is_live(deleted: Any) -> bool:
    """Whether a row's ``DELETED`` column means it is still part of the database.

    A NULL or empty value is live; anything else (a timestamp) is a deletion. See the
    module docstring for why the empty string must count as live.
    """
    return deleted is None or str(deleted).strip() == ""


def _number(value: Any) -> str:
    """An integer-valued cell as an integer string; blank when absent."""
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def sha256_of(path: Path) -> str:
    """The file's SHA-256, so the import report can say exactly which file it read."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Export: relational tables -> the six store tables
# ---------------------------------------------------------------------------

def _rows(connection: sqlite3.Connection, sql: str) -> List[sqlite3.Row]:
    return list(connection.execute(sql))


def _lookup(connection: sqlite3.Connection, table: str, key: str, value: str
            ) -> Dict[Any, str]:
    """``{key: value}`` over one lookup table, values whitespace-normalized."""
    return {row[key]: normalize_text(row[value])
            for row in _rows(connection, f"SELECT * FROM {table}")}


def export_tables(sqlite_path: Path) -> Tuple[Dict[str, List[Dict[str, str]]],
                                              List[Dict[str, Any]]]:
    """Reproduce the flat release tables from the relational database.

    Returns ``(tables, findings)``. ``tables`` maps each store table name to its rows,
    with exactly the store's columns and no provenance. ``findings`` is a list of
    data-quality observations, each ``{"kind", "detail", ...}``, for the report.
    """
    connection = sqlite3.connect(f"file:{Path(sqlite_path)}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    findings: List[Dict[str, Any]] = []
    try:
        return _export(connection, findings), findings
    finally:
        connection.close()


def _export(c: sqlite3.Connection, findings: List[Dict[str, Any]]
            ) -> Dict[str, List[Dict[str, str]]]:
    # ---- lookup tables ---------------------------------------------------------
    # References: id -> normalized name, plus the year so the References table can leave
    # unpublished studies out while every other table still resolves them by name.
    reference_name: Dict[int, str] = {}
    reference_year: Dict[int, Any] = {}
    for row in _rows(c, "SELECT * FROM LM_REFERENCES"):
        reference_name[row["REFERENCE_ID"]] = normalize_text(row["REFERENCE_NAME"])
        reference_year[row["REFERENCE_ID"]] = row["PUBLICATION_YEAR"]

    # Host / vector / parasite taxonomy, one flattened row per species id. Normalized
    # here, once: TAX_SPECIES carries names with a trailing newline ('Conirostrum
    # cinereum\n'), which the release never showed and which would make one host
    # species count as two.
    species: Dict[int, Dict[str, str]] = {
        row["SPECIES_ID"]: {column: normalize_text(row[column])
                            for column in ("SPECIES_NAME", "GENUS_NAME",
                                           "FAMILY_NAME", "ORDER_NAME")}
        for row in _rows(c, """
        SELECT s.SPECIES_ID, s.SPECIES_NAME, g.GENUS_NAME, f.FAMILY_NAME, o.ORDER_NAME
        FROM TAX_SPECIES s
        JOIN TAX_GENUS g ON g.GENUS_ID = s.GENUS_ID
        JOIN TAX_FAMILY f ON f.FAMILY_ID = g.FAMILY_ID
        JOIN TAX_ORDER o ON o.ORDER_ID = f.ORDER_ID""")}
    subspecies = _lookup(c, "TAX_SUB_SPECIES", "SUB_SPECIES_ID", "SUB_SPECIES_NAME")
    genus_name = _lookup(c, "TAX_GENUS", "GENUS_ID", "GENUS_NAME")

    # Geography. A country carries its continent; a site carries its coordinates.
    countries: Dict[int, sqlite3.Row] = {row["COUNTRY_ID"]: row for row in _rows(c, """
        SELECT co.COUNTRY_ID, co.COUNTRY_NAME, ct.CONTINENT_NAME
        FROM LM_COUNTRIES co
        LEFT JOIN LM_CONTINENTS ct ON ct.CONTINENT_ID = co.CONTINENT_ID""")}
    country_regions = _lookup(c, "LM_COUNTRY_REGIONS", "COUNTRY_REGION_ID",
                              "COUNTRY_REGION_NAME")
    sites: Dict[int, sqlite3.Row] = {row["SITE_ID"]: row
                                     for row in _rows(c, "SELECT * FROM LM_SITES")}

    host_age = _lookup(c, "LM_HOST_AGE", "HOST_AGE_ID", "HOST_AGE")
    host_status = _lookup(c, "LM_HOST_STATUS", "HOST_STATUS_ID", "HOST_STATUS")
    sequence_length = _lookup(c, "LM_SEQ_LENGTH", "SEQ_LENGTH_ID", "SEQ_LENGTH")
    vector_method = _lookup(c, "LM_VECTOR_METHODS", "VECTOR_METHOD_ID",
                            "VECTOR_METHOD_NAME")
    journal_name = _lookup(c, "LM_JOURNALS", "JOURNAL_ID", "JOURNAL_NAME")
    study_type = _lookup(c, "LM_STUDY_TYPES", "STUDY_TYPE_ID", "STUDY_TYPE")

    # ---- lineages: MASTER + SEQUENCES, minus soft deletes ----------------------
    master: Dict[str, sqlite3.Row] = {}
    deleted_lineages: List[str] = []
    for row in _rows(c, "SELECT * FROM MASTER ORDER BY LINEAGE_NAME"):
        if is_live(row["DELETED"]):
            master[row["LINEAGE_NAME"]] = row
        else:
            deleted_lineages.append(row["LINEAGE_NAME"])
    if deleted_lineages:
        findings.append({"kind": "soft_deleted_lineages_excluded",
                         "detail": deleted_lineages})

    sequences: Dict[str, str] = {}
    for row in _rows(c, "SELECT * FROM SEQUENCES"):
        if is_live(row["DELETED"]):
            sequences[row["LINEAGE_NAME"]] = normalize_text(row["SEQUENCE"])
    odd_length = sorted((name, len(sequences.get(name, ""))) for name in master
                        if len(sequences.get(name, "")) != EXPECTED_SEQUENCE_LENGTH)
    if odd_length:
        findings.append({"kind": "sequence_length_not_expected",
                         "expected": EXPECTED_SEQUENCE_LENGTH, "detail": odd_length})

    # ---- alternative names -----------------------------------------------------
    # Per (lineage, reference): the distinct alternative names, ordered by GenBank
    # accession then by entry order, cells split on commas first. See the docstring.
    alt_candidates: Dict[Tuple[str, int], List[Tuple[str, int, str]]] = defaultdict(list)
    null_lineage_alt_rows = 0
    for row in _rows(c, "SELECT * FROM LM_LINEAGE_NAMES ORDER BY LINEAGE_KEY"):
        if not is_live(row["DELETED"]):
            continue
        if row["LINEAGE_NAME"] is None:
            null_lineage_alt_rows += 1
            continue
        for part in normalize_text(row["ALT_NAME"]).split(","):
            part = part.strip()
            if part:
                alt_candidates[(row["LINEAGE_NAME"], row["REFERENCE_ID"])].append(
                    (normalize_text(row["GENBANK_ACCNR"]), row["LINEAGE_KEY"], part))
    if null_lineage_alt_rows:
        findings.append({"kind": "alt_name_rows_without_lineage_skipped",
                         "detail": null_lineage_alt_rows})
    # Ordered by accession, then by the name itself: that reproduces 155 of the 156
    # multi-name cells in the 2026-03-23 release, where entry order reproduces 149.
    alt_names_for: Dict[Tuple[str, int], List[str]] = {
        key: list(dict.fromkeys(name for _acc, _key, name
                                in sorted(items, key=lambda t: (t[0], t[2]))))
        for key, items in alt_candidates.items()}

    # ---- host records ----------------------------------------------------------
    host_records: List[Dict[str, str]] = []
    host_pairs: set = set()          # (lineage, reference id) with at least one record
    unresolved: Counter = Counter()
    for row in _rows(c, "SELECT * FROM HOSTS_AND_SITES ORDER BY FINDING_ID"):
        if not is_live(row["DELETED"]):
            continue
        lineage = row["LINEAGE_NAME"]
        host = species.get(row["HOST_SPECIES_ID"])
        country = countries.get(row["COUNTRY_ID"])
        site = sites.get(row["SITE_ID"])
        parasite = master.get(lineage)
        reference = reference_name.get(row["REFERENCE_ID"], "")
        if host is None:
            unresolved["host_species"] += 1
        if country is None:
            unresolved["country"] += 1
        if parasite is None:
            unresolved["lineage_not_in_master"] += 1
        if not reference:
            unresolved["reference"] += 1
        host_pairs.add((lineage, row["REFERENCE_ID"]))
        # The two raw strings are joined first and the whole cell normalized after, not
        # each half trimmed: 1,284 sites carry a trailing space inside LATITUDE, and the
        # release shows it as "57°10.00000' , 016°58.00000'". Trimming each half would
        # silently change 1,284 published values on the way through. Cleaning them is a
        # decision for the corrections track, with a reason attached.
        coordinates = ""
        if site is not None and normalize_text(site["LATITUDE"]):
            coordinates = normalize_text(f"{site['LATITUDE']}, {site['LONGITUDE']}")
        host_records.append({
            "LINEAGE_NAME": lineage,
            "ALT_NAME": ",".join(alt_names_for.get((lineage, row["REFERENCE_ID"]), [])),
            "PARASITE_GENUS": genus_name.get(parasite["PARASITE_GENUS_ID"], "")
            if parasite is not None else "",
            "ORDER_NAME": host["ORDER_NAME"] if host else "",
            "FAMILY_NAME": host["FAMILY_NAME"] if host else "",
            "GENUS_NAME": host["GENUS_NAME"] if host else "",
            "SPECIES_NAME": host["SPECIES_NAME"] if host else "",
            "SUB_SPECIES_NAME": subspecies.get(row["HOST_SUB_SPECIES_ID"], ""),
            "HOST_STATUS": host_status.get(row["HOST_STATUS_ID"], ""),
            "HOST_AGE": host_age.get(row["HOST_AGE_ID"], ""),
            "HOST_ENVIRONMENT": ENVIRONMENT_CAPTIVE if row["CAPTIVITY"] == 1
            else ENVIRONMENT_WILD,
            "CONTINENT_NAME": normalize_text(country["CONTINENT_NAME"]) if country else "",
            "COUNTRY_NAME": normalize_text(country["COUNTRY_NAME"]) if country else "",
            "COUNTRY_REGION_NAME": country_regions.get(row["COUNTRY_REGION_ID"], ""),
            "SITE_NAME": normalize_text(site["SITE_NAME"]) if site else "",
            "SITE_COORDINATES": coordinates,
            "NUMBER_FOUND": _number(row["NUMBER_FOUND"]),
            "NUMBER_TESTED": _number(row["NUMBER_TESTED"]),
            "REFERENCE_NAME": reference,
            "COMMENT": normalize_text(row["COMMENT"]),
        })
    for what, count in sorted(unresolved.items()):
        findings.append({"kind": f"host_records_with_unresolved_{what}", "detail": count})

    # ---- vector records --------------------------------------------------------
    vector_records: List[Dict[str, str]] = []
    for row in _rows(c, "SELECT * FROM VECTOR_DATA ORDER BY rowid"):
        if not is_live(row["DELETED"]):
            continue
        vector = species.get(row["VECTOR_SPECIES"])
        country = countries.get(row["COUNTRY_ID"])
        site = sites.get(row["SITE_ID"])
        vector_records.append({
            "LINEAGE_NAME": row["LINEAGE_NAME"],
            "VECTOR_SPECIES": vector["SPECIES_NAME"] if vector else "",
            "VECTOR_METHOD": vector_method.get(row["VECTOR_METHOD_ID"], ""),
            "COUNTRY_NAME": normalize_text(country["COUNTRY_NAME"]) if country else "",
            "SITE_NAME": normalize_text(site["SITE_NAME"]) if site else "",
            "REFERENCE_NAME": reference_name.get(row["REFERENCE_ID"], ""),
        })

    # ---- alternative names table: only pairs that have a host record -----------
    alt_rows: set = set()
    for (lineage, reference_id), names in alt_names_for.items():
        if (lineage, reference_id) in host_pairs and reference_id in reference_name:
            for name in names:
                alt_rows.add((lineage, name, reference_name[reference_id]))
    alt_names = [{"LINEAGE_NAME": lineage, "ALT_NAME": name, "REFERENCE_NAME": reference}
                 for lineage, name, reference in sorted(alt_rows)]

    # ---- morphospecies links ---------------------------------------------------
    morpho_species: List[Dict[str, str]] = []
    morpho_names_for: Dict[str, List[str]] = defaultdict(list)
    for row in _rows(c, "SELECT * FROM MORPHO_SPECIES ORDER BY FINDING_ID"):
        if not is_live(row["DELETED"]):
            continue
        parasite = species.get(row["SPECIES_ID"])
        name = normalize_text(parasite["SPECIES_NAME"]) if parasite else ""
        morpho_species.append({
            "LINEAGE_NAME": row["LINEAGE_NAME"],
            "GENUS_NAME": genus_name.get(row["GENUS_ID"], ""),
            "SPECIES_NAME": name,
            "REFERENCE_NAME": reference_name.get(row["REFERENCE_ID"], ""),
            "MORPHOLOGY_COMMENT": normalize_text(row["MORPHOLOGY_COMMENT"]),
        })
        if name not in morpho_names_for[row["LINEAGE_NAME"]]:
            morpho_names_for[row["LINEAGE_NAME"]].append(name)

    # ---- references: published studies only ------------------------------------
    # A study is unpublished when its year is 9999 OR its study type is "Unpublished":
    # two rows ("Zhu unpubl 4", year 2002; "Juan van Rooyen 2012") carry a real year
    # with the unpublished study type, and the 2026-03-23 References table left both
    # out. Their records, if any, are still exported like every other citation.
    references: List[Dict[str, str]] = []
    for row in _rows(c, "SELECT * FROM LM_REFERENCES ORDER BY REFERENCE_ID"):
        if (row["PUBLICATION_YEAR"] == UNPUBLISHED_YEAR
                or study_type.get(row["STUDY_TYPE_ID"], "") == UNPUBLISHED_STUDY_TYPE):
            continue
        references.append({
            "REFERENCE_NAME": normalize_text(row["REFERENCE_NAME"]),
            "PUBLICATION_YEAR": _number(row["PUBLICATION_YEAR"]),
            "TITLE": normalize_text(row["TITLE"]),
            "JOURNAL_NAME": journal_name.get(row["JOURNAL_ID"], ""),
            "VOLUME_PAGES": normalize_text(row["VOLUME_PAGES"]),
            "STUDY_TYPE": study_type.get(row["STUDY_TYPE_ID"], ""),
        })
    # Names that collide after normalization are one join key in the store and in
    # malaviR, so the rows behind them fan out on every join. Reported, never merged.
    name_counts = Counter(row["REFERENCE_NAME"] for row in references)
    collisions = sorted(name for name, count in name_counts.items() if count > 1)
    if collisions:
        findings.append({"kind": "reference_names_not_unique", "detail": collisions})
    cited = {reference_name.get(pair[1], "") for pair in host_pairs}
    published = set(name_counts)
    unpublished_cited = sorted(name for name in cited
                               if name and name not in published)
    findings.append({"kind": "cited_references_absent_from_references_table",
                     "detail": len(unpublished_cited),
                     "note": "unpublished studies (year 9999) or names the database "
                             "has no reference row for; the release has always "
                             "carried their records"})

    # ---- lineages (the primary columns of the Grand Lineage Summary) -----------
    lineages: List[Dict[str, str]] = []
    for name, row in master.items():
        for morpho in (morpho_names_for.get(name) or [""]):
            lineages.append({
                "LINEAGE_NAME": name,
                "GENBANK_ACC": normalize_text(row["GENBANK_ACC"]),
                "SEQ_LENGTH": sequence_length.get(row["SEQ_LENGTH_ID"], ""),
                "GENUS_NAME": genus_name.get(row["PARASITE_GENUS_ID"], ""),
                "SPECIES_NAME": morpho,
                "SEQUENCE": sequences.get(name, ""),
            })
    multi = sorted(name for name, names in morpho_names_for.items()
                   if len(names) > 1 and name in master)
    if multi:
        findings.append({"kind": "lineages_with_several_morphospecies",
                         "detail": multi,
                         "note": "one Grand Lineage Summary row per morphospecies, as "
                                 "the release has always done"})
    missing_sequence = sorted(name for name in master if not sequences.get(name))
    if missing_sequence:
        findings.append({"kind": "lineages_without_sequence", "detail": missing_sequence})

    return {
        "lineages": lineages,
        "host_records": host_records,
        "vector_records": vector_records,
        "alt_names": alt_names,
        "morpho_species": morpho_species,
        "references": references,
    }


# ---------------------------------------------------------------------------
# Comparison against a release: the reproduction check
# ---------------------------------------------------------------------------

def signature(spec: TableSpec, row: Dict[str, Any]) -> tuple:
    """A row as a tuple of its store columns, whitespace-normalized."""
    return tuple(normalize_text(row.get(column)) for column in spec.columns)


def compare_rows(spec: TableSpec, before: Sequence[Dict[str, Any]],
                 after: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Multiset comparison of two row lists on the store's columns.

    Whole rows, not keys, for the reason :func:`release_seed.compare_tables` gives: the
    release holds rows its natural key cannot separate, and a keyed comparison would
    drop them and call the result clean.
    """
    old = Counter(signature(spec, row) for row in before)
    new = Counter(signature(spec, row) for row in after)
    only_after = sorted((new - old).elements())
    only_before = sorted((old - new).elements())
    return {
        "n_before": len(before), "n_after": len(after),
        "n_only_after": len(only_after), "n_only_before": len(only_before),
        "only_after": [list(row) for row in only_after[:25]],
        "only_before": [list(row) for row in only_before[:25]],
        "identical": not only_after and not only_before,
    }


# ---------------------------------------------------------------------------
# Merge: exported rows into the existing store, keeping identities
# ---------------------------------------------------------------------------

def merge_table(spec: TableSpec, existing: Sequence[Dict[str, Any]],
                exported: Sequence[Dict[str, Any]], release: str
                ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fold one exported table into its store table.

    Three passes, in order of confidence:

    1. an exported row identical (on the store's columns) to an unmatched existing row
       takes that row's identity and provenance;
    2. an exported row is paired with an unmatched existing row of the same natural key,
       closest row first when several share the key -- this is an edit Staffan made to a
       record the store already holds, and it is reported with the columns that changed;
    3. everything else is a new row: fresh id, ``_source=seed``, ``_added=<release>``.

    Existing rows no exported row claimed are removed and reported by id and key.
    """
    # Pass 1: identical rows.
    unmatched_existing: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in existing:
        unmatched_existing[signature(spec, row)].append(row)
    merged: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    kept = 0
    for row in exported:
        bucket = unmatched_existing.get(signature(spec, row))
        if bucket:
            old = bucket.pop()
            merged.append(_carry(spec, row, old))
            kept += 1
        else:
            pending.append(row)
    leftovers = [row for bucket in unmatched_existing.values() for row in bucket]

    # Pass 2: natural-key matches. Within one natural key, the unmatched existing rows
    # and the unmatched exported rows are paired greedily by fewest differing columns,
    # so an edit to a record keeps that record's identity even when the key does not
    # separate it from a sibling (two records of one lineage in one host at one site
    # from one study, differing only in age class, are the common case). Whatever is
    # left unpaired on either side is a genuine addition or removal.
    by_key_old: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in leftovers:
        by_key_old[row_key(spec, row)].append(row)
    by_key_new: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in pending:
        by_key_new[row_key(spec, row)].append(row)
    changed: List[Dict[str, Any]] = []
    claimed_old: set = set()
    still_pending: List[Dict[str, Any]] = []
    for key, news in by_key_new.items():
        olds = list(by_key_old.get(key, []))
        # Every (old, new) pair with its number of differing columns, closest first.
        candidates = sorted(
            ((sum(1 for column in spec.columns
                  if normalize_text(old.get(column)) != normalize_text(new.get(column))),
              i, j) for i, old in enumerate(olds) for j, new in enumerate(news)))
        paired_old: set = set()
        paired_new: set = set()
        for _distance, i, j in candidates:
            if i in paired_old or j in paired_new:
                continue
            paired_old.add(i)
            paired_new.add(j)
            old, new = olds[i], news[j]
            claimed_old.add(id(old))
            merged.append(_carry(spec, new, old))
            changed.append({
                "record_id": record_id(old), "key": list(key),
                "columns": {column: {"was": normalize_text(old.get(column)),
                                     "now": normalize_text(new.get(column))}
                            for column in spec.columns
                            if normalize_text(old.get(column))
                            != normalize_text(new.get(column))}})
        still_pending.extend(new for j, new in enumerate(news) if j not in paired_new)
    removed = [row for row in leftovers if id(row) not in claimed_old]

    # Pass 3: new rows. Ids start above the highest the store has EVER issued, including
    # the rows being removed here: ``assign_ids`` only knows the ids of the rows it is
    # handed, so without this a removed row's id would be reissued to a new record and
    # every reference to the old one (a correction, an edition report) would silently
    # point at the wrong row.
    highest = 0
    for row in existing:
        match = re.fullmatch(rf"{spec.prefix}-(\d+)", record_id(row))
        if match:
            highest = max(highest, int(match.group(1)))
    new_rows = stamp([dict(row) for row in still_pending], source=SEED, release=release)
    merged = assign_ids(spec, merged + new_rows, start=highest + 1)

    report = {
        "n_existing": len(existing), "n_exported": len(exported), "n_merged": len(merged),
        "n_kept_identical": kept, "n_changed": len(changed), "n_added": len(new_rows),
        "n_removed": len(removed),
        "changed": changed,
        "added": [{"key": list(row_key(spec, row))} for row in new_rows],
        "removed": [{"record_id": record_id(row), "key": list(row_key(spec, row)),
                     "_source": row.get("_source", ""), "_added": row.get("_added", "")}
                    for row in removed],
    }
    return merged, report


def _carry(spec: TableSpec, new_row: Dict[str, Any], old_row: Dict[str, Any]
           ) -> Dict[str, Any]:
    """The exported row's values under the existing row's identity and provenance."""
    out = {column: new_row.get(column, "") for column in spec.columns}
    out["RECORD_ID"] = record_id(old_row)
    out["_source"] = old_row.get("_source") or SEED
    out["_added"] = old_row.get("_added") or ""
    return out


def merge_store(existing: Dict[str, List[Dict[str, Any]]],
                exported: Dict[str, List[Dict[str, Any]]], release: str
                ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any]]:
    """:func:`merge_table` over every store table."""
    store: Dict[str, List[Dict[str, Any]]] = {}
    report: Dict[str, Any] = {}
    for name in STORE_TABLE_ORDER:
        spec = TABLES[name]
        # Lund's export is the whole truth about the rows that CAME from Lund and says
        # nothing about rows a submission put here. Only seed rows are merged against
        # the export; anything else in the store (an ingested submission's records)
        # is carried through untouched, or a second import would delete every
        # submission ingested since the first.
        seed_rows = [row for row in existing.get(name, [])
                     if (row.get("_source") or SEED) == SEED]
        other_rows = [row for row in existing.get(name, [])
                      if (row.get("_source") or SEED) != SEED]
        merged, report[name] = merge_table(spec, seed_rows, exported.get(name, []),
                                           release)
        report[name]["n_kept_from_submissions"] = len(other_rows)
        store[name] = merged + other_rows
    return store, report


# ---------------------------------------------------------------------------
# Replaying the correction log
# ---------------------------------------------------------------------------

_SELECTOR_RE = re.compile(r"^(where|site|record)\s+(.*)$", re.S)


def correction_from_log(row: Dict[str, str]) -> Correction:
    """Rebuild a :class:`Correction` from one row of ``data/corrections.csv``.

    The log stores the selector as :meth:`Correction.describe_selector` printed it --
    ``where COLUMN='value'``, ``site 'name'`` or ``record ID`` -- with the value in
    Python repr form, so it is read back with ``ast.literal_eval`` rather than by
    stripping quotes, which would mishandle a value that itself contains one.
    """
    selector = normalize_text(row.get("SELECTOR"))
    match = _SELECTOR_RE.match(selector)
    if not match:
        raise ValueError(f"{row.get('CORRECTION_ID')}: unreadable selector {selector!r}")
    kind, rest = match.group(1), match.group(2).strip()
    column = ""
    if kind == "where":
        column, _, literal = rest.partition("=")
        value = ast.literal_eval(literal.strip())
    elif kind == "site":
        value = ast.literal_eval(rest)
    else:
        value = rest
    return Correction(
        table=row["TABLE"], column=row["COLUMN"], new_value=row["NEW_VALUE"],
        reason=row.get("REASON", ""), actor=row.get("ACTOR", "maintainer"),
        selector_kind=kind, selector_value=str(value), selector_column=column.strip())


def replay_corrections(store: Dict[str, List[Dict[str, Any]]],
                       log_rows: Iterable[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Re-apply every logged correction whose fault the imported data still carries.

    Returns one entry per log row: ``status`` is ``reapplied`` with the row count, or
    ``not_needed`` when no row matches any more -- which means the database had already
    been corrected the same way, or the rows are gone. The log itself is not appended
    to: the decision was recorded when it was first taken, and this is that same decision
    being upheld.
    """
    outcome: List[Dict[str, Any]] = []
    for row in log_rows:
        correction = correction_from_log(row)
        changes = plan_correction(store, correction)
        changed_ids = {change.record for change in changes}
        for stored in store.get(correction.table, []):
            if record_id(stored) in changed_ids:
                stored[correction.column] = correction.new_value
        outcome.append({
            "correction_id": row.get("CORRECTION_ID", ""),
            "table": correction.table, "selector": correction.describe_selector(),
            "column": correction.column, "new_value": correction.new_value,
            "originally_changed": row.get("ROWS_CHANGED", ""),
            "status": "reapplied" if changes else "not_needed",
            "rows_changed": len(changes),
        })
    return outcome


# ---------------------------------------------------------------------------
# Writing the export as CSV, for inspection and for the record
# ---------------------------------------------------------------------------

def write_export(directory: Path, tables: Dict[str, List[Dict[str, str]]]) -> List[Path]:
    """Write each exported table as ``<table>.csv`` with the store's columns, no ids."""
    import csv
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for name in STORE_TABLE_ORDER:
        spec = TABLES[name]
        path = directory / spec.filename
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(spec.columns),
                                    extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in sorted(tables.get(name, []), key=lambda r: row_key(spec, r)):
                writer.writerow(row)
        written.append(path)
    return written
