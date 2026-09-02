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
        return cls.from_sequences(list(recs), [recs[n] for n in recs],
                                  where=str(path))

    @classmethod
    def from_sequences(cls, names: Sequence[str], seqs: Sequence[str],
                       where: str = "the given sequences") -> "Reference":
        """A reference built from sequences already in hand.

        The alignment on disk is one source; the record store is another, and it is the
        one ``store_ingest`` uses. Every lineage MalAvi holds is exactly 479 bp and sits
        at offset 0 by construction, so the store *is* a valid reference and using it
        avoids making ingest depend on an exported file that may not have been rebuilt.
        """
        seqs = [s for s in seqs]
        if not seqs:
            raise ValueError(f"no sequences in {where}")
        width = len(seqs[0])
        if any(len(s) != width for s in seqs):
            raise ValueError(f"{where} is ragged; expected equal lengths")
        consensus = []
        for col in range(width):
            counts = Counter(s[col] for s in seqs if s[col] in BASES)
            consensus.append(counts.most_common(1)[0][0] if counts else "N")
        return cls(list(names), seqs, width, "".join(consensus))


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
    # One of: "unchecked", "empty", "unplaceable", "known_lineage",
    # "exact_match_low_coverage" (identical over every shared position, but fewer than
    # MIN_COMPARABLE_TO_RANK of them -- see check_sequence) or "new_candidate".
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


# A reference sequence must cover at least this much of the query before its *rate* of
# mismatch is trusted for ranking. 92 of the 5,365 sequences in the 2026-03-23 alignment
# fall below it (1.7%); the median is 478. Same value, and the same reasoning, as
# lineage_resolve.MIN_COMPARABLE_TO_ASSIGN.
MIN_COMPARABLE_TO_RANK = 300


def _neighbor_rank(d: int, c: int) -> Tuple[int, int, float, int]:
    """Sort key for one candidate neighbor: lower is nearer.

    **Ranked by rate of mismatch, not by count.** Counting absolute mismatches lets a
    reference that overlaps the query in few positions win simply by having less to
    disagree over. NECMON01 (2026-08-20) was reported as nearest to ``P_RBQ18`` -- 23
    mismatches, but over only 133 comparable positions, 82.7% identity, and ``P_RBQ18`` is
    the *least* covered sequence in the whole alignment. Its true nearest relative,
    ``N_CIAE08``, was 38 mismatches over 477 positions: 92.0% identity, and the clade the
    lineage actually belongs to. The curator report named a *Plasmodium* as the closest
    relative of a *Haemoproteus*.

    Three terms, in order:

    1. **An exact match always wins**, whatever it covers. This is unchanged, and
       deliberately so: "never report a known lineage as new" is the rule this function
       exists to enforce, and it is checked against ``nearest[0]``. Making an exact match
       compete on rate would risk a lineage MalAvi already holds being announced as novel,
       which is the one outcome worse than a confusing neighbor list. Note that winning
       the *ranking* is not the same as earning the ``known_lineage`` *verdict*: the
       verdict additionally requires the overlap to reach ``MIN_COMPARABLE_TO_RANK``
       (see ``check_sequence``), so a thin exact match is still listed first, where a
       curator can see it, without being called the same lineage.
    2. **Thinly covered references sort after well covered ones**, so a 20-position overlap
       with one mismatch cannot outrank a full-length relative. They are ordered after
       rather than dropped: a submission that only overlaps short references still gets a
       neighbor list, and the ``comparable`` column shows why it is weak.
    3. **Then rate, then coverage, then name** -- the last for a deterministic order.
    """
    if d == 0:
        return (0, 0, 0.0, -c)
    return (1, 0 if c >= MIN_COMPARABLE_TO_RANK else 1, d / c, -c)


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
            scored.append((_neighbor_rank(d, c), name, d, c))
    scored.sort()
    res.nearest = [(name, d, c) for _rank, name, d, c in scored[:top_n]]

    if res.nearest:
        best_name, best_dist, best_comp = res.nearest[0]
        if best_dist == 0 and best_comp < MIN_COMPARABLE_TO_RANK:
            # Identical over every position both cover -- but they cover too few. A
            # partial barcode laid over a thinly covered reference can agree at 20
            # positions and disagree nowhere, and that used to be reported as
            # "identical over all 20 comparable positions": a known lineage, verdict
            # settled. Twenty positions cannot settle anything. The same floor that
            # decides whether a mismatch *rate* is trustworthy for ranking decides
            # here whether an exact match is trustworthy as an identity: below it the
            # answer is neither "known" nor "new" but "not enough sequence to say".
            # The match still heads the neighbor list, so it is not hidden; it is
            # simply not called the same lineage.
            res.verdict = "exact_match_low_coverage"
            res.flags.append("exact_match_over_too_few_positions")
            res.notes.append(
                f"Identical to {best_name} over the {best_comp} positions both cover, "
                f"but that overlap is too short to call it the same lineage: at least "
                f"{MIN_COMPARABLE_TO_RANK} comparable positions are needed. A longer "
                f"read covering more of the barcode is needed before this can be "
                f"called {best_name} or anything new.")
        elif best_dist == 0:
            # Identical over every position both cover, and enough of them to trust.
            # Never report as new.
            res.verdict = "known_lineage"
            res.flags.append("exact_match_to_known_lineage")
            res.notes.append(
                f"Identical to {best_name} over all {best_comp} comparable "
                f"positions. This is not a new lineage.")
        elif best_dist == 1:
            res.verdict = "new_candidate"
            res.flags.append("one_base_from_known_lineage")
            # Says the fact and stops. It used to add "New under the >=1 bp rule, but
            # confirm the difference is real and not a sequencing artifact", which was
            # dropped 2026-08-19: every curator knows the rule, and confirming that a
            # base is not an artifact is not something a curator can do from a report.
            # The specific notes -- which position, transversion or not, how rare the
            # base is, whether the codon changes -- are what actually help, and they
            # are already carried alongside this line.
            res.notes.append(
                f"Differs from {best_name} at a single position of {best_comp} "
                f"compared.")
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
