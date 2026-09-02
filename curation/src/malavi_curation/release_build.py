# @title Build a MalAvi release from the canonical record store
# @purpose Regenerate the Grand Lineage Summary from the primary records, emit the five
#          release tables and the cytochrome b alignment, and pack them into the
#          MalAvi_<date>.zip that malaviR consumes.
# @why Nothing produced a release. The store became authoritative when it was seeded, and
#      until this existed there was no way to get a release back out of it.
# @input data/records/*.csv
# @input reference/country_regions.csv
# @output MalAvi_<release>.zip (five .xlsx tables + one .fas alignment)
# @output release_diff.json
# @program python
# @program openpyxl
# @critical-var GRAND_LINEAGE_SUMMARY_COLUMNS
# @critical-var RELEASE_TABLE_FILES
# @critical-var FASTA_WRAP
# @critical-var GENUS_PREFIXES
"""Turn the record store back into a MalAvi release.

``release_seed`` moved the database's home: the release ZIP used to be authoritative and
the store was derived from it, and after seeding that is reversed. This is the other half
of that move -- the store is the database, and a release is a **projection** of it, built
fresh each time.

**The Grand Lineage Summary is regenerated, never stored.** Its five tallies, its
Passeriformes flag and its twelve region columns are all computable from the host and
vector records, and computing them at build time is the only way they cannot drift out of
agreement with the records they summarize. The 2026-03-23 release shows why that matters:
measured against its own record tables, **248 lineages carry host records and no region
flag at all**, and 279 have records supporting a region the summary does not flag, against
**zero** where the summary flags a region the records do not support. That asymmetry is the
signature of a summary that stopped being regenerated while records kept arriving under
it. Rebuilding corrects all of it, and ``diff_against_release`` reports every correction
rather than making it quietly.

(Those three numbers were 248 / 266 / 25 when this was written. Re-measured 2026-08-10:
the 25 regressions are gone, closed by the ``authored`` rows added to
``country_regions.csv``, so the rebuild now loses nothing at all. They are worth
re-measuring rather than trusting -- the recipe is in ``diff_against_release``.)

**Every derivation here was measured against the legacy release, not assumed.** Where a
rule had more than one plausible reading, both were tried and the one that reproduces the
legacy summary was kept: ``SUM_HOST`` counts distinct host binomials rather than rows,
``SUM_VECTORS`` counts distinct vector species rather than rows, the Hawaii flag replaces
NORTH_AMERICA rather than accompanying it, and the region flags read the vector table as
well as the host table. That last one matters most -- 614 lineages have no host record at
all, and reading hosts alone would silently strip the geography from every one of them.

**What is emitted, and what is deliberately not.** The ZIP carries what the legacy release
carried and what ``malaviR/data-raw/process_release.R`` reads: five ``.xlsx`` tables and
one ``.fas`` alignment, in a folder named for the release, all stamped with the release
date. The Table of Lineage Names is **not** among them, and that is correct rather than an
omission -- MalAvi packs a paper's original names into the host table's ``ALT_NAME``
column and the site unpacks them for browsing (``export/lib/tables.R``). It is a view,
not a table, and the legacy ZIP never carried it either.

**The alignment is data, not a rendering.** Every aligned sequence already lives in the
store's ``lineages.SEQUENCE`` column, gapped, at the full 479 bp alignment width -- so the
FASTA is written from the store rather than being carried alongside it. Verified against
the 2026-03-23 release: 5,358 of the 5,363 lineages that have both a stored sequence and
a FASTA record agree exactly, and the five that do not are a disagreement inside the
legacy release itself, reported by the diff.
"""
from __future__ import annotations

import csv
import json
import re
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .country_regions import (
    REGION_COLUMNS, load_region_map, region_for, rows_needing_review, unmapped_countries,
)
from . import reference_names
from .release_store import SEED, TABLES, read_store, store_dir

