"""Deterministic sequence checks for submitted lineages.

Answers, for one submitted sequence, the three questions a curator needs before a
name can be assigned:

  1. **Is it registered to the MalAvi reading frame?** Submitted sequences are
     routinely offset. A primer-trimmed haem ASV is 478 bp spanning frame
     positions 2-479, so it needs one ``N`` at the 5' end; deposits are also
     seen starting at frame position 3. Pasted raw, such a sequence enters the
     alignment shifted, and every downstream comparison is then meaningless.
  2. **Is it actually new?** A sequence identical to a named lineage over the
     positions both cover must never be reported as new.
  3. **Does it translate cleanly?** Stop codons in the correct frame indicate a
     sequencing problem, a pseudogene, or a chimera.

Everything here is rule-based and offline: same input, same output, always. There
is no model, no heuristic scoring and no network call. The only external input is
the reference alignment for the pinned release.

Genetic code 4 (mold/protozoan mitochondrial) is used throughout. That is the
correct code for avian haemosporidians -- verified against 210 annotated
haemosporidian mitochondrial genomes, every one of which declares
``/transl_table=4`` (see export/validate_frame.py).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

BASES = ("A", "C", "G", "T")
GAPS = ("-", ".", "~")

# NCBI translation table 4 (mold/protozoan mitochondrial), built from the
# standard code with its single documented change: TGA = Trp rather than a stop.
# Everything else, including ATA = I and AGA/AGG = R, is as in the standard code.
#
# Verified codon-by-codon against Bio.Data.CodonTable id 4 (64/64 identical).
# Note this deliberately does NOT match malaviR's .qc_genetic_code_4(), which
# returns the table 5 (invertebrate mitochondrial) values M/S/S for ATA/AGA/AGG;
# that is a bug filed in malaviR's notes. It does not affect stop-codon counts,
# since none of those three codons is a stop in either table.
_CODE4 = {}
for _i, _b1 in enumerate("TCAG"):
    for _b2 in "TCAG":
        for _b3 in "TCAG":
            _CODE4[_b1 + _b2 + _b3] = "FFLLSSSSYY**CCWWLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"[
                len(_CODE4)]
_CODE4["TGA"] = "W"   # protozoan mitochondrial: Trp, not a stop

# How far the search slides a query along the reference when registering it.
# Real offsets seen in deposits are 0-2; 25 leaves generous headroom while
# keeping a mis-registration from silently matching somewhere absurd.
MAX_OFFSET = 25

# A registration is only trusted if it is clearly better than the alternatives.
# Random sequence sits near 75% mismatch; conserved cytb sits far below it.
MAX_ACCEPTABLE_MISMATCH = 0.35


def translate(seq: str) -> str:
    """Translate in frame 1 with genetic code 4. Unknown codons become 'X'."""
    seq = seq.upper()
    return "".join(_CODE4.get(seq[i:i + 3], "X") for i in range(0, len(seq) - len(seq) % 3, 3))


def count_stops(seq: str) -> int:
    return translate(seq).count("*")


def read_fasta(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    name = None
    chunks: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if name is not None:
                    out[name] = "".join(chunks).upper()
                name = line[1:].split()[0]
                chunks = []
            else:
                chunks.append(line)
    if name is not None:
        out[name] = "".join(chunks).upper()
    return out


# The shapes a correctly processed cytochrome b barcode actually arrives in, as
# (offset, length) where offset is 0-based within the 479 bp MalAvi window. Documented in
# reference/cytb_primer_frame_reference/ and confirmed against practice: Sanger reads
# position 1 as well, so a full-window 479 bp sequence is normal, while the primers
# themselves amplify only 478.
CANONICAL_SHAPES = {
    (0, 479): "Sanger (full window)",
    (1, 478): "haem",
    (1, 476): "leuc",
}


def _canonical_summary() -> str:
    """The canonical shapes, for a note a submitter can act on."""
    return "; ".join(
        f"{length} bp at position {offset + 1} = {assay}"
        for (offset, length), assay in sorted(CANONICAL_SHAPES.items()))


def _assay_of(offset: int, length: int) -> str:
    return CANONICAL_SHAPES.get((offset, length), "recognized")


@dataclass
class Reference:
    """The pinned release's alignment, plus the consensus used for registration."""

    names: List[str]
    seqs: List[str]
    width: int
    consensus: str

    @classmethod
    def from_fasta(cls, path: Path) -> "Reference":
        recs = read_fasta(Path(path))
        if not recs:
            raise ValueError(f"no sequences in {path}")
        names = list(recs)
        seqs = [recs[n] for n in names]
        width = len(seqs[0])
        if any(len(s) != width for s in seqs):
            raise ValueError("reference alignment is ragged; expected equal lengths")
        consensus = []
        for col in range(width):
            counts = Counter(s[col] for s in seqs if s[col] in BASES)
            consensus.append(counts.most_common(1)[0][0] if counts else "N")
        return cls(names, seqs, width, "".join(consensus))


