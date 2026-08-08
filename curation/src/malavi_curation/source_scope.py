"""Decide whether an extracted row is the paper's OWN data or someone else's.

MalAvi records an association *under a reference*: this lineage was found in this
host, at this place, **and this paper is who reported it**. An extractor that
reads a table correctly can therefore still be wrong about the only thing MalAvi
files the row under -- because plenty of tables in the literature are not the
paper's own results at all.

Two shapes of this are in the ground-truth corpus, and between them they were the
single largest error class measured on 2026-07-30 (1,982 of 2,444 false-positive
associations, 81%):

* **A reprinted reference table.** Harl et al 2026's Additional file 2 is a list
  of haemosporidian mitochondrial genomes, 88 of whose 104 rows are *previously
  published* genomes the paper aligned against. Reading them is right; crediting
  them to Harl is not.
* **A pooled compilation.** Fecchio et al 2023b's Table S1 is 11,585 individual
  birds "gathered ... from [9, 18, 24, 29-31]" -- six earlier studies -- plus
  "962 unpublished samples". MalAvi credits the bulk of those rows to the six
  earlier references, so 1,890 correctly-read associations scored as false
  positives.

What this module does **not** do is guess. It classifies a row only when the
documents themselves say something about provenance, and everything else stays
``unknown``. There are exactly two kinds of evidence it will act on, and both are
statements the paper makes about itself:

1. **The paper's declared accessions** (row level). A data-availability statement
   such as Harl's --

       "The sequences generated for the present study were uploaded to NCBI
       GenBank under the accession numbers PV872084-PV872130 (cytb) and
       PV839571-PV839588 (mitochondrial genomes)."

   -- is an explicit, machine-readable list of what this study deposited. A table
   row carrying an accession inside that set is the paper's own; a row carrying
   an accession outside it is, by the paper's own account, someone else's.

2. **A pooled-compilation declaration** (paper level). Language like "we gathered
   ... from [refs]" or "previously published data" says the dataset is an
   aggregate. That is a statement about the whole dataset and not about any
   particular row, so it is recorded at the paper level and leaves rows
   ``scope_uncertain`` -- which is the honest answer, because Fecchio's Table S1
   carries no column that separates its 962 new samples from the 10,623 it
   compiled.

**Nothing here deletes a row.** Every outcome is a tag plus the sentence that
justifies it, because a wrong scope call is unrecoverable in a way a wrong tag is
not: a deleted correct record is invisible to the curator, whereas a mislabeled
one is one click away. This is the same reasoning that kept row-level host
validation out of the pipeline -- recall is the product, and scope is advice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .accession_mine import _prefix_ok, expand_accession_ranges

# --- Scope labels ------------------------------------------------------------

# What each label asserts, and what a curator should do about it. These are the
# only values ever written to a row's ``source_scope`` field.
SCOPES = {
    "focal": "the paper's own data: its accession is one the paper says it deposited",
    "reprinted": "someone else's data: its accession is outside the set the paper "
                 "says it deposited",
    "scope_uncertain": "the paper declares itself a pooled compilation of earlier "
                       "studies, and this row carries nothing that says which "
                       "study it came from",
    "unknown": "the documents say nothing that bears on this row's provenance",
}

# Fields written onto a row by :func:`classify_rows`.
SCOPE_FIELDS = ("source_scope", "source_scope_evidence")


# --- Sentence splitting ------------------------------------------------------

# Abbreviations whose trailing period does not end a sentence. Without these a
# data-availability statement is routinely cut in half ("... Harl et al. 2026
# deposited ..."), which would separate a deposition cue from its accessions.
_ABBREVIATIONS = ("et al", "e.g", "i.e", "cf", "spp", "sp", "subsp", "var",
                  "Fig", "Figs", "Tab", "no", "No", "vs", "approx", "ca")

# A sentence ends at .!? followed by whitespace and something that starts a new
# sentence (a capital, a digit, or an opening bracket).
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[0-9])")


def sentences(text: str) -> List[str]:
    """Split ``text`` into sentences, keeping common abbreviations intact.

    Deliberately simple. The only thing this has to get right is not splitting a
    deposition statement away from the accessions it introduces, so abbreviations
    are protected by temporarily masking their periods and the rest is a plain
    terminator split.
    """
    if not text:
        return []
    masked = text
    for abbreviation in _ABBREVIATIONS:
        masked = masked.replace(abbreviation + ".", abbreviation + "\x00")
    parts = _SENTENCE_BREAK.split(masked)
    return [part.replace("\x00", ".").strip() for part in parts if part.strip()]


# --- Evidence 1: the paper's declared accessions -----------------------------

# A sentence only declares the paper's OWN deposits if it says something was
# deposited AND says it was this paper that did it. Requiring both is what keeps
# "we downloaded sequences from GenBank (AB250415, KY653770)" -- a sentence full
# of accessions that declares the opposite -- from being read as a declaration.
_DEPOSITION_CUES = (
    "deposit", "uploaded", "submitted to genbank", "submitted to the genbank",
    "submitted to ncbi", "released in genbank", "made available in genbank",
    "available in genbank", "available from genbank", "assigned the accession",
    "under the accession", "under accession", "accession numbers are",
)

# Markers that the sentence is talking about *this* paper's work.
_OWNERSHIP_CUES = (
    "this study", "the present study", "present study", "this paper", "this work",
    "the current study", "this article", "our study", "our sequences",
    "we generated", "we obtained", "we deposited", "we submitted", "we uploaded",
    "newly generated", "newly obtained", "newly sequenced", "new sequences",
    "generated in this", "generated for the", "obtained in this", "reported here",
    "sequences generated", "described here",
)

# Cues that the sentence is explicitly about OTHER people's sequences. Any of
# these disqualifies the sentence outright, however well it otherwise reads as a
# declaration -- "sequences were retrieved from GenBank (accession numbers ...)"
# is the exact inverse of what we are looking for.
_FOREIGN_CUES = (
    "retrieved from", "downloaded from", "obtained from genbank", "taken from",
    "previously published", "published elsewhere", "were mined", "sourced from",
    "extracted from genbank", "available in malavi", "from the malavi",
)


@dataclass
class DeclaredAccessions:
    """The accession set a paper says it deposited, and the sentences saying so."""

    accessions: Set[str] = field(default_factory=set)
    evidence: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        """True only when the paper actually declared something usable."""
        return bool(self.accessions)

    def as_dict(self) -> dict:
        return {"n_accessions": len(self.accessions),
                "accessions": sorted(self.accessions),
                "evidence": list(self.evidence)}


def normalize_accession(value: str) -> str:
    """Uppercase an accession and drop any version suffix.

    ``PV839571.1`` and ``pv839571`` are the same record. Comparing raw strings
    would make a declared range miss a versioned table cell.
    """
    return str(value).strip().upper().split(".")[0]


def split_accession_cell(value: Optional[str]) -> List[str]:
    """Every accession token in one table cell.

    A cell often holds more than one, because a row can span several deposits:
    Harl et al 2026 writes ``PV839574_PV839575`` for a genome assembled from two
    records. Splitting on non-alphanumerics and keeping the tokens with a real
    INSDC prefix reads both without inventing anything.
    """
    if not value:
        return []
    tokens = re.split(r"[^A-Za-z0-9.]+", str(value))
    found = []
    for token in tokens:
        accession = normalize_accession(token)
        # Same shape test the accession miner applies, so the two agree on what
        # counts as an accession.
        if re.fullmatch(r"[A-Z]{1,2}[0-9]{5,6}", accession) and _prefix_ok(accession):
            found.append(accession)
    return found


def parse_declared_accessions(text: str) -> DeclaredAccessions:
    """Accessions the paper states it deposited, expanded from ranges.

    Scans sentence by sentence and keeps only those that (a) say something was
    deposited, (b) attribute that deposition to this paper, and (c) are not
    explicitly about other people's sequences. Ranges are expanded with the
    accession miner's own expander, because papers report a block of new
    deposits as ``PV839571-PV839588`` and never write the interior numbers --
    which are exactly the ones a supplementary table cites.
    """
    declared = DeclaredAccessions()
    for sentence in sentences(text):
        lowered = sentence.lower()
        if any(cue in lowered for cue in _FOREIGN_CUES):
            continue
        if not any(cue in lowered for cue in _DEPOSITION_CUES):
            continue
        if not any(cue in lowered for cue in _OWNERSHIP_CUES):
            continue

        # Ranges first (they carry the interior accessions), then the literal
        # tokens, which also picks up the two endpoints.
        expanded, _ranges = expand_accession_ranges(sentence)
        found = {normalize_accession(a) for a in expanded}
        found.update(split_accession_cell(sentence))
        if not found:
            # A declaration with no accessions in it -- Fecchio et al 2023b's
            # "accession numbers can be found in the Supporting Information" --
            # states nothing this module can use.
            continue
        declared.accessions.update(found)
        declared.evidence.append(" ".join(sentence.split()))
    return declared


# --- Evidence 2: a pooled-compilation declaration ----------------------------

# Phrases by which a paper says its dataset is an aggregate of earlier work. Each
# needs to be specific enough that a passing mention of the literature does not
# trigger it, so every one of these is about the *dataset* being assembled.
_COMPILATION_CUES = (
    "previously published data", "previously published dataset",
    "published data were compiled", "we compiled data", "we compiled published",
    "data were compiled from", "dataset was compiled", "compiled from published",
    "we gathered geolocation", "we gathered data", "data were gathered from",
    "we collated", "were collated from", "we assembled a dataset",
    "combined with previously published", "along with previously published",
    "we combined data", "meta-analysis of published", "systematic review",
    "records were extracted from published", "obtained from published studies",
    "sourced from published", "aggregated from",
)

# A compilation sentence should also point at where the data came from: a
# citation bracket ([9, 18, 24, 29-31]), an author-year citation, or the word
# "studies"/"papers". This keeps a methods sentence about compiling *climate*
# layers from being read as a data-provenance claim.
_CITATION_MARKER = re.compile(
    r"\[\s*\d+\s*[,;\-‐-―]?[^\]]*\]"     # numeric citation bracket
    r"|\b[A-Z][a-z]+\s+et\s+al\.?\s*,?\s*\(?\d{4}"  # Author et al. 2019
    r"|\bstudies\b|\bpapers\b|\bpublications\b|\bliterature\b",
    re.IGNORECASE)


@dataclass
class PooledCompilation:
    """Whether the paper declares its dataset to be an aggregate of earlier work."""

    is_compilation: bool = False
    evidence: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"is_compilation": self.is_compilation, "evidence": list(self.evidence)}


def detect_pooled_compilation(text: str) -> PooledCompilation:
    """Detect a paper-level statement that the dataset pools earlier studies."""
    result = PooledCompilation()
    for sentence in sentences(text):
        lowered = sentence.lower()
        if not any(cue in lowered for cue in _COMPILATION_CUES):
            continue
        if not _CITATION_MARKER.search(sentence):
            continue
        result.is_compilation = True
        result.evidence.append(" ".join(sentence.split()))
    return result


# --- Putting the two together ------------------------------------------------

@dataclass
class SourceScope:
    """Everything the documents say about where this paper's rows came from."""

    declared: DeclaredAccessions = field(default_factory=DeclaredAccessions)
    compilation: PooledCompilation = field(default_factory=PooledCompilation)

    def as_dict(self) -> dict:
        return {"declared_accessions": self.declared.as_dict(),
                "pooled_compilation": self.compilation.as_dict()}