# A published citation as MalAvi writes it: "<Authors> <year>", with the optional letter
# that tells two papers from one group in one year apart ("Hellgren et al 2007a"). Used
# to find the published sibling of a "<Authors> unpubl" name -- the shape publish_reference
# renames to -- and nothing else, so the author part is left as loose as
# reference_names leaves it.
_PUBLISHED_CITATION = re.compile(r"^(?P<authors>.+?)\s+(?P<year>\d{4}[a-z]?)$")

# The Grand Lineage Summary's columns, in the release's own order. The first five and the
# last are primary facts copied from the store's lineages table; everything between them
# is derived here.
GRAND_LINEAGE_SUMMARY_COLUMNS: Tuple[str, ...] = (
    "LINEAGE_NAME", "GENBANK_ACC", "SEQ_LENGTH", "GENUS_NAME", "SPECIES_NAME",
    "SUM_VECTORS", "SUM_HOST", "SUM_GENUS", "SUM_FAMILY", "SUM_ORDER", "PASSERIFORMES",
    *REGION_COLUMNS,
    "SEQUENCE",
)

# Which store table becomes which file in the ZIP. The basenames are the legacy release's
# and are matched by prefix in process_release.R, so they are not ours to restyle.
RELEASE_TABLE_FILES: Dict[str, str] = {
    "grand_lineage_summary": "GrandLineageSummary",
    "host_records": "Hosts_and_Sites",
    "morpho_species": "MorphoSpecies",
    "references": "References",
    "vector_records": "VectorData",
}

# The single worksheet name the legacy tables use. readxl takes the first sheet either
# way, but matching it keeps a rebuilt release diffable against an archived one.
SHEET_NAME = "sheet1"

# Line width of the emitted FASTA, matching the legacy alignment.
FASTA_WRAP = 60

# The one-letter prefix each parasite genus contributes to an alignment tip label.
GENUS_PREFIXES: Dict[str, str] = {
    "Plasmodium": "P",
    "Haemoproteus": "H",
    "Leucocytozoon": "L",
}

# The tally columns that are written blank rather than "0" when they are zero, because
# that is what the release does. PASSERIFORMES is not among them: it carries an explicit
# "0", and the region columns carry a blank, and both of those are the release's habits
# rather than anything meaningful.
_BLANK_WHEN_ZERO = ("SUM_VECTORS", "SUM_HOST", "SUM_GENUS", "SUM_FAMILY", "SUM_ORDER")


def _text(value: Any) -> str:
    """A cell as a stripped string. ``None`` and missing are both the empty string."""
    return (value or "").strip() if isinstance(value, str) else ("" if value is None
                                                                 else str(value).strip())


# ---------------------------------------------------------------------------
# Deriving the Grand Lineage Summary
# ---------------------------------------------------------------------------