@dataclass
class SequenceCheck:
    """Result of checking one submitted sequence."""

    label: str
    raw_length: int
    offset: Optional[int] = None            # query position 1 == frame position offset+1
    registered: Optional[str] = None        # the query placed in the reference frame
    mismatch_fraction: Optional[float] = None
    n_stop_codons: Optional[int] = None
    nearest: List[Tuple[str, int, int]] = field(default_factory=list)  # (name, dist, comparable)
    verdict: str = "unchecked"
    flags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {
            "label": self.label,
            "raw_length": self.raw_length,
            "offset": self.offset,
            "mismatch_fraction": (None if self.mismatch_fraction is None
                                  else round(self.mismatch_fraction, 5)),
            "n_stop_codons": self.n_stop_codons,
            "verdict": self.verdict,
            "flags": list(self.flags),
            "notes": list(self.notes),
            "nearest": [{"lineage": n, "distance": d_, "comparable": c}
                        for n, d_, c in self.nearest],
            # The query as placed in the 479 bp frame. Carried so the curator report can
            # show it beside its nearest relatives without re-registering it -- the
            # figure must use the same placement the distances above were computed from,
            # or it would illustrate a different comparison than the one reported.
            "registered": self.registered,
        }
        return d


def clean(seq: str) -> str:
    """Strip whitespace, FASTA headers and numbering from a pasted sequence."""
    seq = re.sub(r"^>.*$", "", seq or "", flags=re.MULTILINE)
    return re.sub(r"[^A-Za-z\-.~]", "", seq).upper()


