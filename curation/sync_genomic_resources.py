#!/usr/bin/env python3
# @title Sync the genomic_resources table from NCBI and ENA
# @purpose Query NCBI Assembly, GenBank nucleotide, SRA/ENA and TSA for every avian
#          haemosporidian nuclear genome, apicoplast genome, transcriptome and
#          mitochondrial genome; tie each record to a MalAvi lineage by the cytochrome b
#          barcode found inside it; fill REFERENCE_NAME, DOI and HOST_SPECIES for the
#          rows the table already holds from the records' own REFERENCE and /host
#          fields. Dry run by default: writes two side tables under results/ and
#          nothing to the store without --apply.
# @why TODO 12b (curator meeting 2026-09-24; Vincenzo 2026-10-14): the table held only
#      mitogenomes with patchy citations. The lineage link is taken from the sequence
#      or from a statement in the record itself, never from a name's resemblance: a
#      genome filed under the wrong lineage would mislead every phylogenetic use.
# @input data/records/lineages.csv (the reference the barcode is called against)
# @input data/records/genomic_resources.csv (the rows to fill)
# @input NCBI E-utilities (esearch, esummary, efetch on assembly, nuccore, biosample, bioproject, sra, pubmed)
# @input NCBI Datasets v2 (genome FASTA by assembly accession)
# @input ENA portal API (read_run, assembly) and ENA browser API (TSA set FASTA)
# @output results/GENOMIC_RESOURCES_sync_<date>.tsv (every candidate record and its call)
# @output results/GENOMIC_RESOURCES_fill_<date>.tsv (proposed fills for existing rows)
# @output results/genomic_sync_cache/ (every fetched response, so a re-run does not re-download)
# @output data/records/genomic_resources.csv (with --apply only)
# @program python3
# @critical-var FULL_GENOME_MIN_BP
# @critical-var MIN_COMPARABLE
# @critical-var APICOPLAST_SLEN
# @critical-var MT_SLEN
# @critical-var MT_TITLE
# @critical-var AVIAN_GENERA
# @critical-var PLASMODIUM_AVIAN_SPECIES
# @critical-var KMER_LENGTH
# @critical-flag sync_genomic_resources.py "" --apply
"""Usage: .venv/bin/python curation/sync_genomic_resources.py [--sources LIST] [--date D] [--apply]

Runs under the project ``.venv`` (plain urllib; no BioPython needed, the GenBank flat
file is parsed here). Every HTTP response is cached under ``results/genomic_sync_cache/``
keyed by the request, so a second run reads the cache and makes no request at all; delete
the cache to refresh.

**The rule for a row** is the seed's rule (``curation/seed_genomic_resources.py``): the
record's cytochrome b barcode, located anywhere in the record on either strand by the
submission screen's own code, must be identical to one MalAvi lineage over at least
MIN_COMPARABLE of the 479 positions with no mismatch. Only such a record is proposed as
``add``. A record whose barcode is one base from a lineage is a candidate for a new
lineage or a sequence correction (``curator call``). A record with no barcode at all (a
nuclear assembly without its mitochondrion, an apicoplast genome, an RNA-seq project)
cannot be tied by sequence: it is ``paper needed`` unless the record itself states the
lineage (a BioSample ``lineage`` attribute, a ``/strain`` qualifier that is a stored
lineage name), which is reported as ``record states: NAME`` and is still a curator's
call, never a store write. ``--apply`` writes the ``add`` rows and fills empty
REFERENCE_NAME / DOI / HOST_SPECIES cells of existing rows; it never overwrites a value.

**References.** The GenBank record's REFERENCE blocks: the first one that is not a
"Direct Submission" and whose JOURNAL is not "Unpublished" is the study. Its PUBMED id is
resolved through PubMed esummary to the DOI, the publication year and the author list;
without a PUBMED id the authors and the year in the JOURNAL line are used and the DOI
stays blank. REFERENCE_NAME follows MalAvi's citation keys: "Hellgren 2005",
"Baillie & Brunton 2011", "Pacheco et al 2018". Assemblies and read projects take their
publication from the BioProject record the same way.

NCBI asks for at most 3 requests per second without an API key and 10 with one; the key
is read from the environment variable NCBI_API_KEY (NCBI's own name for it, used by
EDirect and BioPython) and the contact email from config/project.yml
(``project.curator_email``, the convention ``gate.py`` uses). Neither is printed.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from malavi_curation import normalize, reference_names, sequence_check       # noqa: E402
from malavi_curation.config import load_config, repo_root                    # noqa: E402
from malavi_curation.release_store import (                                  # noqa: E402
    TABLES, assign_ids, read_high_water, read_store, store_dir, write_store,
)

# ---------------------------------------------------------------------------------------
# Knobs. Everything a curator might ask "why this number?" about lives here.
# ---------------------------------------------------------------------------------------

# A haemosporidian mitochondrial genome is about 6 kb; shorter records are the coding
# region only and the table calls them a partial mitogenome (the seed's rule).
FULL_GENOME_MIN_BP = 5500

# Positions of the 479 bp window a record must determine for an exact match to count
# (the seed's floor; the template asks submitters for 470).
MIN_COMPARABLE = 470

# The mitochondrial genome query is malaviTree's NCBI monitor, scripts/05_ncbi_scan.sh
# with config/config.sh of /mnt/ellisbiostore/malaviTree, copied verbatim so this sync
# and that scan see the same records: the length window excludes the 479 bp barcodes
# and nuclear contigs, the title filter catches both "complete genome" deposits and the
# three-gene cox3/cox1/cytb deposits.
MT_SLEN = "5000:7000"
MT_TITLE = ("(mitochondrion[Title] OR mitochondrial[Title] OR "
            "(cytochrome b[Title] AND cytochrome c oxidase[Title]))")
# Genera scanned in full (the scan's NCBI_SCAN_GENERA, plus Fallisia, which the
# curators named as an avian genus; it had no records at NCBI on 2026-10-16).
AVIAN_GENERA = ("Haemoproteus", "Leucocytozoon", "Parahaemoproteus", "Fallisia")
# Plasmodium is dominated by human, primate and rodent species, so it is restricted
# to the avian species and subgenera the scan lists (PLASMODIUM_AVIAN_SPECIES there).
PLASMODIUM_AVIAN_SPECIES = (
    "relictum", "gallinaceum", "juxtanucleare", "elongatum", "ashfordi", "lutzi",
    "cathemerium", "homocircumflexum", "homopolare", "collidatum", "tejerai",
    "matutinum", "nucleophilum", "unalis", "octamerium", "huffi", "vaughani", "rouxi",
    "polare", "parahexamerium", "bertii", "formosanum", "gaboldoni", "jiangi")

# Apicoplast genomes are 28 to 35 kb in Haemosporida; the length window keeps the
# single-gene apicoplast deposits (clpc, tufA) out. Our own query; no scan has one.
APICOPLAST_SLEN = "20000:60000"
APICOPLAST_TITLE = "apicoplast[Title]"

# The read-archive and assembly listings are taken for the whole order (ENA
# tax_tree(5819) = Haemosporida, NCBI Haemosporida[Organism]) and then reduced to avian
# taxa by name: a genus in AVIAN_GENERA, or Plasmodium with an epithet in
# PLASMODIUM_AVIAN_SPECIES. An organism that is neither is counted in the console
# summary as dropped, with the most frequent names, so an avian species missing from
# the list is visible there rather than silently lost.
HAEMOSPORIDA_TAXID = "5819"

# The k-mer prefilter that finds where in a 20 Mb assembly or a 17,000-contig
# transcriptome the barcode might sit before the (slower) registration is run on a
# window around it. Every k-mer of every stored lineage, both strands, is indexed; a
# k-mer dominated by one base (AT-rich nuclear sequence is full of them) is left out
# because it matches everywhere and means nothing. The registration decides; the
# prefilter only chooses where to look, so a dropped k-mer can only cost a lookup
# that another k-mer of the same barcode makes anyway.
KMER_LENGTH = 24
# A k-mer is informative when it uses at least three of the four bases and no single
# base makes up more than half of it (the first H. majoris scan, 2026-10-16, showed
# that a 60 % ceiling still let AT runs of nuclear sequence through).
KMER_MAX_SINGLE_BASE_FRACTION = 0.5
KMER_MIN_DISTINCT_BASES = 3
WINDOW_FLANK_BP = 1500
SHORT_SEQUENCE_MAX_BP = 10000

# Resource types the table accepts (schemas/genomic_resources.schema.json).
TYPE_MITO, TYPE_MITO_PARTIAL = "mitogenome", "mitogenome_partial"
TYPE_NUCLEAR, TYPE_APICOPLAST, TYPE_TRANSCRIPTOME = "nuclear_genome", "apicoplast_genome", "transcriptome"

# Proposed actions in the side table.
ADD, PAPER_NEEDED, CURATOR_CALL, ALREADY = "add", "paper needed", "curator call", "already in table"

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
DATASETS = "https://api.ncbi.nlm.nih.gov/datasets/v2/genome/accession/"
ENA_PORTAL = "https://www.ebi.ac.uk/ena/portal/api/search"
ENA_FASTA = "https://www.ebi.ac.uk/ena/browser/api/fasta/"
TOOL_NAME = "malavi_rebuild_genomic_sync"
SOURCES = ("mitogenome", "apicoplast", "assembly", "transcriptome")


# ---------------------------------------------------------------------------------------
# The HTTP client: one place that knows about the rate limit, the cache and the key.
# ---------------------------------------------------------------------------------------

class Client:
    """GET with a per-request pause and a file cache. Tests replace :meth:`fetch`."""

    def __init__(self, cache_dir: Path, email: str = "", api_key: str = "",
                 timeout: float = 300.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.email = email
        self.api_key = api_key
        self.timeout = timeout
        # NCBI: 3 requests per second without a key, 10 with. A little slower than the
        # limit on purpose; a 429 would cost more than the margin.
        self.pause = 0.12 if api_key else 0.36
        self._last = 0.0
        self.requests_made = 0
        self.cache_hits = 0

    # -- the one network call ------------------------------------------------------
    # NCBI answers 429 (too many requests) now and then even inside its stated rate,
    # for instance when other jobs on the same cluster address are querying too
    # (seen on the first run, 2026-10-16). A 429 or a 5xx is retried after a growing
    # pause; anything else, and the last failure, is raised to the caller.
    RETRY_PAUSES = (2.0, 5.0, 15.0, 30.0, 60.0)

    def fetch(self, url: str) -> bytes:
        for attempt, retry_pause in enumerate(self.RETRY_PAUSES + (None,)):
            wait = self.pause - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests_made += 1
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or retry_pause is None:
                    raise
                time.sleep(retry_pause)
        raise RuntimeError("unreachable")  # the loop returns or raises

    def get(self, url: str, params: Optional[Dict[str, str]] = None, label: str = "") -> bytes:
        """Cached GET. The cache key is the URL without the email and key, so a run
        with a key reuses what a run without one fetched."""
        public = dict(params or {})
        full = dict(public)
        if url.startswith(EUTILS):
            full["tool"] = TOOL_NAME
            if self.email:
                full["email"] = self.email
            if self.api_key:
                full["api_key"] = self.api_key
        key_url = url + ("?" + urllib.parse.urlencode(sorted(public.items())) if public else "")
        digest = hashlib.sha1(key_url.encode("utf-8")).hexdigest()
        path = self.cache_dir / f"{digest}.bin"
        meta = self.cache_dir / f"{digest}.url"
        if path.exists():
            self.cache_hits += 1
            return path.read_bytes()
        request_url = url + ("?" + urllib.parse.urlencode(full) if full else "")
        data = self.fetch(request_url)
        path.write_bytes(data)
        meta.write_text(key_url + "\n", encoding="utf-8")   # what was asked, without the key
        return data

    def text(self, url: str, params: Optional[Dict[str, str]] = None) -> str:
        return self.get(url, params).decode("utf-8", "replace")

    def json(self, url: str, params: Optional[Dict[str, str]] = None) -> Any:
        return json.loads(self.text(url, params))

    # -- E-utilities ---------------------------------------------------------------
    def esearch(self, db: str, term: str, retmax: int = 5000) -> List[str]:
        payload = self.json(EUTILS + "esearch.fcgi",
                            {"db": db, "term": term, "retmode": "json", "retmax": str(retmax)})
        return list(payload.get("esearchresult", {}).get("idlist", []))

    def esummary(self, db: str, ids: Sequence[str], chunk: int = 100) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        ids = [i for i in ids if i]
        for start in range(0, len(ids), chunk):
            payload = self.json(EUTILS + "esummary.fcgi",
                                {"db": db, "id": ",".join(ids[start:start + chunk]), "retmode": "json"})
            result = payload.get("result", {})
            out.extend(result[uid] for uid in result.get("uids", []) if uid in result)
        return out

    def efetch(self, db: str, ids: Sequence[str], rettype: str = "", retmode: str = "text",
               chunk: int = 50) -> str:
        parts = []
        ids = [i for i in ids if i]
        for start in range(0, len(ids), chunk):
            params = {"db": db, "id": ",".join(ids[start:start + chunk]), "retmode": retmode}
            if rettype:
                params["rettype"] = rettype
            parts.append(self.text(EUTILS + "efetch.fcgi", params))
        return "\n".join(parts)


# ---------------------------------------------------------------------------------------
# GenBank flat-file parsing: the record's own statements about itself.
# ---------------------------------------------------------------------------------------

@dataclass
class GbReference:
    authors: str = ""
    title: str = ""
    journal: str = ""
    pubmed: str = ""

    @property
    def is_published(self) -> bool:
        """A journal reference, not the deposit itself and not an unpublished note."""
        journal = self.journal.strip()
        if self.title.strip().lower() == "direct submission":
            return False
        if not journal or journal.lower().startswith(("unpublished", "submitted", "published only in database")):
            return False
        return True


@dataclass
class GbRecord:
    accession: str = ""
    version: str = ""
    length: int = 0
    definition: str = ""
    organism: str = ""
    lineage_text: str = ""       # the taxonomy lines under ORGANISM
    organelle: str = ""
    host: str = ""
    strain: str = ""
    isolate: str = ""
    keywords: str = ""
    comment: str = ""
    dblink: Dict[str, str] = field(default_factory=dict)
    references: List[GbReference] = field(default_factory=list)
    sequence: str = ""

    @property
    def published_reference(self) -> Optional[GbReference]:
        for ref in self.references:
            if ref.is_published:
                return ref
        return None


def _qualifier(features: str, name: str) -> str:
    """The first ``/name="value"`` in the source feature (values can wrap lines)."""
    match = re.search(r'/' + re.escape(name) + r'="([^"]*)"', features, re.S)
    if not match:
        match = re.search(r'/' + re.escape(name) + r'=([^\s"]+)', features)
        return match.group(1).strip() if match else ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def parse_genbank(text: str) -> List[GbRecord]:
    """Every record in a GenBank flat-file text (several when efetch was given several ids)."""
    records: List[GbRecord] = []
    for chunk in re.split(r"^//\s*$", text, flags=re.M):
        if "LOCUS" not in chunk:
            continue
        rec = GbRecord()
        # Top-level fields start in column 1; continuation lines are indented.
        fields: List[Tuple[str, str]] = []
        for line in chunk.splitlines():
            if not line.strip():
                continue
            if line[:1] != " " and line[:12].strip():
                fields.append((line[:12].strip(), line[12:]))
            elif fields:
                key, value = fields[-1]
                fields[-1] = (key, value + "\n" + line)
        features = ""
        origin = ""
        for key, value in fields:
            if key == "LOCUS":
                rec.accession = value.split()[0].split(".")[0]
                match = re.search(r"(\d+)\s+(?:bp|rc|aa)", value)
                rec.length = int(match.group(1)) if match else 0
            elif key == "DEFINITION":
                rec.definition = re.sub(r"\s+", " ", value).strip().rstrip(".")
            elif key == "VERSION":
                rec.version = value.split()[0] if value.split() else ""
            elif key == "KEYWORDS":
                rec.keywords = re.sub(r"\s+", " ", value).strip().rstrip(".")
            elif key == "DBLINK":
                for line in value.splitlines():
                    if ":" in line:
                        name, _, acc = line.strip().partition(":")
                        rec.dblink[name.strip()] = acc.strip()
            elif key == "SOURCE":
                lines = value.splitlines()
                rec.organism = ""
                for line in lines[1:]:
                    if line.strip().startswith("ORGANISM"):
                        rec.organism = line.split("ORGANISM", 1)[1].strip()
                    elif rec.organism:
                        rec.lineage_text += " " + line.strip()
                rec.lineage_text = rec.lineage_text.strip()
            elif key == "COMMENT":
                rec.comment = re.sub(r"\s+", " ", value).strip()
            elif key == "REFERENCE":
                # Sub-fields (AUTHORS, TITLE, JOURNAL, PUBMED, REMARK ...) start in
                # column 3; their continuation lines are indented further.
                ref = GbReference()
                current = ""
                for line in value.splitlines()[1:]:
                    head = line[:12].strip()
                    body = line[12:].strip()
                    if head:
                        current = head
                        if head in ("AUTHORS", "TITLE", "JOURNAL", "PUBMED"):
                            setattr(ref, head.lower(), body)
                    elif current in ("AUTHORS", "TITLE", "JOURNAL"):
                        setattr(ref, current.lower(), (getattr(ref, current.lower()) + " " + body).strip())
                ref.pubmed = ref.pubmed.strip()
                rec.references.append(ref)
            elif key == "FEATURES":
                features = value
            elif key == "ORIGIN":
                origin = value
        if features:
            source = features.split("     source", 1)[1] if "     source" in features else features
            # Only the source feature's qualifiers; the next feature starts at column 6.
            source = re.split(r"\n     \S", source, maxsplit=1)[0]
            rec.organelle = _qualifier(source, "organelle")
            rec.host = _qualifier(source, "host")
            rec.strain = _qualifier(source, "strain")
            rec.isolate = _qualifier(source, "isolate")
        if origin:
            rec.sequence = re.sub(r"[^A-Za-z]", "", origin).upper()
        records.append(rec)
    return records


# ---------------------------------------------------------------------------------------
# Citation keys and DOIs
# ---------------------------------------------------------------------------------------

def _surnames_from_genbank(authors: str) -> List[str]:
    """``Harl,J., Himmel,T. and Weissenboeck,H.`` -> ``['Harl', 'Himmel', 'Weissenboeck']``."""
    text = re.sub(r"\s+", " ", authors or "").strip()
    if not text:
        return []
    parts = re.split(r",\s(?=[^,]*,)|\sand\s", text)
    names = []
    for part in parts:
        part = part.strip().rstrip(",")
        if not part:
            continue
        names.append(part.split(",")[0].strip())
    return names


def citation_key(surnames: Sequence[str], year: str) -> str:
    """MalAvi's citation key from a surname list and a year, or '' if either is missing."""
    if not surnames or not re.fullmatch(r"(19|20)\d\d", year or ""):
        return ""
    if len(surnames) == 1:
        key = f"{surnames[0]} {year}"
    elif len(surnames) == 2:
        key = f"{surnames[0]} & {surnames[1]} {year}"
    else:
        key = f"{surnames[0]} et al {year}"
    return reference_names.published_form(key)