def derive_summary(store: Dict[str, List[Dict[str, Any]]],
                   region_map: Optional[Dict[str, str]] = None
                   ) -> List[Dict[str, str]]:
    """Build the Grand Lineage Summary from the primary records.

    One row per lineage, in the store's lineage order. Every derived value is recomputed;
    nothing is carried over from any previous summary, because carrying values over is
    precisely how the legacy summary went stale.
    """
    region_map = load_region_map() if region_map is None else region_map

    # Index the records once. Both tables are read whole several times otherwise, and the
    # host table is 18,000 rows.
    hosts_by_lineage: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in store.get("host_records", []):
        hosts_by_lineage[_text(row.get("LINEAGE_NAME"))].append(row)

    vectors_by_lineage: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in store.get("vector_records", []):
        vectors_by_lineage[_text(row.get("LINEAGE_NAME"))].append(row)

    summary: List[Dict[str, str]] = []
    for lineage in store.get("lineages", []):
        name = _text(lineage.get("LINEAGE_NAME"))
        hosts = hosts_by_lineage.get(name, [])
        vectors = vectors_by_lineage.get(name, [])

        # The tallies. Each counts *distinct* values, ignoring blanks -- a host record
        # with no family recorded must not become a family of its own.
        #
        # SUM_HOST counts distinct host binomials rather than rows, because MalAvi records
        # one lineage-in-one-host-at-one-site-by-one-study per row and the same host found
        # by three studies is one host. SUM_VECTORS counts distinct vector species for the
        # same reason; measured against the 2026-03-23 release, counting rows instead
        # disagrees on 39 lineages and counting species disagrees on one.
        binomials = {(_text(r.get("GENUS_NAME")), _text(r.get("SPECIES_NAME")))
                     for r in hosts if _text(r.get("SPECIES_NAME"))}
        genera = {_text(r.get("GENUS_NAME")) for r in hosts if _text(r.get("GENUS_NAME"))}
        families = {_text(r.get("FAMILY_NAME")) for r in hosts if _text(r.get("FAMILY_NAME"))}
        orders = {_text(r.get("ORDER_NAME")) for r in hosts if _text(r.get("ORDER_NAME"))}
        vector_species = {_text(r.get("VECTOR_SPECIES")) for r in vectors
                          if _text(r.get("VECTOR_SPECIES"))}

        row: Dict[str, str] = {
            # Primary facts, copied straight across.
            "LINEAGE_NAME": name,
            "GENBANK_ACC": _text(lineage.get("GENBANK_ACC")),
            "SEQ_LENGTH": _text(lineage.get("SEQ_LENGTH")),
            "GENUS_NAME": _text(lineage.get("GENUS_NAME")),
            "SPECIES_NAME": _text(lineage.get("SPECIES_NAME")),
            "SEQUENCE": _text(lineage.get("SEQUENCE")),
            # Derived tallies.
            "SUM_VECTORS": str(len(vector_species)),
            "SUM_HOST": str(len(binomials)),
            "SUM_GENUS": str(len(genera)),
            "SUM_FAMILY": str(len(families)),
            "SUM_ORDER": str(len(orders)),
            # Derived flag. Always explicit, never blank.
            "PASSERIFORMES": "1" if any(
                _text(r.get("ORDER_NAME")).lower() == "passeriformes" for r in hosts) else "0",
        }

        # Blank rather than "0" for the tallies, matching the release.
        for column in _BLANK_WHEN_ZERO:
            if row[column] == "0":
                row[column] = ""

        # The region flags, from host records **and** vector records. A lineage known
        # only from a mosquito still has a geography, and the legacy summary flags it:
        # of the 614 lineages in the 2026-03-23 release with no host record at all, 173
        # carry region flags, and deriving those from the vector table alone reproduces
        # 602 of the 614 exactly. Reading host records only would strip a region from
        # every one of them.
        #
        # Vector records carry no COUNTRY_REGION_NAME, so the Hawaii rule simply never
        # fires for them; region_for handles the missing column rather than requiring one.
        regions = set()
        for record in list(hosts) + list(vectors):
            region = region_for(_text(record.get("COUNTRY_NAME")),
                                _text(record.get("COUNTRY_REGION_NAME")), region_map)
            if region:
                regions.add(region)
        for column in REGION_COLUMNS:
            row[column] = "1" if column in regions else ""

        summary.append(row)
    return summary


# ---------------------------------------------------------------------------
# The alignment
# ---------------------------------------------------------------------------