def assess(text: str) -> SourceScope:
    """Read both kinds of provenance evidence out of a paper's text surface."""
    return SourceScope(declared=parse_declared_accessions(text),
                       compilation=detect_pooled_compilation(text))


def classify_rows(rows: Sequence[Dict[str, Optional[str]]],
                  scope: SourceScope) -> Dict[str, int]:
    """Tag each row with its provenance. Mutates ``rows``. Returns a tally.

    The order of the tests is the order of evidence strength:

    1. **A declared accession decides the row outright.** The paper listed what it
       deposited; a row's accession is either in that list or it is not, and no
       paper-level statement overrides a row-level fact.
    2. **Otherwise a compilation declaration makes the row uncertain.** The paper
       says the dataset pools earlier studies and the row carries nothing that
       says which one, so the honest label is "a curator has to check this".
    3. **Otherwise the row is unknown** -- the ordinary case, and the one where
       the pipeline behaves exactly as it did before this module existed.

    Rows are never dropped and no existing field is overwritten.
    """
    tally: Dict[str, int] = {}

    for row in rows:
        label = "unknown"
        evidence = ""

        row_accessions = split_accession_cell(row.get("accession"))
        if scope.declared and row_accessions:
            # Any declared accession on the row makes it the paper's own: a row
            # citing both a new deposit and an old one is reporting its own
            # result against a reference sequence.
            if any(a in scope.declared.accessions for a in row_accessions):
                label = "focal"
                evidence = ("carries an accession the paper says it deposited; "
                            + (scope.declared.evidence[0] if scope.declared.evidence else ""))
            else:
                label = "reprinted"
                evidence = ("carries no accession from the set the paper says it "
                            "deposited; " + (scope.declared.evidence[0]
                                             if scope.declared.evidence else ""))
        elif scope.compilation.is_compilation:
            label = "scope_uncertain"
            evidence = (scope.compilation.evidence[0]
                        if scope.compilation.evidence else "")

        row["source_scope"] = label
        if evidence:
            row["source_scope_evidence"] = evidence.strip()
        tally[label] = tally.get(label, 0) + 1

    return tally


def scope_of(row: Dict[str, Optional[str]]) -> str:
    """The scope label on a row, defaulting to ``unknown`` for untagged rows."""
    return str(row.get("source_scope") or "unknown")


def focal_rows(rows: Iterable[Dict[str, Optional[str]]]) -> List[Dict[str, Optional[str]]]:
    """The rows that are not positively attributed to another paper.

    ``reprinted`` is the only label this drops. ``scope_uncertain`` is kept
    because it means "nobody can tell from the documents", and dropping it would
    discard Fecchio et al 2023b's 962 genuinely new samples along with the rest.
    """
    return [row for row in rows if scope_of(row) != "reprinted"]