@dataclass
class Study:
    reference_name: str = ""
    doi: str = ""
    pubmed: str = ""
    origin: str = ""     # where the name came from, for the side table


def pubmed_studies(client: Client, pmids: Sequence[str]) -> Dict[str, Study]:
    """PubMed id -> Study, from PubMed esummary (DOI, year, authors)."""
    out: Dict[str, Study] = {}
    pmids = sorted({p for p in pmids if re.fullmatch(r"\d+", p or "")})
    for summary in client.esummary("pubmed", pmids):
        pmid = str(summary.get("uid", ""))
        doi = ""
        for article_id in summary.get("articleids", []):
            if article_id.get("idtype") == "doi":
                doi = article_id.get("value", "").strip()
        if not doi:
            match = re.search(r"doi:\s*(10\.\S+)", summary.get("elocationid", ""))
            doi = match.group(1) if match else ""
        year = (summary.get("pubdate", "") or "")[:4]
        surnames = [a.get("name", "").rsplit(" ", 1)[0] for a in summary.get("authors", [])
                    if a.get("authtype", "Author") == "Author"]
        out[pmid] = Study(citation_key(surnames, year), doi, pmid, f"PubMed {pmid}")
    return out


def study_from_genbank(rec: GbRecord, pubmed: Dict[str, Study]) -> Study:
    """The study a GenBank record cites, as REFERENCE_NAME and DOI."""
    ref = rec.published_reference
    if ref is None:
        return Study(origin="GenBank record: no published reference")
    if ref.pubmed and ref.pubmed in pubmed and pubmed[ref.pubmed].reference_name:
        return pubmed[ref.pubmed]
    year_match = re.search(r"\((19|20)(\d\d)\)", ref.journal)
    year = year_match.group(1) + year_match.group(2) if year_match else ""
    name = citation_key(_surnames_from_genbank(ref.authors), year)
    return Study(name, "", ref.pubmed, "GenBank REFERENCE block (no PubMed id)" if name else
                 "GenBank REFERENCE block without a year")