def fasta_label(lineage: Dict[str, Any]) -> str:
    """The alignment tip label for one lineage.

    ``<prefix>_<LINEAGE>`` for a lineage with no morphospecies, and
    ``<prefix>_<LINEAGE>_<Genus>_<epithet>`` for one that has been linked to a described
    species. A genus outside the three known ones contributes no prefix rather than a
    guessed letter, so an unrecognized genus is visible in the output instead of being
    silently filed under someone else's letter.

    **``SPECIES_NAME`` already holds the binomial, so the genus is not prepended.** All
    238 lineages that carry a species hold one like ``"Leucocytozoon toddi"``, and
    prepending ``GENUS_NAME`` produced ``L_ACCFRA01_Leucocytozoon_Leucocytozoon toddi``:
    a duplicated genus, and a space. Almost every FASTA reader truncates the sequence id
    at the first whitespace, so 238 of the 5,368 records in every release shipped an id
    that silently lost everything after the genus -- and two ``TUPHI01`` rows then
    truncated to the *same* id without the build's duplicate-label check noticing, because
    it compares full labels. Seven ``GENUS_NAME = "N/A"`` lineages also contributed a
    ``/``. Both classes of character are gone now that the binomial is used directly. (Those
    seven were corrected to ``Haemoproteus`` on 2026-08-20 by COR-000031, so no row carries
    ``"N/A"`` today; the handling above still stands for the next unrecognized genus.)

    Whitespace inside the binomial becomes ``_`` rather than being stripped, so the label
    stays reversible: ``Leucocytozoon_toddi`` is still readable as two words.
    """
    lineage_name = _text(lineage.get("LINEAGE_NAME"))
    genus = _text(lineage.get("GENUS_NAME"))
    species = _text(lineage.get("SPECIES_NAME"))
    prefix = GENUS_PREFIXES.get(genus)
    label = f"{prefix}_{lineage_name}" if prefix else lineage_name
    if species:
        label = f"{label}_{'_'.join(species.split())}"
    return label


def build_fasta(lineages: Sequence[Dict[str, Any]], wrap: int = FASTA_WRAP) -> str:
    """The aligned FASTA for a release.

    Lineages with no stored sequence are omitted rather than written as an empty record:
    an alignment row of nothing is not a missing sequence, it is a sequence of gaps, and
    a downstream aligner cannot tell the difference.
    """
    out: List[str] = []
    for lineage in lineages:
        sequence = _text(lineage.get("SEQUENCE"))
        if not sequence:
            continue
        out.append(f">{fasta_label(lineage)}")
        for start in range(0, len(sequence), wrap):
            out.append(sequence[start:start + wrap])
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------------------
# Emitting the release
# ---------------------------------------------------------------------------

def write_xlsx(path: Path, columns: Sequence[str],
               rows: Iterable[Dict[str, Any]]) -> Path:
    """Write one release table as .xlsx.

    Every cell is written as text, and an empty value is written as a genuinely empty cell
    rather than an empty string, because that is what the legacy tables contain and what
    readxl turns into NA. Writing "" instead would give malaviR a zero-length string that
    is not NA, and every downstream ``is.na()`` would quietly stop matching.
    """
    # Imported here rather than at module scope so that importing this module -- which the
    # tests and the diff path do -- does not require openpyxl to be installed.
    import openpyxl

    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet(title=SHEET_NAME)
    sheet.append(list(columns))
    for row in rows:
        sheet.append([(_text(row.get(column)) or None) for column in columns])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(path))
    return path


