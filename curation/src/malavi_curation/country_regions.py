# @title The country to MalAvi-region mapping behind the Grand Lineage Summary
# @purpose Load the committed country -> region table, resolve one host record to one
#          region, and re-derive the table from a release so the committed one can be
#          checked rather than trusted.
# @why The Grand Lineage Summary's twelve region columns are derived, and nothing in the
#      repository recorded how a country becomes a region. Without this table a release
#      cannot be built at all.
# @input reference/country_regions.csv
# @output in-memory mapping (no files written)
# @program python
# @critical-var REGION_COLUMNS
# @critical-var HAWAII_COUNTRY
# @critical-var HAWAII_COUNTRY_REGION
"""How a host record's geography becomes one of MalAvi's twelve regions.

The Grand Lineage Summary carries twelve region columns, and they are **not** the
``CONTINENT_NAME`` column under another name: that column has six values, these are
twelve. Africa is split into sub-Saharan and North-Africa-with-the-Middle-East, the
Americas into three, the Pacific into Australia-and-New-Zealand and Oceania, and Hawaii
is pulled out of the United States entirely. So the mapping is from ``COUNTRY_NAME``,
with one sub-national exception, and it lived only in whatever produced the release at
Lund.

**Why the table is inferred rather than invented.** MalAvi's regions do not follow any
standard scheme, and guessing them would have quietly changed the database's meaning.
Three examples from the committed table, each of which a reasonable person would have got
wrong from first principles:

* **Russia is EUROPE**, not Asia.
* **Mexico is CENTRAL_AMERICA**, not North America.
* **Armenia is NORTH_AFRICA_AND_MIDDLE_EAST**, not Asia or Europe.

Each of those is recoverable from the release itself: a lineage whose host records name
exactly one country must have got its region flags from that country. Ninety-eight of the
125 countries are settled that way, most of them unanimously across dozens of lineages.
The rest carry ``BASIS=authored`` and were written by hand following the conventions the
inferred rows establish -- every Caribbean country in the inferred set maps to
CENTRAL_AMERICA, so Grenada and the Cayman Islands do too. Four genuinely debatable
entries carry ``REVIEW=review`` and are a curator's call, not a maintainer's.

**The Hawaii rule is exclusive, not additive.** A United States record from Hawaii sets
HAWAI and does *not* set NORTH_AMERICA. That is measurable rather than assumed: in the
2026-03-23 release exactly three lineages have a Hawaii host record and exactly those
three carry the HAWAI flag, and the one whose only US records are Hawaiian (FREMIN01) has
no NORTH_AMERICA flag.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .config import repo_root

# The twelve region columns, in the order the Grand Lineage Summary carries them. The
# order is the release's own and is load-bearing: emitting a release means writing these
# columns in this sequence.
REGION_COLUMNS = (
    "EUROPE",
    "SUB_SAHARAN_AFRICA",
    "NORTH_AFRICA_AND_MIDDLE_EAST",
    "NORTH_AMERICA",
    "HAWAI",
    "CENTRAL_AMERICA",
    "SOUTH_AMERICA",
    "ASIA",
    "AUSTRALIA_AND_NEW_ZEALAND",
    "OCEANIA",
    "ANTARCTICA",
    "UNKNOWN_REGION",
)

# The sub-national exception. Hawaii is a region in its own right, so a United States
# record has to be read at state level before the country mapping is consulted.
HAWAII_COUNTRY = "United States"
HAWAII_COUNTRY_REGION = "Hawaii"
HAWAII_REGION = "HAWAI"

# Where a record with no usable country lands. The release already uses the country name
# "Unknown Country" for this and maps it here; a blank country goes to the same place,
# because "we were not told" and "we were told it was unknown" are the same fact.
UNKNOWN_REGION = "UNKNOWN_REGION"

# Where the committed table lives. Under reference/ rather than config/ because it is
# data a curator maintains, not a knob a program reads for its own behavior.
_TABLE_PATH = ("reference", "country_regions.csv")


def table_path(root: Optional[Path] = None) -> Path:
    """The committed country -> region table."""
    return (Path(root) if root else repo_root()).joinpath(*_TABLE_PATH)


def load_region_map(path: Optional[Path] = None) -> Dict[str, str]:
    """Country name -> region column name.

    Keys are stripped but otherwise verbatim, because they are matched against
    ``COUNTRY_NAME`` exactly as the records spell it. Normalizing case or punctuation here
    would silently merge two country spellings that a curator may well have intended to
    keep apart, and would hide the fact that a new release had introduced a new spelling.
    """
    path = Path(path) if path else table_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} is missing. The Grand Lineage Summary's region columns cannot be "
            f"derived without it, so a release cannot be built.")
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    mapping: Dict[str, str] = {}
    for row in rows:
        country = (row.get("COUNTRY_NAME") or "").strip()
        region = (row.get("REGION") or "").strip()
        if not region:
            # A row left deliberately blank for a curator to fill. Skipping it rather than
            # defaulting to UNKNOWN_REGION means unmapped_countries() reports it, which is
            # the difference between "nobody has decided yet" and "somebody decided
            # unknown".
            continue
        if region not in REGION_COLUMNS:
            raise ValueError(
                f"{path.name}: {country!r} maps to {region!r}, which is not one of the "
                f"twelve region columns.")
        mapping[country] = region
    return mapping


def rows_needing_review(path: Optional[Path] = None) -> List[Dict[str, str]]:
    """Entries a curator has been asked to confirm, and entries nobody has filled in.

    Surfaced by the release build so an authored guess cannot ride into a published
    release simply because nobody looked at the file again.
    """
    path = Path(path) if path else table_path()
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)
                if (row.get("REVIEW") or "").strip()
                or not (row.get("REGION") or "").strip()]


def region_for(country: str, country_region: str = "",
               mapping: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The region one record belongs to, or None if its country is not in the table.

    ``None`` is deliberately not UNKNOWN_REGION. An unmapped country is a gap in the
    table that somebody must close; folding it into UNKNOWN_REGION would file it under a
    real region and there would be nothing left to notice.
    """
    mapping = load_region_map() if mapping is None else mapping
    country = (country or "").strip()
    # State before country: a Hawaiian record is HAWAI and nothing else.
    if country == HAWAII_COUNTRY and (country_region or "").strip() == HAWAII_COUNTRY_REGION:
        return HAWAII_REGION
    return mapping.get(country)


