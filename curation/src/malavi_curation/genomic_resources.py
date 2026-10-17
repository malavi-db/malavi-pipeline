# @title Genomic resources: the sheet, the rows, and the cyt b check against GenBank
# @purpose Read a submission's GenomicResources sheet into store rows, and check that
#          the cytochrome b barcode inside a deposited genome or transcriptome is the
#          lineage the row names, by fetching the record from GenBank and registering
#          its barcode window against MalAvi's lineages.
# @why Curator meeting 2026-09-24, point 9: a table of mitochondrial genomes linked
#      to cyt b lineages, submittable by users; widened on 2026-09-25 to genomes and
#      transcriptomes with their papers and accessions. A genome filed under the wrong
#      lineage would mislead every phylogenetic use of the table, so the link is
#      checked against the sequence, not taken on trust.
# @input the GenomicResources sheet of a filled template
# @input NCBI nucleotide (Entrez efetch, FASTA) when the online check runs
# @output rows for data/records/genomic_resources.csv
# @program python
# @critical-var RESOURCE_TYPES
# @critical-var MIN_CYTB_LENGTH
"""Genomes, transcriptomes and other large sequence records tied to a cyt b lineage.

``RESOURCE_TYPES`` is the controlled vocabulary of the RESOURCE_TYPE column. A submitter
types one of these on the sheet (the template offers them as a dropdown); anything else
is reported by the screen and not written.

The online check (:func:`cytb_check`) fetches the record as FASTA with NCBI efetch,
finds the 479 bp barcode window anywhere in it on either strand
(:func:`sequence_check.locate`), and asks :func:`sequence_check.check_sequence` which
lineage it is. The row passes when the verdict is ``known_lineage`` and the lineage is
the one named; it is a finding when the barcode is a different lineage, and a separate,
weaker finding when the record holds no recognizable barcode at all (a nuclear genome
assembly or an RNA-seq record legitimately may not).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import normalize, reference_names, sequence_check, template_adapter

RESOURCE_TYPES = ("mitogenome", "mitogenome_partial", "nuclear_genome",
                  "apicoplast_genome", "transcriptome", "other")

# An INSDC nucleotide accession (KY653811, NC_012426, MT513721.1), a BioProject
# (PRJNA123456) or an SRA/ENA run or experiment (SRR1234567, ERX123456).
ACCESSION_RE = re.compile(
    r"^(?:[A-Z]{1,2}\d{5,6}(?:\.\d+)?|NC_\d{6}(?:\.\d+)?|[A-Z]{4}\d{8,10}(?:\.\d+)?"
    r"|PRJ[NED][A-Z]\d+|[SED]R[RXSP]\d{6,9}|GC[AF]_\d{9}(?:\.\d+)?)$")

# Records shorter than this cannot hold the barcode window; skipped by the cyt b check.
MIN_CYTB_LENGTH = 479


def _text(value: Any) -> str:
    return normalize.text(value) or "" if value is not None else ""


def resource_links(workbook) -> List[Dict[str, str]]:
    """Every row of the GenomicResources sheet, as typed, with where it came from."""
    links: List[Dict[str, str]] = []
    if template_adapter.SHEET_GENOMES not in workbook.sheetnames:
        return links
    header, body = template_adapter.sheet_rows(
        workbook[template_adapter.SHEET_GENOMES], "LINEAGE_NAME")
    for row_number, values in body:
        cell = lambda col: template_adapter.cell(header, values, col)   # noqa: E731
        name = normalize.lineage_name(cell("LINEAGE_NAME")) or ""
        accession = (cell("Accession") or "").strip()
        if not name and not accession:
            continue
        links.append({
            "lineage": name,
            "resource_type": (cell("ResourceType") or "").strip().lower().replace(" ", "_"),
            "accession": accession,
            "database": (cell("Database") or "").strip() or ("GenBank" if accession else ""),
            "length_bp": re.sub(r"[^\d]", "", cell("LengthBp") or ""),
            "description": _text(cell("Description")),
            "host_species": _text(cell("HostSpecies")),
            "reference": (cell("Reference") or "").strip(),
            "doi": (cell("DOI") or "").strip(),
            "comment": _text(cell("Comment")),
            "origin": f"{template_adapter.SHEET_GENOMES} row {row_number}",
        })
    return links


def rows_from_links(links: Iterable[Dict[str, str]], submission_id: str, release: str,
                    columns: Tuple[str, ...]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Sheet rows -> genomic_resources rows. Rows the screen would refuse are skipped
    with a note, so the store never receives an unknown type or a malformed accession."""
    rows: List[Dict[str, Any]] = []
    notes: List[str] = []
    seen = set()
    for link in links:
        problems = []
        if not link["lineage"]:
            problems.append("no lineage")
        if link["resource_type"] not in RESOURCE_TYPES:
            problems.append(f"resource type {link['resource_type']!r} not one of {', '.join(RESOURCE_TYPES)}")
        if not ACCESSION_RE.match(link["accession"].upper()):
            problems.append(f"accession {link['accession']!r} is not a recognizable accession")
        if problems:
            notes.append(f"{link['origin']}: not written ({'; '.join(problems)})")
            continue
        key = (link["lineage"], link["resource_type"], link["accession"].upper())
        if key in seen:
            continue
        seen.add(key)
        row = {column: "" for column in columns}
        row.update({
            "LINEAGE_NAME": link["lineage"], "RESOURCE_TYPE": link["resource_type"],
            "ACCESSION": link["accession"].upper(), "DATABASE": link["database"],
            "LENGTH_BP": link["length_bp"], "DESCRIPTION": link["description"],
            "HOST_SPECIES": link["host_species"],
            "REFERENCE_NAME": reference_names.published_form(link["reference"]) if link["reference"] else "",
            "DOI": link["doi"], "SOURCE": submission_id, "NOTES": link["comment"],
            "_source": submission_id, "_added": release,
        })
        rows.append(row)
    return rows, notes