def _register(query: str, ref: Reference,
              max_offset: int = MAX_OFFSET) -> Tuple[Optional[int], Optional[float]]:
    """Find the offset that places `query` position 1 at frame position offset+1.

    Slides the query against the reference consensus and returns the offset with
    the fewest mismatches over the positions both define. Returns (None, None) if
    nothing lands convincingly, rather than guessing.

    ``max_offset`` bounds the slide. The default suits a *submitted* barcode,
    which is at most a base or two out of frame. A sequence read from a paper's
    supplement can instead be a longer amplicon that contains the barcode window
    somewhere inside it (McNew et al 2021 deposits 818 bp for some samples), and
    a caller that expects those passes a wider bound -- see
    ``lineage_resolve.LineageResolver``.
    """
    best: List[Tuple[float, int]] = []
    for off in range(-max_offset, max_offset + 1):
        mm = tot = 0
        for i, ch in enumerate(query):
            j = i + off
            if 0 <= j < ref.width and ch in BASES and ref.consensus[j] in BASES:
                tot += 1
                mm += ch != ref.consensus[j]
        # Require substantial overlap so a short tail cannot win by luck.
        if tot >= min(200, max(1, len(query) // 2)):
            best.append((mm / tot, off))
    if not best:
        return None, None
    best.sort()
    frac, off = best[0]
    if frac > MAX_ACCEPTABLE_MISMATCH:
        return None, frac
    return off, frac


def _place(query: str, offset: int, width: int) -> str:
    """Pad/trim `query` so that it occupies the reference frame exactly."""
    if offset > 0:
        placed = "N" * offset + query
    else:
        placed = query[-offset:]
    if len(placed) < width:
        placed += "N" * (width - len(placed))
    return placed[:width]


def _distance(a: str, b: str) -> Tuple[int, int]:
    """Mismatches and comparable positions between two aligned sequences.

    Only positions where BOTH sequences carry an unambiguous base are compared,
    so a short or N-padded sequence is not penalised for what it does not cover.
    """
    mm = comparable = 0
    for x, y in zip(a, b):
        if x in BASES and y in BASES:
            comparable += 1
            mm += x != y
    return mm, comparable


def check_sequence(sequence: str, ref: Reference, label: str = "query",
                   top_n: int = 5) -> SequenceCheck:
    """Run every check on one submitted sequence."""
    q = clean(sequence)
    res = SequenceCheck(label=label, raw_length=len(q))
    if not q:
        res.verdict = "empty"
        res.flags.append("empty_sequence")
        return res

    stray = set(q) - set(BASES) - set(GAPS) - set("NRYSWKMBDHV")
    if stray:
        res.flags.append("non_iupac_characters")
        res.notes.append("unexpected characters: " + ", ".join(sorted(stray)))

    offset, frac = _register(q, ref)
    res.mismatch_fraction = frac
    if offset is None:
        res.verdict = "unplaceable"
        res.flags.append("could_not_register_to_reference_frame")
        res.notes.append(
            "No offset placed this sequence convincingly against the reference. "
            "It may not be a haemosporidian cytochrome b barcode, may be reverse "
            "complemented, or may carry indels.")
        return res

    res.offset = offset
    placed = _place(q, offset, ref.width)
    res.registered = placed
    res.n_stop_codons = count_stops(placed.replace("-", "N"))

    # Is this one of the shapes a correctly processed barcode actually arrives in?
    #
    # An earlier version flagged every non-zero offset as needing reframing, which meant
    # it told 100% of correctly trimmed haem amplicons that they were misframed -- the
    # commonest submission MalAvi receives. The three canonical shapes are documented in
    # reference/cytb_primer_frame_reference/ and confirmed by Vincenzo:
    #
    #   479 bp at frame position 1  Sanger. You read position 1 too, so the full window
    #                               is reported even though the primers amplify 478.
    #   478 bp at frame position 2  the haem amplicon (HaemF/HaemR2). Position 1 sits
    #                               under the primer and is not the template's base.
    #   476 bp at frame position 2  the leuc amplicon (HaemFL/HaemR2L), which also loses
    #                               positions 478-479 to the reverse primer.
    #
    # Anything else is genuinely displaced and worth a curator's attention. The
    # distinction matters: a real shift enters the alignment wrong and produces a
    # cascade of downstream QC warnings that look like biology and are not.
    canonical = (offset, len(q)) in CANONICAL_SHAPES
    if offset != 0 and not canonical:
        res.flags.append("needs_reframing")
        if offset > 0:
            res.notes.append(
                f"Sequence begins at frame position {offset + 1}, which is not one of "
                f"the shapes a correctly processed barcode arrives in "
                f"({_canonical_summary()}). Prepend {offset} N to register it as "
                f"submitted, but check the trimming first: as sent it would enter the "
                f"alignment shifted by {offset} base(s).")
        else:
            res.notes.append(
                f"Sequence starts {-offset} base(s) before the frame; trim "
                f"{-offset} from the 5' end.")
    elif canonical and offset != 0:
        # Said explicitly rather than left silent, so a curator can see the checker
        # recognized the assay rather than simply having nothing to say.
        res.flags.append("canonical_amplicon")
        res.notes.append(
            f"{len(q)} bp beginning at frame position {offset + 1}: the expected shape "
            f"of a correctly trimmed {_assay_of(offset, len(q))} amplicon. Position 1 "
            f"sits under the primer and is not the template's own base.")

    if len(q) != ref.width and not canonical:
        res.flags.append("length_differs_from_reference")
        res.notes.append(f"{len(q)} bp submitted against a {ref.width} bp reference frame.")
    if res.n_stop_codons:
        res.flags.append("contains_stop_codon")

    scored = []
    for name, rseq in zip(ref.names, ref.seqs):
        d, c = _distance(placed, rseq)
        if c:
            scored.append((d, -c, name))
    scored.sort()
    res.nearest = [(name, d, -negc) for d, negc, name in scored[:top_n]]

    if res.nearest:
        best_name, best_dist, best_comp = res.nearest[0]
        if best_dist == 0:
            # Identical over every position both cover. Never report as new.
            res.verdict = "known_lineage"
            res.flags.append("exact_match_to_known_lineage")
            res.notes.append(
                f"Identical to {best_name} over all {best_comp} comparable "
                f"positions. This is not a new lineage.")
        elif best_dist == 1:
            res.verdict = "new_candidate"
            res.flags.append("one_base_from_known_lineage")
            res.notes.append(
                f"Differs from {best_name} at a single position of {best_comp} "
                f"compared. New under the >=1 bp rule, but confirm the difference "
                f"is real and not a sequencing artifact.")
        else:
            res.verdict = "new_candidate"
    else:
        res.verdict = "unplaceable"

    return res


def check_many(sequences: Sequence[Tuple[str, str]], ref: Reference) -> List[SequenceCheck]:
    """Check an iterable of (label, sequence) pairs."""
    return [check_sequence(seq, ref, label=label) for label, seq in sequences]


def default_alignment_path(repo: Path, release: str) -> Optional[Path]:
    """Where export/build_downloads.R writes the alignment for a release."""
    p = repo / "docs" / "assets" / "downloads" / f"malavi_alignment_{release}.fasta"
    return p if p.is_file() else None