def regions_for_rows(rows: Iterable[Dict[str, Any]],
                     mapping: Optional[Dict[str, str]] = None) -> set:
    """Every region a set of host records touches."""
    mapping = load_region_map() if mapping is None else mapping
    found = set()
    for row in rows:
        region = region_for(row.get("COUNTRY_NAME", ""),
                            row.get("COUNTRY_REGION_NAME", ""), mapping)
        if region:
            found.add(region)
    return found


def unmapped_countries(rows: Iterable[Dict[str, Any]],
                       mapping: Optional[Dict[str, str]] = None) -> Dict[str, int]:
    """Country names appearing in records that the table does not cover, with row counts.

    A new release that adds a country nobody has classified would otherwise produce a
    lineage with no region flags and no complaint, which reads exactly like a lineage that
    genuinely has none.
    """
    mapping = load_region_map() if mapping is None else mapping
    missing: Counter = Counter()
    for row in rows:
        country = (row.get("COUNTRY_NAME") or "").strip()
        if region_for(country, row.get("COUNTRY_REGION_NAME", ""), mapping) is None:
            missing[country] += 1
    return dict(missing)


def infer_from_release(host_rows: Sequence[Dict[str, Any]],
                       summary_rows: Sequence[Dict[str, Any]]
                       ) -> Dict[str, Counter]:
    """Re-derive the mapping from a release, as evidence rather than as an import.

    A lineage whose host records name exactly one country got its region flags from that
    country, so each such lineage is one vote. Returns country -> Counter of votes, so a
    caller can see both the winner and how unanimous it was; the committed table records
    the same tallies in its EVIDENCE column.

    This exists so the committed table can be **checked** against the release rather than
    taken on trust -- see the tests, which assert that no inferred row has drifted away
    from the release it was read out of.
    """
    flags: Dict[str, set] = {}
    for row in summary_rows:
        flags[row["LINEAGE_NAME"]] = {
            column for column in REGION_COLUMNS if (row.get(column) or "").strip() == "1"}

    by_lineage: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in host_rows:
        by_lineage[row["LINEAGE_NAME"]].append(row)

    votes: Dict[str, Counter] = defaultdict(Counter)
    for lineage, rows in by_lineage.items():
        countries = {(row.get("COUNTRY_NAME") or "").strip() for row in rows}
        if len(countries) != 1:
            continue
        observed = flags.get(lineage)
        if not observed:
            # No flags at all. That is the release's staleness, not evidence about the
            # country, so it must not be counted as a vote for anything.
            continue
        (country,) = countries
        for region in observed:
            votes[country][region] += 1
    return dict(votes)
