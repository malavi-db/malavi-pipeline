"""Seed the record store from the last externally produced release.

This runs **once**. MalAvi's authoritative form has been a release ZIP produced at Lund;
after this, the record store is authoritative and each release is generated from it. That
is a real change in where the database lives, and it should be done deliberately and
verifiably rather than as a side effect of the first build.

**The test that matters is the round trip.** Import the current release into the store,
regenerate the release from the store, and compare it to what we started with. Anything
that differs is either something the import lost — which must be fixed — or something the
existing release contains that its own records do not support, which is a finding worth
having. Both are far better learned now than during the first real release.

Nothing here judges the data. Rows that are duplicated or internally inconsistent are
imported as they are and reported. Correcting them is a curator's decision, made through
the corrections track with a reason attached, not something an importer should do quietly
on the way past.
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .release_store import (
    SEED, TABLES, TableSpec, assign_ids, natural_key_violations, record_id,
    row_key, stamp, write_store,
)

# Which release CSV feeds which store table. The Grand Lineage Summary is read for the
# primary lineage facts only -- its counts and region flags are regenerated, never stored.
_SOURCE_FILES = {
    "lineages": "grand_lineage_summary",
    "host_records": "hosts_and_sites",
    "vector_records": "vector_data",
    "alt_names": "lineage_names",
    "morpho_species": "morpho_species",
    "references": "references",
}


def release_table_path(downloads: Path, table: str, release: str) -> Path:
    return Path(downloads) / f"{table}_{release}.csv"


def read_release_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def seed_store(downloads: Path, release: str) -> Tuple[Dict[str, List[Dict[str, Any]]],
                                                       Dict[str, Any]]:
    """Build the store from a release's CSV tables.

    Returns ``(store, report)``. The report carries per-table row counts, duplicate keys
    and any missing source file, so the seed can be inspected before it is written
    anywhere.
    """
    store: Dict[str, List[Dict[str, Any]]] = {}
    report: Dict[str, Any] = {"release": release, "tables": {}, "missing": []}

    for name, spec in TABLES.items():
        source = _SOURCE_FILES[name]
        path = release_table_path(downloads, source, release)
        if not path.is_file():
            report["missing"].append(str(path))
            store[name] = []
            continue

        raw = read_release_csv(path)
        # Every column the store expects must actually be present in the release CSV.
        # Without this, a renamed or dropped column is read as `""` for every row -- and
        # because the verifier reads the original the same way, it compares blank against
        # blank and reports a clean round trip over a column it has silently emptied.
        header = set(raw[0]) if raw else set()
        absent = [column for column in spec.columns if column not in header]
        if absent:
            raise ValueError(
                f"{path.name} is missing column(s) {', '.join(absent)} that the store "
                f"expects. Seeding would blank them for all {len(raw)} rows and the "
                f"round-trip check would not notice, because it reads the source the "
                f"same way.")

        # Project onto the store's columns. The Grand Lineage Summary carries twenty-odd
        # derived columns that are deliberately dropped here; everything else is a
        # straight copy, so a column the release has and the store does not is a
        # deliberate omission rather than a loss.
        rows = [{column: (row.get(column) or "") for column in spec.columns}
                for row in raw]
        # Identity is assigned here, once, and never reissued.
        store[name] = assign_ids(spec, stamp(rows, source=SEED, release=release))

        report["tables"][name] = {
            "source": path.name,
            "rows": len(rows),
            "columns_dropped": sorted(set(raw[0]) - set(spec.columns)) if raw else [],
            "natural_key_violations": natural_key_violations(spec, store[name]),
        }
    return store, report


def compare_tables(spec: TableSpec, before: List[Dict[str, str]],
                   after: List[Dict[str, str]]) -> Dict[str, Any]:
    """Compare two versions of one table by row key, ignoring order and provenance.

    Order is ignored because the store sorts on write and the release does not; comparing
    on order would report thousands of differences that are not differences. Provenance is
    ignored because the release never carries it.
    """
    # Compared as multisets of full rows, not by key. The release contains rows its own
    # natural key cannot separate -- 302 host records are byte-identical to another row --
    # so a key-indexed comparison would silently drop them and report a clean round trip
    # while having lost data. Counting whole rows cannot make that mistake.
    def signature(row: Dict[str, str]) -> tuple:
        return tuple((row.get(column) or "").strip() for column in spec.columns)

    old = Counter(signature(row) for row in before)
    new = Counter(signature(row) for row in after)

    added = sorted((new - old).elements())
    removed = sorted((old - new).elements())
    changed: List[Dict[str, Any]] = []

    return {
        "added": [list(k) for k in added[:50]],
        "removed": [list(k) for k in removed[:50]],
        "changed": changed,
        "n_added": len(added),
        "n_removed": len(removed),
        "n_changed": len(changed),
        "identical": not added and not removed and not changed,
    }


def verify_round_trip(downloads: Path, release: str,
                      store: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Does the store still contain what the release contained?

    Compares every stored table against the release table it came from. A clean result
    means the seed lost nothing and the store can be trusted as the new authority.
    Anything else needs explaining before the first release is built from it.
    """
    out: Dict[str, Any] = {"release": release, "tables": {}, "clean": True,
                           "missing": []}
    for name, spec in TABLES.items():
        path = release_table_path(downloads, _SOURCE_FILES[name], release)
        if not path.is_file():
            # Emphatically NOT a skip. seed_store writes an empty table when its source
            # is absent, so skipping the comparison here would certify an empty
            # authoritative table as a clean migration -- unrecoverable data loss
            # presented as a verified one. This is the single gate between "the release
            # is the database" and "the store is the database".
            out["missing"].append(str(path))
            out["tables"][name] = {"identical": False,
                                   "error": f"source table not found: {path.name}"}
            out["clean"] = False
            continue
        original = read_release_csv(path)
        result = compare_tables(spec, original, store.get(name, []))
        out["tables"][name] = result
        if not result["identical"]:
            out["clean"] = False
    return out