def bioproject_studies(client: Client, accessions: Sequence[str]) -> Dict[str, Tuple[str, List[str]]]:
    """BioProject accession -> (title, PubMed ids it lists)."""
    out: Dict[str, Tuple[str, List[str]]] = {}
    for acc in sorted({a for a in accessions if a}):
        uids = client.esearch("bioproject", f"{acc}[Project Accession]")
        if not uids:
            out[acc] = ("", [])
            continue
        xml = client.efetch("bioproject", uids, retmode="xml")
        title_match = re.search(r"<Title>([^<]*)</Title>", xml)
        pmids = re.findall(r'<Publication[^>]*\bid="(\d+)"', xml)
        out[acc] = (title_match.group(1).strip() if title_match else "", pmids)
    return out


def biosample_attributes(client: Client, accessions: Sequence[str]) -> Dict[str, Dict[str, str]]:
    """BioSample accession -> its attributes (host, strain, isolate, lineage ...)."""
    out: Dict[str, Dict[str, str]] = {}
    wanted = sorted({a for a in accessions if a})
    if not wanted:
        return out
    uids: List[str] = []
    for acc in wanted:
        uids.extend(client.esearch("biosample", f"{acc}[Accession]"))
    xml = client.efetch("biosample", uids, retmode="xml")
    for block in re.split(r"(?=<BioSample )", xml):
        acc_match = re.search(r'<BioSample [^>]*accession="([^"]+)"', block)
        if not acc_match:
            continue
        attrs: Dict[str, str] = {}
        for name, harmonized, value in re.findall(
                r'<Attribute attribute_name="([^"]*)"(?:[^>]*harmonized_name="([^"]*)")?[^>]*>([^<]*)</Attribute>',
                block):
            value = value.strip()
            if value and value.lower() not in ("missing", "not applicable", "not collected", "na"):
                attrs[name.strip().lower()] = value
                if harmonized:
                    attrs.setdefault(harmonized.strip().lower(), value)
        title_match = re.search(r"<Title>([^<]*)</Title>", block)
        if title_match:
            attrs["_title"] = title_match.group(1).strip()
        out[acc_match.group(1)] = attrs
    return out


