"""Mine NCBI sequence-accession tokens from free text.

This is the one piece of real, tested logic in the Phase 1 scaffold. The regex
patterns mirror the battle-tested ones in malaviTree's accession sweep
(scripts/03_mine_accessions.sh) so the two projects agree on what an accession
looks like.

The miner is recall-oriented: it favors catching candidate tokens (to show a
curator) over precision, so expect some false positives that a human prunes.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# -- Accession token patterns -------------------------------------------------
# Each pattern is anchored on word boundaries. Names match malaviTree's classes.
#
#   nucleotide : classic GenBank, 1-2 letters + 5-6 digits (e.g. AF069611, MN696030)
#   assembly   : assembly/RefSeq/WGS (GCA_/GCF_/NC_/4-letter WGS prefixes)
#   bioproject : PRJNA / PRJEB / PRJDB ...
#   sra        : SRA run/experiment/sample/study (SRR, ERX, DRP, ...)
PATTERNS: Dict[str, re.Pattern] = {
    "nucleotide": re.compile(r"\b[A-Z]{1,2}[0-9]{5,6}(?:\.[0-9]+)?\b"),
    "assembly": re.compile(r"\bGC[AF]_[0-9]+\.?[0-9]*\b|\bNC_[0-9]{6}\b|\b[A-Z]{4}[0-9]{8,}\b"),
    "bioproject": re.compile(r"\bPRJ[A-Z]{2}[0-9]+\b"),
    "sra": re.compile(r"\b[SED]R[RXPS][0-9]{5,}\b"),
}

# -- Valid INSDC (GenBank/EMBL/DDBJ) nucleotide accession prefixes -------------
# The nucleotide pattern above is shape-only, so it also matches DOI/journal IDs
# (S11686 from doi 10.1007/s11686-…), specimen/voucher codes (MB030997, GM103446),
# and elocation IDs (E01069). We therefore require a real, issued INSDC accession
# prefix. Direct-submission sequence prefixes follow a documented, mostly
# sequential scheme (https://www.ncbi.nlm.nih.gov/Sequin/acc.html); this set is
# the realistic universe of prefixes seen in haemosporidian cytb records, with
# headroom. Extend it when NCBI issues a new series (e.g. beyond PZ). RefSeq
# prefixes (NC_/NG_/NM_/NR_/…) are deliberately EXCLUDED here — they carry an
# underscore and are matched by the assembly pattern, so leaving them out also
# drops the bare "NC009336" duplicate of "NC_009336".
_VALID_SINGLE_LETTER = {"D", "J", "K", "L", "M", "U", "X", "Y", "Z"}  # GenBank/EMBL/DDBJ
_VALID_DOUBLE_LETTER = {
    # GenBank direct-submission series (roughly chronological)
    "AF", "AY", "DQ", "EF", "EU", "FJ", "GQ", "GU", "HM", "HQ", "JF", "JN", "JQ",
    "JX", "KC", "KF", "KJ", "KM", "KP", "KR", "KT", "KU", "KX", "KY", "MF", "MG",
    "MH", "MK", "MN", "MT", "MW", "MZ", "OK", "OL", "OM", "ON", "OP", "OQ", "OR",
    "PP", "PQ", "PV", "PZ",
    # GenBank contig/other + EMBL/ENA + DDBJ prefixes common in older records
    "AC", "AE", "AB", "AG", "AJ", "AK", "AL", "AM", "AN", "AP", "AT", "AU", "AV",
    "AX", "BA", "BN", "BR", "CR", "CT", "CU", "FM", "FN", "FO", "FP", "FQ", "FR",
    "HE", "HF", "HG", "LC", "LK", "LM", "LN", "LR", "LS", "LT", "OA", "OB", "OU",
    "OV", "OW", "OX", "OY", "OZ",
}
VALID_NUC_PREFIXES = _VALID_SINGLE_LETTER | _VALID_DOUBLE_LETTER

_PREFIX_RE = re.compile(r"^([A-Z]{1,2})[0-9]")


def _prefix_ok(token: str) -> bool:
    """True if ``token`` starts with a real INSDC nucleotide accession prefix."""
    m = _PREFIX_RE.match(token)
    return bool(m) and m.group(1) in VALID_NUC_PREFIXES


# URLs (which embed IDs shaped like accessions) are masked out before mining, the
# same way DOIs are (see DOI_PATTERN below and _mask_reference_noise).
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

# Accession RANGES, e.g. "PV948475-PV948494", "MN696030–45", "AF069611-AF069620".
# Papers almost always report a block of new sequences as a range and never write
# the interior accessions, yet MalAvi curates specific interior ones -- so we MUST
# expand ranges to recover them. The end endpoint may repeat the letter prefix or
# give only its (possibly abbreviated) trailing digits. Dash may be hyphen, en/em
# dash, or minus.
_DASH = r"[-‐‑‒–—−]"
RANGE_PATTERN = re.compile(
    r"\b([A-Z]{1,2})([0-9]{5,6})(?:\.[0-9]+)?"      # start: prefix + digits
    r"\s*" + _DASH + r"\s*"                           # dash
    r"(?:([A-Z]{1,2}))?([0-9]{2,6})(?:\.[0-9]+)?\b"  # end: optional prefix + digits
)

# Cap how many accessions a single range may expand to, so a malformed match
# (e.g. two unrelated numbers joined by a hyphen) cannot explode.
MAX_RANGE_SPAN = 500

# -- DOI pattern --------------------------------------------------------------
# A DOI is "10." + a registrant code (4-9 digits) + "/" + an opaque suffix. The
# suffix character set is broad; we stop at whitespace and trim trailing sentence
# punctuation afterwards. Case-insensitive: DOIs are case-insensitive, so we
# normalize matches to lower case.
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)


def _mask_reference_noise(text: str) -> str:
    """Blank out DOIs and URLs so their embedded, accession-shaped IDs are not
    mined as sequence accessions.

    Journal DOIs (``10.1007/s11686-026-01301-5``) and URLs carry tokens shaped
    exactly like accessions (``S11686``); replacing them with spaces before
    accession matching removes that whole false-positive class without touching
    real accessions, which never live inside a DOI/URL.
    """
    text = DOI_PATTERN.sub(" ", text)
    text = URL_PATTERN.sub(" ", text)
    return text


def mine_doi(text: str) -> Optional[str]:
    """Return the most likely article DOI found in ``text``, or None.

    A paper's own DOI is repeated in the running header/footer of every page, so
    it is typically the MOST FREQUENT DOI in the extracted text; DOIs of cited
    works appear once each in the reference list. We therefore return the modal
    DOI (ties broken by first appearance). Trailing sentence punctuation is
    stripped, and the result is lower-cased to match malaviR/MalAvi conventions.

    Recall-oriented like :func:`mine_accessions`: it surfaces a candidate for the
    curator to confirm, not an authoritative DOI.
    """
    if not text:
        return None
    candidates: List[str] = []
    for match in DOI_PATTERN.finditer(text):
        # Trim punctuation that commonly trails a DOI in running prose.
        doi = match.group(0).rstrip(".,;:)]}>").lower()
        if doi:
            candidates.append(doi)
    if not candidates:
        return None
    # Counter.most_common preserves first-seen order for equal counts (Python 3.7+),
    # so the earliest-appearing DOI wins a tie.
    return Counter(candidates).most_common(1)[0][0]


@dataclass
class AccessionHits:
    """Accession tokens found in a text, grouped by class (sorted, de-duplicated).

    ``nucleotide`` includes accessions recovered by expanding ranges; ``ranges``
    keeps the literal range expressions (e.g. "PV948475-PV948494") so a curator
    can see which accessions were inferred rather than written out verbatim.
    """

    nucleotide: List[str] = field(default_factory=list)
    assembly: List[str] = field(default_factory=list)
    bioproject: List[str] = field(default_factory=list)
    sra: List[str] = field(default_factory=list)
    ranges: List[str] = field(default_factory=list)

    def all(self) -> List[str]:
        """All tokens across every class, de-duplicated and sorted."""
        return sorted(set(self.nucleotide + self.assembly + self.bioproject + self.sra))

    def is_empty(self) -> bool:
        return not self.all()


def expand_accession_ranges(text: str) -> "tuple[List[str], List[str]]":
    """Find accession ranges and expand them to explicit accessions.

    Returns ``(accessions, range_labels)`` where ``accessions`` is the sorted set
    of all accessions implied by every range (endpoints included) and
    ``range_labels`` is the list of literal range strings matched.

    Endpoint handling: if the second endpoint omits the letter prefix or gives
    fewer digits than the start, its trailing digits are right-aligned onto the
    start number (so "PV948475-94" -> ...948494, "MN696030-45" -> ...696045).
    Ranges whose prefixes disagree, that run backward, or that exceed
    ``MAX_RANGE_SPAN`` are skipped (treated as not a real range).
    """
    accessions: set[str] = set()
    labels: List[str] = []

    for m in RANGE_PATTERN.finditer(text):
        start_prefix, start_digits, end_prefix, end_digits = m.groups()
        if end_prefix and end_prefix != start_prefix:
            continue  # "AF069611-MN696030" is two accessions, not a range

        width = len(start_digits)
        start_num = int(start_digits)
        # Right-align an abbreviated endpoint onto the start (PV948475-94 -> 948494).
        if len(end_digits) < width:
            end_full = start_digits[: width - len(end_digits)] + end_digits
        else:
            end_full = end_digits
        end_num = int(end_full)

        if end_num <= start_num or (end_num - start_num) > MAX_RANGE_SPAN:
            continue
        if start_prefix not in VALID_NUC_PREFIXES:
            continue  # e.g. "S12862-..." from a DOI/voucher block, not a real range

        labels.append(m.group(0).strip())
        for n in range(start_num, end_num + 1):
            accessions.add(f"{start_prefix}{n:0{width}d}")

    return sorted(accessions), labels


def mine_accessions(text: str) -> AccessionHits:
    """Extract accession tokens from ``text``.

    Returns an :class:`AccessionHits` with one sorted, de-duplicated list per
    class. Matching is case-sensitive (accessions are upper-case); callers that
    receive lower-cased OCR text should upper-case it first.
    """
    if not text:
        return AccessionHits()

    # Mask DOIs/URLs so their embedded, accession-shaped IDs are not mined, then
    # keep only nucleotide tokens with a real INSDC prefix (drops S11686 journal
    # IDs, MB030997/GM103446 voucher codes, E01069 elocation IDs, and bare NC###
    # RefSeq duplicates). See _mask_reference_noise / VALID_NUC_PREFIXES.
    text = _mask_reference_noise(text)

    hits = AccessionHits()
    nucleotide = {t for t in PATTERNS["nucleotide"].findall(text) if _prefix_ok(t)}
    hits.bioproject = sorted(set(PATTERNS["bioproject"].findall(text)))
    hits.sra = sorted(set(PATTERNS["sra"].findall(text)))
    # The assembly pattern has alternations; findall returns the whole match only
    # when there are no capture groups, so keep the groups out of the pattern.
    hits.assembly = sorted(set(PATTERNS["assembly"].findall(text)))

    # Expand accession ranges and fold the interior accessions into nucleotide.
    range_accessions, hits.ranges = expand_accession_ranges(text)
    nucleotide.update(range_accessions)
    hits.nucleotide = sorted(nucleotide)
    return hits