# ---------------------------------------------------------------------------------------
# The online cyt b check
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CytbCheck:
    accession: str
    status: str                 # matches | other_lineage | no_barcode | not_found | unavailable
    length_bp: int = 0
    definition: str = ""
    lineage_found: str = ""     # the lineage the barcode is, when it is one
    comparable: int = 0
    distance: int = 0


def fetch_fasta(accessions: List[str], email: str = "", timeout: float = 60.0,
                chunk_size: int = 20) -> Optional[Dict[str, Tuple[str, str]]]:
    """accession (no version) -> (definition line, sequence), or None if NCBI could not
    be reached at all. Plain urllib, so the curation environment needs no BioPython."""
    found: Dict[str, Tuple[str, str]] = {}
    for start in range(0, len(accessions), chunk_size):
        chunk = accessions[start:start + chunk_size]
        params = {"db": "nuccore", "id": ",".join(chunk), "rettype": "fasta",
                  "retmode": "text", "tool": "malavi_rebuild_genomic_resources"}
        if email:
            params["email"] = email
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        header, seq = None, []
        for line in text.splitlines() + [">"]:
            if line.startswith(">"):
                if header is not None:
                    acc = header[1:].split()[0].split(".")[0]
                    found[acc] = (header[1:].strip(), "".join(seq).upper())
                header, seq = line, []
            else:
                seq.append(line.strip())
        if start + chunk_size < len(accessions):
            time.sleep(0.4)
    return found


def cytb_check(accession: str, lineage: str, record: Optional[Tuple[str, str]],
               ref: Any) -> CytbCheck:
    """Does the barcode inside ``record`` belong to ``lineage``? ``ref`` is the screen's
    :class:`sequence_check.Reference` (names may carry a genus prefix, P_/H_/L_)."""
    acc = accession.split(".")[0].upper()
    if record is None:
        return CytbCheck(acc, "not_found")
    definition, seq = record
    if len(seq) < MIN_CYTB_LENGTH:
        return CytbCheck(acc, "no_barcode", len(seq), definition)
    res = sequence_check.check_sequence(seq, ref, label=acc)
    if res.verdict in ("unplaceable", "empty"):
        return CytbCheck(acc, "no_barcode", len(seq), definition)
    if not res.nearest:
        return CytbCheck(acc, "no_barcode", len(seq), definition)
    best, dist, comp = res.nearest[0]
    found = normalize.lineage_name(re.sub(r"^[PHLN]_", "", best)) or best
    if res.verdict == "known_lineage" and found == normalize.lineage_name(lineage):
        return CytbCheck(acc, "matches", len(seq), definition, found, comp, dist)
    return CytbCheck(acc, "other_lineage", len(seq), definition, found, comp, dist)