# ---------------------------------------------------------------------------------------
# The barcode check
# ---------------------------------------------------------------------------------------

def build_reference(store: Dict[str, List[Dict[str, str]]]) -> sequence_check.Reference:
    names = [row["LINEAGE_NAME"] for row in store["lineages"] if row.get("SEQUENCE")]
    seqs = [row["SEQUENCE"].upper() for row in store["lineages"] if row.get("SEQUENCE")]
    return sequence_check.Reference.from_sequences(names, seqs, where="data/records/lineages.csv")


def build_kmer_index(ref: sequence_check.Reference) -> Set[str]:
    """Every informative KMER_LENGTH-mer of every lineage, on both strands."""
    index: Set[str] = set()
    limit = KMER_MAX_SINGLE_BASE_FRACTION * KMER_LENGTH
    for seq in ref.seqs:
        for start in range(len(seq) - KMER_LENGTH + 1):
            kmer = seq[start:start + KMER_LENGTH]
            if (set(kmer) <= set("ACGT") and len(set(kmer)) >= KMER_MIN_DISTINCT_BASES
                    and max(Counter(kmer).values()) <= limit):
                index.add(kmer)
                index.add(sequence_check.reverse_complement(kmer))
    return index


def barcode_windows(sequence: str, index: Set[str]) -> List[str]:
    """Stretches of a long sequence worth registering: around every run of k-mer hits."""
    sequence = sequence.upper()
    # A record the size of a mitogenome is registered whole, as the seed did; the
    # prefilter is for sequences where a whole-record registration would be wasted.
    if len(sequence) <= SHORT_SEQUENCE_MAX_BP:
        return [sequence]
    hits = [i for i in range(len(sequence) - KMER_LENGTH + 1) if sequence[i:i + KMER_LENGTH] in index]
    windows: List[str] = []
    last_end = -1
    for pos in hits:
        if pos < last_end:
            continue
        start, end = max(0, pos - WINDOW_FLANK_BP), pos + WINDOW_FLANK_BP
        windows.append(sequence[start:end])
        last_end = end
    return windows


@dataclass
class BarcodeCall:
    verdict: str = "unplaceable"
    nearest: str = ""
    distance: int = -1
    comparable: int = 0
    exact: bool = False
    contig: str = ""
    caveat: str = ""         # why an identical barcode is still not an exact call

    @property
    def summary(self) -> str:
        if self.verdict in ("unplaceable", "empty", "no sequence"):
            return self.verdict
        text = f"{self.verdict}: {self.nearest} at {self.distance} over {self.comparable}"
        return text + (f"; {self.caveat}" if self.caveat else "")


def _plain(name: str) -> str:
    return normalize.lineage_name(re.sub(r"^[PHLN]_", "", name)) or name


def call_barcode(label: str, sequence: str, ref: sequence_check.Reference) -> BarcodeCall:
    """The screen's own call on one sequence, reduced to what the sync needs.

    ``exact`` (the only road to a row) needs one lineage identical over at least
    MIN_COMPARABLE positions, and no second reading of the same barcode: not another
    lineage also identical over the floor (a duplicate or a contained pair, the
    synonymy pass's business), and not a lineage that determines more positions and
    sits within one base (the P. relictum SGS1 reference genome, 2026-10-16: identical
    to the partial DONANA05 over 478, one base from the complete SGS1 over 479; the
    rank order would have filed it under DONANA05). Either case is a curator's call."""
    if not sequence:
        return BarcodeCall("no sequence")
    result = sequence_check.check_sequence(sequence, ref, label=label, top_n=10)
    if not result.nearest:
        return BarcodeCall(result.verdict)
    nearest, distance, comparable = result.nearest[0]
    nearest = _plain(nearest)
    call = BarcodeCall(result.verdict, nearest, distance, comparable)
    if not (result.verdict == "known_lineage" and distance == 0 and comparable >= MIN_COMPARABLE):
        return call
    identical = [(_plain(n), c) for n, d, c in result.nearest if d == 0 and c >= MIN_COMPARABLE]
    within_one = [(_plain(n), c) for n, d, c in result.nearest
                  if d == 1 and c > comparable]
    if len(identical) > 1:
        call.caveat = "also identical to " + ", ".join(f"{n} over {c}" for n, c in identical[1:])
    elif within_one:
        call.caveat = ("one base from " + ", ".join(f"{n} over {c}" for n, c in within_one)
                       + f", which determines more positions than {nearest}")
    else:
        call.exact = True
    return call


def call_contigs(contigs: Dict[str, str], ref: sequence_check.Reference,
                 index: Set[str]) -> BarcodeCall:
    """The best barcode call over many contigs (an assembly, a transcriptome set).

    An exact call wins; otherwise the placed call with the most comparable positions
    and the smallest distance; otherwise unplaceable."""
    best = BarcodeCall()
    for name, seq in contigs.items():
        for window in barcode_windows(seq, index):
            call = call_barcode(name, window, ref)
            call.contig = name
            if call.exact:
                return call
            if call.verdict not in ("unplaceable", "empty", "no sequence") and (
                    best.verdict in ("unplaceable",) or
                    (call.comparable, -call.distance) > (best.comparable, -best.distance)):
                best = call
    return best