def build_release(store: Dict[str, List[Dict[str, Any]]], release: str,
                  destination: Path,
                  region_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Build the release ZIP, and report what went into it.

    ``destination`` receives ``MalAvi_<release>.zip``. The staged folder inside the ZIP is
    named for the release, exactly as the legacy archives are, because process_release.R
    unzips into a temporary directory and globs for its five tables by prefix.
    """
    region_map = load_region_map() if region_map is None else region_map
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    summary = derive_summary(store, region_map)
    tables = {
        "grand_lineage_summary": (GRAND_LINEAGE_SUMMARY_COLUMNS, summary),
        "host_records": (TABLES["host_records"].columns, store.get("host_records", [])),
        "morpho_species": (TABLES["morpho_species"].columns, store.get("morpho_species", [])),
        "references": (TABLES["references"].columns, store.get("references", [])),
        "vector_records": (TABLES["vector_records"].columns, store.get("vector_records", [])),
    }

    folder = destination / f"MalAvi_{release}"
    folder.mkdir(parents=True, exist_ok=True)
    staged: List[Path] = []
    for name, (columns, rows) in tables.items():
        # Provenance columns are dropped here, by projecting onto the release's own
        # columns. RECORD_ID, _source and _added are how the store answers "why is this
        # row here"; the release format is Staffan's and does not carry them.
        staged.append(write_xlsx(folder / f"{RELEASE_TABLE_FILES[name]}_{release}.xlsx",
                                 columns, rows))

    lineages = store.get("lineages", [])
    fasta = build_fasta(lineages)
    alignment = folder / f"MalAvi_{release}.fas"
    alignment.write_text(fasta, encoding="utf-8")
    staged.append(alignment)

    archive = destination / f"MalAvi_{release}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(staged):
            bundle.write(path, arcname=f"MalAvi_{release}/{path.name}")

    # Anything that would make the release wrong in a way nobody would notice.
    hosts = store.get("host_records", [])
    vectors = store.get("vector_records", [])
    warnings: List[str] = []
    # Host AND vector rows, because derive_summary reads the region flags from both. Until
    # 2026-09-02 only the host rows were checked, so a vector record from a country the
    # table does not cover set no region and drew no warning -- and for the 614 lineages
    # with no host record at all, the vector table is the only geography there is.
    missing = unmapped_countries(list(hosts) + list(vectors), region_map)
    if missing:
        warnings.append(
            f"{len(missing)} country name(s) in the host and vector records are not in "
            f"reference/country_regions.csv, so their records set no region: "
            f"{', '.join(sorted(missing))}")
    review = rows_needing_review()
    if review:
        warnings.append(
            f"{len(review)} row(s) in reference/country_regions.csv are still flagged "
            f"for curator review: "
            f"{', '.join(r['COUNTRY_NAME'] for r in review)}")
    no_sequence = sorted(_text(r.get("LINEAGE_NAME")) for r in lineages
                         if not _text(r.get("SEQUENCE")))
    if no_sequence:
        warnings.append(
            f"{len(no_sequence)} lineage(s) have no sequence and are absent from the "
            f"alignment: {', '.join(no_sequence)}")

    # A lineage name appearing twice. MalAvi's 2026-03-23 release already contains one
    # (TUPHI01, the same accession under two species assignments), so this reports rather
    # than refuses -- but it must be reported, because a summary with a repeated
    # LINEAGE_NAME breaks any downstream join that treats the name as a key.
    name_counts: Dict[str, int] = defaultdict(int)
    for row in lineages:
        name_counts[_text(row.get("LINEAGE_NAME"))] += 1
    repeated = sorted(name for name, count in name_counts.items() if count > 1)
    if repeated:
        warnings.append(
            f"{len(repeated)} lineage name(s) appear more than once in the store, so the "
            f"summary carries duplicate rows: {', '.join(repeated)}")

    # Two sequences under one tip label would be silently dropped or renamed by whatever
    # reads the alignment, so this is checked separately from the name above: distinct
    # lineages can collide on a label, and identical names can produce distinct labels.
    label_counts: Dict[str, int] = defaultdict(int)
    for row in lineages:
        if _text(row.get("SEQUENCE")):
            label_counts[fasta_label(row)] += 1
    duplicate_labels = sorted(label for label, count in label_counts.items() if count > 1)
    if duplicate_labels:
        warnings.append(
            f"{len(duplicate_labels)} alignment tip label(s) are not unique, and a reader "
            f"will drop or rename the duplicates: {', '.join(duplicate_labels)}")

    # ---- referential and arithmetic checks on the records themselves -----------------
    #
    # Added 2026-08-10 after a review found all three shipping silently in the seeded
    # store. Warnings rather than refusals: every one of them is a curator's decision to
    # make about somebody's data, and a release that refuses to build over a citation
    # typo would simply be overridden. What they must not do is go unnoticed.
    #
    # These name studies and their faults, so they belong in the operator's report and the
    # gitignored release report. They must NOT reach the public site or malaviR, where a
    # data fault becomes public blame attached to a contributor.

    # A record citing a reference the release does not contain. The deliberate
    # "<Authors> unpubl" convention is excluded: those rows have no reference row BY
    # DESIGN -- there is nothing to cite until the study appears.
    known_references = {_text(row.get("REFERENCE_NAME"))
                        for row in store.get("references", [])}
    orphan_citations: Dict[str, int] = defaultdict(int)
    blank_citations = 0
    for table_name in ("host_records", "vector_records", "alt_names", "morpho_species"):
        for row in store.get(table_name, []):
            cited = _text(row.get("REFERENCE_NAME"))
            if not cited:
                blank_citations += 1
            elif cited not in known_references and not reference_names.is_unpublished(cited):
                orphan_citations[cited] += 1
    if orphan_citations:
        listed = ", ".join(f"{name} ({count} row(s))"
                           for name, count in sorted(orphan_citations.items()))
        warnings.append(
            f"{len(orphan_citations)} reference name(s) are cited by records but have no "
            f"row in references.csv, and are not marked unpublished: {listed}")
    if blank_citations:
        warnings.append(
            f"{blank_citations} record row(s) carry no REFERENCE_NAME at all, so they "
            f"are published with no attribution")

    # The fault the "unpubl" exclusion above is blind to. publish_reference renames a
    # study's rows from "<Authors> unpubl" to the citation and lifts the embargo on every
    # submission behind the study -- including ones never ingested, whose workbooks still
    # say "unpubl". A later ingest copies the Reference cell verbatim, so the study sits in
    # the store under two names, and neither is an orphan in the sense checked above: one
    # has its reference row, the other is excused as unpublished.
    #
    # Only INGESTED rows are examined, deliberately. The seed carries 20 unpublished names
    # beside a same-author published paper ("Hellgren et al unpubl" next to "Hellgren et
    # al 2004", and so on), every one a different study; they predate the rename program
    # and cannot be this fault, and a warning that fires twenty times on every build is
    # one nobody reads. A row a submission brought can be this fault, and it is named.
    published_by_authors: Dict[str, List[str]] = defaultdict(list)
    for name in known_references:
        match = _PUBLISHED_CITATION.match(name)
        if match:
            published_by_authors[match.group("authors").strip()].append(name)
    split_studies: Dict[str, int] = defaultdict(int)
    for table_name in ("host_records", "vector_records", "alt_names", "morpho_species"):
        for row in store.get(table_name, []):
            source = _text(row.get("_source"))
            if not source or source == SEED:
                continue
            cited = _text(row.get("REFERENCE_NAME"))
            if (reference_names.is_unpublished(cited)
                    and reference_names.authors_of(cited) in published_by_authors):
                split_studies[cited] += 1
    if split_studies:
        listed = ", ".join(
            f"{name} ({count} row(s); published as "
            f"{', '.join(sorted(published_by_authors[reference_names.authors_of(name)]))})"
            for name, count in sorted(split_studies.items()))
        warnings.append(
            f"{len(split_studies)} unpublished citation(s) on ingested rows already have "
            f"a published row under the same authors in references.csv, so one study may "
            f"be in the store under two names -- an ingest after publish_reference.py ran "
            f"copies the workbook's 'unpubl' name verbatim; rename those rows: {listed}")

    # REFERENCE_NAME is the join key malaviR and the site use, so a duplicate fans out
    # every join on it 2x. The build already checks LINEAGE_NAME for the same reason.
    reference_counts: Dict[str, int] = defaultdict(int)
    for row in store.get("references", []):
        reference_counts[_text(row.get("REFERENCE_NAME"))] += 1
    repeated_references = sorted(name for name, count in reference_counts.items()
                                 if count > 1)
    if repeated_references:
        warnings.append(
            f"{len(repeated_references)} reference name(s) appear more than once in "
            f"references.csv, so any join on the name fans out: "
            f"{', '.join(repeated_references)}")

    # More positives than samples. Not arithmetic this program can correct -- which of the
    # two numbers is wrong is a question for the authors -- but it must be visible.
    impossible = []
    for row in hosts:
        found, tested = _text(row.get("NUMBER_FOUND")), _text(row.get("NUMBER_TESTED"))
        if found.isdigit() and tested.isdigit() and int(found) > int(tested):
            impossible.append(f"{_text(row.get('RECORD_ID'))} "
                              f"({_text(row.get('LINEAGE_NAME'))}, {found}/{tested})")
    if impossible:
        warnings.append(
            f"{len(impossible)} host record(s) report more infections than birds tested: "
            f"{', '.join(impossible[:10])}"
            + (f", and {len(impossible) - 10} more" if len(impossible) > 10 else ""))

    # An alignment whose rows are not all the same width is not an alignment. The store
    # holds gapped sequences at a fixed width (479 bp in the seeded release), so more than
    # one width means a sequence was inserted ungapped or against a different reference.
    widths = sorted({len(_text(r.get("SEQUENCE"))) for r in lineages
                     if _text(r.get("SEQUENCE"))})
    if len(widths) > 1:
        warnings.append(
            f"the alignment is ragged: sequences occur at {len(widths)} different widths "
            f"({', '.join(str(w) for w in widths[:10])}). Every stored sequence should be "
            f"gapped to the same alignment width.")

    return {
        "release": release,
        "archive": str(archive),
        "folder": str(folder),
        "files": [p.name for p in staged],
        "rows": {name: len(rows) for name, (_, rows) in tables.items()},
        # Records actually written, not distinct labels: if the two ever differ the
        # duplicate-label warning above says so, and this should still report the file.
        "alignment_records": sum(label_counts.values()),
        "alignment_width": widths[0] if len(widths) == 1 else None,
        "unmapped_countries": missing,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Checking a build against the release it supersedes
# ---------------------------------------------------------------------------

def _read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def diff_against_release(summary: Sequence[Dict[str, str]], reference_csv: Path
                         ) -> Dict[str, Any]:
    """Compare a regenerated Grand Lineage Summary against a previous release's.

    Every difference here is a derived value the rebuild has changed, so this is the
    report a curator signs off on before a release ships. It is deliberately per-column
    and per-lineage: "290 lineages differ" is not reviewable, "these 248 gained a region
    flag their records already supported" is.

    Lineages present in only one of the two are reported separately, because a release
    that adds or retires lineages is normal and should not be read as 5,000 changes.
    """
    reference = {row["LINEAGE_NAME"]: row for row in _read_csv(Path(reference_csv))}
    built = {row["LINEAGE_NAME"]: row for row in summary}

    derived_columns = [column for column in GRAND_LINEAGE_SUMMARY_COLUMNS
                       if column not in ("LINEAGE_NAME", "GENBANK_ACC", "SEQ_LENGTH",
                                         "GENUS_NAME", "SPECIES_NAME", "SEQUENCE")]

    changes: Dict[str, List[Dict[str, str]]] = {column: [] for column in derived_columns}
    for name in sorted(set(built) & set(reference)):
        for column in derived_columns:
            was = _text(reference[name].get(column))
            now = _text(built[name].get(column))
            if was != now:
                changes[column].append({"lineage": name, "was": was, "now": now})

    changed_lineages = {change["lineage"]
                        for column in changes for change in changes[column]}
    return {
        "reference": str(reference_csv),
        "lineages_compared": len(set(built) & set(reference)),
        "only_in_build": sorted(set(built) - set(reference)),
        "only_in_reference": sorted(set(reference) - set(built)),
        "changed_lineages": len(changed_lineages),
        "by_column": {column: {
            "changed": len(entries),
            # Capped because the whole point is a reviewable report; the counts above are
            # complete and the full list is reproducible by re-running the build.
            "examples": entries[:20],
        } for column, entries in changes.items() if entries},
    }


# build_from_repository() was removed on 2026-08-10. It built a release straight from
# data/records/ and wrote a release report WITHOUT consulting release_gate or the review
# ledger -- the exact ungated path release_gate was written to close, left in place as a
# public function with zero callers in source, tests or the RUNBOOK. Its report also had
# no `approval` block, so two report shapes could diverge.
#
# Build a release through curation/build_release.py, which is gated.