def read_fasta_text(text: str) -> Dict[str, str]:
    out: Dict[str, List[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith(">"):
            current = line[1:].split()[0]
            out[current] = []
        elif current:
            out[current].append(line.strip())
    return {k: "".join(v).upper() for k, v in out.items()}


# ---------------------------------------------------------------------------------------
# Candidates: one per record, whatever the source
# ---------------------------------------------------------------------------------------

@dataclass
class Candidate:
    accession: str
    resource_type: str
    source: str                      # which query found it
    database: str = "GenBank"
    organism: str = ""
    length_bp: int = 0
    description: str = ""
    host: str = ""
    strain: str = ""
    isolate: str = ""
    bioproject: str = ""
    biosample: str = ""
    biosample_lineage: str = ""      # the BioSample's own "lineage" attribute, if any
    stated_lineage: str = ""         # a lineage the record itself states, with where
    study: Study = field(default_factory=Study)
    sequence: str = ""               # one sequence (nucleotide records)
    contigs: Dict[str, str] = field(default_factory=dict)   # many (assemblies, TSA)
    note: str = ""


def is_avian_taxon(organism: str) -> bool:
    """The name rule: an avian genus, or Plasmodium with an avian epithet."""
    words = (organism or "").replace("(", " ").split()
    if not words:
        return False
    if words[0] in AVIAN_GENERA:
        return True
    if words[0] == "Plasmodium":
        return any(w in PLASMODIUM_AVIAN_SPECIES for w in words[1:])
    return False


def stated_lineage(values: Iterable[Tuple[str, str]], known: Set[str]) -> str:
    """``record states: NAME (/strain)`` when a field of the record is a stored lineage
    name, exactly or behind one lower-case genus letter (Harl's ``lACCGEN04``).
    Resemblance is not a statement: ``SGS1-like`` or ``cf. SGS1`` say nothing here."""
    for where, value in values:
        value = (value or "").strip()
        if not value:
            continue
        plain = normalize.lineage_name(value) or ""
        if plain in known:
            return f"record states: {plain} ({where})"
        match = re.fullmatch(r"[phl]([A-Za-z0-9_]+)", value)
        if match and (normalize.lineage_name(match.group(1)) or "") in known:
            return f"record states: {normalize.lineage_name(match.group(1))} ({where}, behind the genus letter)"
    return ""


def resource_type_for(rec: GbRecord) -> str:
    """The table's type for a nucleotide record, from its organelle and length."""
    organelle = (rec.organelle or "").lower()
    if "apicoplast" in organelle or "plastid" in organelle:
        return TYPE_APICOPLAST
    if "transcribed" in rec.keywords.lower() or rec.keywords.upper().startswith("TSA"):
        return TYPE_TRANSCRIPTOME
    return TYPE_MITO if rec.length >= FULL_GENOME_MIN_BP else TYPE_MITO_PARTIAL


def mitogenome_query_terms() -> List[Tuple[str, str]]:
    """The malaviTree scan's queries, verbatim: (label, Entrez term)."""
    terms = [(genus, f"{genus}[Organism] AND {MT_SLEN}[SLEN] AND {MT_TITLE}") for genus in AVIAN_GENERA]
    cues = " OR ".join(PLASMODIUM_AVIAN_SPECIES)
    terms.append(("Plasmodium (avian cues)",
                  f"Plasmodium[Organism] AND {MT_SLEN}[SLEN] AND {MT_TITLE} AND ({cues})"))
    return terms


def apicoplast_query_terms() -> List[Tuple[str, str]]:
    terms = [(genus, f"{genus}[Organism] AND {APICOPLAST_TITLE} AND {APICOPLAST_SLEN}[SLEN]")
             for genus in AVIAN_GENERA]
    cues = " OR ".join(PLASMODIUM_AVIAN_SPECIES)
    terms.append(("Plasmodium (avian cues)",
                  f"Plasmodium[Organism] AND {APICOPLAST_TITLE} AND {APICOPLAST_SLEN}[SLEN] AND ({cues})"))
    return terms


def tsa_query_terms() -> List[Tuple[str, str]]:
    terms = [(genus, f"{genus}[Organism] AND tsa master[Properties]") for genus in AVIAN_GENERA]
    cues = " OR ".join(PLASMODIUM_AVIAN_SPECIES)
    terms.append(("Plasmodium (avian cues)",
                  f"Plasmodium[Organism] AND tsa master[Properties] AND ({cues})"))
    return terms


def sra_query_terms() -> List[Tuple[str, str]]:
    genera = " OR ".join(f"{g}[Organism]" for g in AVIAN_GENERA)
    cues = " OR ".join(PLASMODIUM_AVIAN_SPECIES)
    return [("SRA transcriptomic",
             f"({genera} OR (Plasmodium[Organism] AND ({cues}))) AND transcriptomic[Source]")]


def fetch_nucleotide_records(client: Client, accessions: Sequence[str]) -> Dict[str, GbRecord]:
    """accession (no version) -> parsed GenBank record, fetched in batches of 50."""
    out: Dict[str, GbRecord] = {}
    accessions = sorted({a.split(".")[0].upper() for a in accessions if a})
    if not accessions:
        return out
    text = client.efetch("nuccore", accessions, rettype="gbwithparts", retmode="text")
    for rec in parse_genbank(text):
        out[rec.accession.upper()] = rec
    return out


def nucleotide_candidates(client: Client, terms: List[Tuple[str, str]], source: str,
                          log: Callable[[str], None]) -> List[Candidate]:
    """Records of a nuccore query family, with their references and sequences."""
    uids: List[str] = []
    for label, term in terms:
        found = client.esearch("nuccore", term)
        log(f"  {source}: {label}: {len(found)} record(s)")
        uids.extend(found)
    uids = sorted(set(uids), key=int)
    summaries = client.esummary("nuccore", uids)
    accessions = [s.get("caption", "") for s in summaries]
    records = fetch_nucleotide_records(client, accessions)
    pubmed = pubmed_studies(client, [r.published_reference.pubmed for r in records.values()
                                     if r.published_reference and r.published_reference.pubmed])
    out: List[Candidate] = []
    for acc in sorted(records):
        rec = records[acc]
        out.append(Candidate(
            accession=acc, resource_type=resource_type_for(rec), source=source,
            organism=rec.organism, length_bp=rec.length or len(rec.sequence),
            description=rec.definition, host=rec.host, strain=rec.strain, isolate=rec.isolate,
            bioproject=rec.dblink.get("BioProject", ""), biosample=rec.dblink.get("BioSample", ""),
            study=study_from_genbank(rec, pubmed), sequence=rec.sequence,
            note=("RefSeq copy; " + rec.comment[:160]) if acc.startswith("NC_") else ""))
    return out


def assembly_candidates(client: Client, log: Callable[[str], None]) -> List[Candidate]:
    """Every avian Haemosporida assembly at NCBI Assembly (cross-checked against ENA),
    with its genome FASTA downloaded through NCBI Datasets for the barcode scan."""
    uids = client.esearch("assembly", "Haemosporida[Organism]")
    summaries = client.esummary("assembly", uids)
    dropped: Counter = Counter()
    kept: List[Dict[str, Any]] = []
    for s in summaries:
        if is_avian_taxon(s.get("speciesname", "")):
            kept.append(s)
        else:
            dropped[s.get("speciesname", "")] += 1
    log(f"  assembly: {len(summaries)} Haemosporida assemblies at NCBI; {len(kept)} avian by name; "
        f"dropped {sum(dropped.values())} ({', '.join(f'{k} {v}' for k, v in dropped.most_common(6))}...)")
    # ENA's own assembly listing for the order, as a cross-check: anything ENA lists
    # for an avian taxon that NCBI does not is reported, not silently missed.
    try:
        ena = client.text(ENA_PORTAL, {"result": "assembly", "query": f"tax_tree({HAEMOSPORIDA_TAXID})",
                                       "fields": "accession,scientific_name,study_accession,sample_accession",
                                       "format": "tsv", "limit": "0"})
        ena_rows = list(csv.DictReader(io.StringIO(ena), delimiter="\t"))
    except (urllib.error.URLError, OSError, ValueError):
        ena_rows = []
        log("  assembly: ENA assembly listing unavailable; NCBI only")
    ncbi_gca = {(s.get("synonym", {}) or {}).get("genbank", s.get("assemblyaccession", "")).split(".")[0]
                for s in kept}
    ena_avian = [r for r in ena_rows if is_avian_taxon(r.get("scientific_name", ""))]
    ena_only = [r for r in ena_avian if r.get("accession", "").split(".")[0] not in ncbi_gca]
    log(f"  assembly: ENA lists {len(ena_avian)} avian assemblies; {len(ena_only)} not at NCBI")

    out: List[Candidate] = []
    bioprojects = bioproject_studies(client, [
        (s.get("gb_bioprojects") or [{}])[0].get("bioprojectaccn", "") for s in kept])
    biosamples = biosample_attributes(client, [s.get("biosampleaccn", "") for s in kept])
    pubmed = pubmed_studies(client, [p for _, pmids in bioprojects.values() for p in pmids])
    for s in kept:
        gca = (s.get("synonym", {}) or {}).get("genbank", "") or s.get("assemblyaccession", "")
        bioproject = (s.get("gb_bioprojects") or [{}])[0].get("bioprojectaccn", "")
        biosample = s.get("biosampleaccn", "")
        attrs = biosamples.get(biosample, {})
        title, pmids = bioprojects.get(bioproject, ("", []))
        study = next((pubmed[p] for p in pmids if p in pubmed and pubmed[p].reference_name), Study())
        if not study.reference_name:
            study = Study(origin="BioProject lists no publication")
        contigs: Dict[str, str] = {}
        try:
            payload = client.get(DATASETS + f"{gca}/download",
                                 {"include_annotation_type": "GENOME_FASTA", "filename": f"{gca}.zip"})
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                for name in archive.namelist():
                    if name.endswith((".fna", ".fa", ".fasta")):
                        contigs.update(read_fasta_text(archive.read(name).decode("utf-8", "replace")))
        except (urllib.error.URLError, OSError, zipfile.BadZipFile) as exc:
            log(f"  assembly {gca}: genome download failed ({exc}); no barcode scan")
        host = attrs.get("host", "") or attrs.get("host scientific name", "")
        out.append(Candidate(
            accession=gca, resource_type=TYPE_NUCLEAR, source="NCBI Assembly",
            database="GenBank (Assembly)", organism=s.get("speciesname", ""),
            length_bp=sum(len(c) for c in contigs.values()),
            description=f"{s.get('speciesname', '')} assembly {s.get('assemblyname', '')}, "
                        f"{s.get('assemblystatus', '')}" + (f"; BioProject: {title}" if title else ""),
            host=host, strain=attrs.get("strain", ""), isolate=attrs.get("isolate", ""),
            bioproject=bioproject, biosample=biosample, biosample_lineage=attrs.get("lineage", ""),
            study=study, contigs=contigs,
            note=(f"{len(contigs)} sequence(s) scanned" if contigs else "genome not scanned")
                 + (f"; BioSample lineage attribute: {attrs['lineage']}" if attrs.get("lineage") else "")))
    for r in ena_only:
        out.append(Candidate(
            accession=r.get("accession", ""), resource_type=TYPE_NUCLEAR, source="ENA assembly (not at NCBI)",
            database="ENA", organism=r.get("scientific_name", ""),
            bioproject=r.get("study_accession", ""), biosample=r.get("sample_accession", ""),
            note="listed by ENA only; not scanned"))
    return out


def transcriptome_candidates(client: Client, log: Callable[[str], None]) -> List[Candidate]:
    """RNA-seq read projects (ENA read_run for the order, NCBI SRA for the named taxa,
    one candidate per BioProject) and TSA assemblies (scanned for the barcode)."""
    out: List[Candidate] = []
    projects: Dict[str, Dict[str, Any]] = {}
    # ENA: every transcriptomic run of the order, reduced to avian taxa by name.
    try:
        ena = client.text(ENA_PORTAL, {
            "result": "read_run",
            "query": f'tax_tree({HAEMOSPORIDA_TAXID}) AND library_source="TRANSCRIPTOMIC"',
            "fields": "study_accession,scientific_name,tax_id,sample_accession,study_title",
            "format": "tsv", "limit": "0"})
        ena_rows = list(csv.DictReader(io.StringIO(ena), delimiter="\t"))
    except (urllib.error.URLError, OSError, ValueError):
        ena_rows = []
        log("  transcriptome: ENA read_run listing unavailable")
    dropped: Counter = Counter()
    for r in ena_rows:
        organism = r.get("scientific_name", "")
        if not is_avian_taxon(organism):
            dropped[organism.split(";")[0]] += 1
            continue
        entry = projects.setdefault(r["study_accession"], {
            "organisms": set(), "samples": set(), "runs": 0, "title": r.get("study_title", ""),
            "sources": set()})
        entry["organisms"].add(organism)
        entry["samples"].add(r.get("sample_accession", ""))
        entry["runs"] += 1
        entry["sources"].add("ENA read_run")
    log(f"  transcriptome: ENA read_run: {len(ena_rows)} transcriptomic runs in Haemosporida; "
        f"{len(projects)} avian project(s); dropped organisms: "
        + ", ".join(f"{k} {v}" for k, v in dropped.most_common(5)) + " ...")
    # NCBI SRA for the named taxa, as a second listing.
    for label, term in sra_query_terms():
        uids = client.esearch("sra", term)
        log(f"  transcriptome: NCBI {label}: {len(uids)} run(s)")
        for s in client.esummary("sra", uids):
            xml = s.get("expxml", "")
            org = re.search(r'ScientificName="([^"]*)"', xml)
            prj = re.search(r"<Bioproject>([^<]*)</Bioproject>", xml)
            smp = re.search(r"<Biosample>([^<]*)</Biosample>", xml)
            title = re.search(r'<Study[^>]*name="([^"]*)"', xml)
            if not prj:
                continue
            entry = projects.setdefault(prj.group(1), {
                "organisms": set(), "samples": set(), "runs": 0,
                "title": title.group(1) if title else "", "sources": set()})
            if org:
                entry["organisms"].add(org.group(1))
            if smp:
                entry["samples"].add(smp.group(1))
            entry["sources"].add("NCBI SRA")
            if "ENA read_run" not in entry["sources"]:
                entry["runs"] += 1
    bioprojects = bioproject_studies(client, list(projects))
    samples = biosample_attributes(client, sorted({s for e in projects.values() for s in e["samples"]}))
    pubmed = pubmed_studies(client, [p for _, pmids in bioprojects.values() for p in pmids])
    known_lineages: Set[str] = set()
    for prj in sorted(projects):
        entry = projects[prj]
        title, pmids = bioprojects.get(prj, ("", []))
        study = next((pubmed[p] for p in pmids if p in pubmed and pubmed[p].reference_name), Study())
        if not study.reference_name:
            study = Study(origin="BioProject lists no publication")
        hosts = sorted({samples.get(s, {}).get("host", "") or samples.get(s, {}).get("host scientific name", "")
                        for s in entry["samples"]} - {""})
        lineages = sorted({samples.get(s, {}).get("lineage", "") for s in entry["samples"]} - {""})
        strains = sorted({samples.get(s, {}).get("strain", "") for s in entry["samples"]} - {""})
        out.append(Candidate(
            accession=prj, resource_type=TYPE_TRANSCRIPTOME, source=" + ".join(sorted(entry["sources"])),
            database="SRA/ENA", organism="; ".join(sorted(entry["organisms"])),
            description=f"RNA-seq, {entry['runs']} run(s), {len(entry['samples'])} sample(s)"
                        + (f": {title or entry['title']}" if (title or entry["title"]) else ""),
            host="; ".join(hosts), strain="; ".join(strains), bioproject=prj,
            biosample="; ".join(sorted(entry["samples"])[:5]) + (" ..." if len(entry["samples"]) > 5 else ""),
            # One stated lineage is a statement; several are a mixed project and no
            # single lineage can be attached, so only a lone value is passed on.
            biosample_lineage=lineages[0] if len(lineages) == 1 else "",
            study=study,
            note=(f"BioSample lineage attribute: {', '.join(lineages)}" if lineages else "")))
    # TSA assemblies: the master record names the set; ENA serves the contigs as one FASTA.
    for label, term in tsa_query_terms():
        uids = client.esearch("nuccore", term)
        log(f"  transcriptome: TSA masters {label}: {len(uids)}")
        for s in client.esummary("nuccore", uids):
            master = s.get("caption", "")
            records = fetch_nucleotide_records(client, [master])
            rec = records.get(master.upper(), GbRecord(accession=master))
            pm = pubmed_studies(client, [rec.published_reference.pubmed]
                                if rec.published_reference and rec.published_reference.pubmed else [])
            prefix = re.match(r"([A-Z]{4,6})0*$", master)
            set_name = (prefix.group(1) + "01") if prefix else ""
            contigs: Dict[str, str] = {}
            if set_name:
                try:
                    raw = client.get(ENA_FASTA + set_name, {"gzip": "true"})
                    text = gzip.decompress(raw).decode("utf-8", "replace") if raw[:2] == b"\x1f\x8b" \
                        else raw.decode("utf-8", "replace")
                    contigs = read_fasta_text(text)
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    log(f"  transcriptome: TSA set {set_name} download failed ({exc})")
            out.append(Candidate(
                accession=master, resource_type=TYPE_TRANSCRIPTOME, source="TSA master (nuccore)",
                database="GenBank (TSA)", organism=rec.organism or s.get("organism", ""),
                length_bp=0, description=rec.definition or s.get("title", ""),
                host=rec.host, strain=rec.strain, isolate=rec.isolate,
                bioproject=rec.dblink.get("BioProject", ""), biosample=rec.dblink.get("BioSample", ""),
                study=study_from_genbank(rec, pm), contigs=contigs,
                note=f"{len(contigs)} contig(s) scanned from ENA set {set_name}" if contigs else "contigs not scanned"))
    return out


def collect_candidates(client: Client, sources: Sequence[str], log: Callable[[str], None]) -> List[Candidate]:
    """Every candidate from every requested source. Tests replace this function."""
    out: List[Candidate] = []
    if "mitogenome" in sources:
        out.extend(nucleotide_candidates(client, mitogenome_query_terms(), "GenBank mitogenome scan", log))
    if "apicoplast" in sources:
        out.extend(nucleotide_candidates(client, apicoplast_query_terms(), "GenBank apicoplast query", log))
    if "assembly" in sources:
        out.extend(assembly_candidates(client, log))
    if "transcriptome" in sources:
        out.extend(transcriptome_candidates(client, log))
    return out


# ---------------------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------------------

def stated_name(statement: str) -> str:
    """``record states: SGS1 (/strain)`` -> ``SGS1``."""
    match = re.match(r"record states: (\S+)", statement or "")
    return match.group(1) if match else ""


def decide(candidate: Candidate, call: BarcodeCall, in_table: Set[str]) -> str:
    """The proposed action for one candidate. The only road to ``add`` is an exact
    barcode that the record's own statement, if it makes one, does not contradict."""
    if candidate.accession.split(".")[0].upper() in in_table:
        return ALREADY
    if call.exact:
        stated = stated_name(candidate.stated_lineage)
        if stated and stated != call.nearest:
            call.caveat = f"the record states {stated}, the barcode is {call.nearest}"
            call.exact = False
            return CURATOR_CALL
        return ADD
    if call.verdict in ("unplaceable", "empty", "no sequence"):
        return CURATOR_CALL if candidate.stated_lineage else PAPER_NEEDED
    return CURATOR_CALL


def make_row(candidate: Candidate, call: BarcodeCall, columns: Sequence[str], run_date: str) -> Dict[str, Any]:
    row = {column: "" for column in columns}
    notes = [f"cyt b barcode inside the record is {call.nearest} over {call.comparable} of 479 positions, "
             f"no mismatch (sync check" + (f", contig {call.contig}" if call.contig else "") + ")"]
    if candidate.note:
        notes.append(candidate.note)
    row.update({
        "LINEAGE_NAME": call.nearest,
        "RESOURCE_TYPE": candidate.resource_type,
        "ACCESSION": candidate.accession,
        "DATABASE": candidate.database,
        "LENGTH_BP": str(candidate.length_bp) if candidate.length_bp else "",
        "DESCRIPTION": candidate.description or candidate.organism,
        "HOST_SPECIES": candidate.host,
        "REFERENCE_NAME": candidate.study.reference_name,
        "DOI": candidate.study.doi,
        "SOURCE": f"sync {run_date}: {candidate.source}",
        "NOTES": "; ".join(notes),
        "_source": "sync",
        "_added": "",
    })
    return row


FILL_COLUMNS = ("REFERENCE_NAME", "DOI", "HOST_SPECIES")


def proposed_fills(rows: List[Dict[str, str]], records: Dict[str, GbRecord],
                   pubmed: Dict[str, Study]) -> List[Dict[str, str]]:
    """One line per (row, column) the sync has something to say about: ``fill`` when the
    cell is empty and the record states a value, ``conflict`` when both exist and differ
    (never applied), ``none`` when the record is silent. A DOI is filled next to an
    existing REFERENCE_NAME only when the record's own citation key is that same name:
    a DOI belongs to one study, and a different year or first author means a different
    study."""
    out: List[Dict[str, str]] = []
    for row in rows:
        acc = row.get("ACCESSION", "").split(".")[0].upper()
        rec = records.get(acc)
        base = {"RECORD_ID": row.get("RECORD_ID", ""), "ACCESSION": acc,
                "LINEAGE_NAME": row.get("LINEAGE_NAME", "")}
        if rec is None:
            out.append({**base, "column": "", "old_value": "", "new_value": "",
                        "origin": "record not fetched", "action": "none"})
            continue
        study = study_from_genbank(rec, pubmed)
        proposals = {"REFERENCE_NAME": (study.reference_name, study.origin),
                     "DOI": (study.doi, study.origin),
                     "HOST_SPECIES": (rec.host, "GenBank /host qualifier")}
        for column in FILL_COLUMNS:
            old = (row.get(column) or "").strip()
            new, origin = proposals[column]
            if column == "DOI" and new and old == "" and (row.get("REFERENCE_NAME") or "").strip() \
                    and (row.get("REFERENCE_NAME") or "").strip() != study.reference_name:
                out.append({**base, "column": column, "old_value": old, "new_value": new, "origin": origin,
                            "action": f"none: record cites {study.reference_name!r}, row cites "
                                      f"{row.get('REFERENCE_NAME')!r}"})
                continue
            if not new:
                action = "none"
            elif not old:
                action = "fill"
            elif old == new:
                action = "same"
            else:
                action = "conflict (kept)"
            out.append({**base, "column": column, "old_value": old, "new_value": new,
                        "origin": origin if new else "record silent", "action": action})
    return out


def apply_fills(rows: List[Dict[str, str]], fills: List[Dict[str, str]]) -> int:
    by_id = {r.get("RECORD_ID", ""): r for r in rows}
    count = 0
    for f in fills:
        if f["action"] == "fill" and f["RECORD_ID"] in by_id:
            by_id[f["RECORD_ID"]][f["column"]] = f["new_value"]
            count += 1
    return count


# ---------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------

def write_tsv(path: Path, rows: List[Dict[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


SIDE_FIELDS = ("accession", "resource_type", "source", "database", "organism", "length_bp",
               "barcode_call", "nearest_lineage", "distance", "comparable", "stated_lineage",
               "reference_name", "doi", "reference_origin", "host", "strain", "bioproject",
               "biosample", "proposed_action", "description", "note")


def main(argv=None, client: Optional[Client] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", default=",".join(SOURCES),
                        help=f"comma-separated subset of {', '.join(SOURCES)} (default: all)")
    parser.add_argument("--date", default=date.today().isoformat(), help="date stamped on the side tables")
    parser.add_argument("--apply", action="store_true",
                        help="write the add rows and the fills to data/records/genomic_resources.csv")
    parser.add_argument("--cache", type=Path, default=None, help="cache directory (default results/genomic_sync_cache)")
    parser.add_argument("--repo", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = args.repo or repo_root()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        print(f"unknown source(s): {', '.join(unknown)}; choose from {', '.join(SOURCES)}", file=sys.stderr)
        return 2
    store = read_store(store_dir(root))
    if not store.get("lineages"):
        print("the record store has no lineages to call barcodes against.", file=sys.stderr)
        return 1
    if client is None:
        try:
            email = load_config().get("project", {}).get("curator_email", "")
        except Exception:
            email = ""
        client = Client(args.cache or (root / "results" / "genomic_sync_cache"),
                        email=email, api_key=os.environ.get("NCBI_API_KEY", ""))

    log = print
    print("== malavi_rebuild :: sync genomic_resources ==")
    ref = build_reference(store)
    index = build_kmer_index(ref)
    known = set(ref.names)
    print(f"reference: {len(ref.names)} lineages x {ref.width} bp, from the record store; "
          f"{len(index)} informative {KMER_LENGTH}-mers")
    existing = store.get("genomic_resources", [])
    in_table = {r.get("ACCESSION", "").split(".")[0].upper() for r in existing}
    print(f"table: {len(existing)} row(s) covering {len({r.get('LINEAGE_NAME') for r in existing})} lineage(s)")

    # 1. Candidates from every source.
    print(f"sources: {', '.join(sources)}")
    pool = collect_candidates(client, sources, log)
    side: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    actions: Counter = Counter()
    by_source: Counter = Counter()
    for candidate in pool:
        # What the record says about its own lineage, if anything: a /strain or
        # /isolate qualifier or a BioSample "lineage" attribute that is a stored name.
        candidate.stated_lineage = stated_lineage(
            [("/strain", candidate.strain), ("/isolate", candidate.isolate),
             ("BioSample lineage attribute", candidate.biosample_lineage)], known)
        if candidate.sequence:
            call = call_barcode(candidate.accession, candidate.sequence, ref)
        elif candidate.contigs:
            call = call_contigs(candidate.contigs, ref, index)
        else:
            call = BarcodeCall("no sequence")
        action = decide(candidate, call, in_table)
        actions[action] += 1
        by_source[(candidate.source, action)] += 1
        side.append({
            "accession": candidate.accession, "resource_type": candidate.resource_type,
            "source": candidate.source, "database": candidate.database, "organism": candidate.organism,
            "length_bp": candidate.length_bp or "", "barcode_call": call.summary,
            "nearest_lineage": call.nearest, "distance": call.distance if call.distance >= 0 else "",
            "comparable": call.comparable or "", "stated_lineage": candidate.stated_lineage,
            "reference_name": candidate.study.reference_name, "doi": candidate.study.doi,
            "reference_origin": candidate.study.origin, "host": candidate.host, "strain": candidate.strain,
            "bioproject": candidate.bioproject, "biosample": candidate.biosample,
            "proposed_action": action, "description": candidate.description, "note": candidate.note,
        })
        if action == ADD:
            rows.append(make_row(candidate, call, TABLES["genomic_resources"].columns, args.date))
    side.sort(key=lambda r: (r["resource_type"], r["proposed_action"], r["accession"]))
    side_path = root / "results" / f"GENOMIC_RESOURCES_sync_{args.date}.tsv"
    write_tsv(side_path, side, SIDE_FIELDS)
    print(f"\n{len(pool)} candidate record(s): " + ", ".join(f"{k} {v}" for k, v in sorted(actions.items())))
    for (source, action), n in sorted(by_source.items()):
        print(f"  {source}: {action} {n}")
    print(f"every candidate and its call: {side_path}")

    # 2. Fills for the rows the table already holds.
    records = fetch_nucleotide_records(client, [r.get("ACCESSION", "") for r in existing
                                                if re.fullmatch(r"[A-Z]{1,2}_?\d{5,8}(\.\d+)?", r.get("ACCESSION", ""))])
    pubmed = pubmed_studies(client, [r.published_reference.pubmed for r in records.values()
                                     if r.published_reference and r.published_reference.pubmed])
    fills = proposed_fills(existing, records, pubmed)
    fill_path = root / "results" / f"GENOMIC_RESOURCES_fill_{args.date}.tsv"
    write_tsv(fill_path, fills, ("RECORD_ID", "ACCESSION", "LINEAGE_NAME", "column", "old_value",
                                 "new_value", "origin", "action"))
    fill_counts = Counter((f["column"], f["action"].split(":")[0].split(" ")[0]) for f in fills if f["column"])
    print(f"\nfills for the {len(existing)} existing row(s): "
          + ", ".join(f"{c} {a} {n}" for (c, a), n in sorted(fill_counts.items())))
    print(f"proposed fills: {fill_path}")
    print(f"network: {client.requests_made} request(s) made, {client.cache_hits} served from the cache "
          f"({client.cache_dir})")

    if not args.apply:
        print(f"\n[dry-run] nothing was written to the store. {len(rows)} row(s) would be added and "
              f"{sum(1 for f in fills if f['action'] == 'fill')} cell(s) filled. Re-run with --apply.")
        return 0
    filled = apply_fills(existing, fills)
    store["genomic_resources"] = assign_ids(
        TABLES["genomic_resources"], list(existing) + rows,
        floor=read_high_water(store_dir(root)).get("genomic_resources", 0))
    write_store(store_dir(root), store)
    print(f"\nwrote {len(rows)} new row(s) and {filled} fill(s) to {store_dir(root) / 'genomic_resources.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
