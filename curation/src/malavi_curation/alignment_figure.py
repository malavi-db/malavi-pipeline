"""Show a submitted sequence beside its nearest relatives, at the positions that differ.

A curator deciding whether a one-base difference from a well-known lineage is real needs
to see the base. A distance of "1 of 478" does not tell them whether the change sits at a
third codon position among a dozen other lineages that also vary there, or alone at a site
nothing in MalAvi has ever varied at. Those are different decisions.

**No aligner is used, and that is deliberate.** MalAvi's release is a fixed 479 bp
alignment: every lineage is already mutually aligned, by construction, and
``sequence_check`` has already registered the query into that same window to compute the
distances printed elsewhere in the report. Running ClustalW or MAFFT here would add a
dependency and — worse — could produce a *different* alignment from the one the numbers
came from. A figure that quietly disagrees with the text above it is worse than no figure.

**Only the differing positions are shown.** A 479-column alignment is unreadable on a
page and mostly identical anyway; the informative part is the handful of columns where
anything varies. Identity is written as a dot, which is the convention every biologist
already reads.

Where a sequence could not be registered at all, this returns nothing. That inability is
itself the finding, and drawing a de-novo alignment would hide it behind something that
looks fine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Bases that carry information. Anything else in a column -- a gap, an ambiguity code, the
# padding that registration adds outside the query's own span -- is not a difference and
# must not be counted as one.
_DEFINITE = frozenset("ACGT")

# How many differing columns to draw before the figure stops being readable. Bounded by
# the page: each column has to be wide enough for a three-digit position number written
# horizontally, and twenty of those is what fits across A4 without clipping.
#
# Rotated labels were tried first and abandoned -- WeasyPrint's writing-mode support turned
# them into an unreadable strip, and a figure that only works in a browser is no good when
# the copy curators actually open is the PDF.
#
# A submission with more differences than this is not one where the extra columns would
# help: at that point the finding is "it is very different from everything", which the
# count states better than another forty columns would.
MAX_COLUMNS = 20

# How many neighbours to show. Five is enough to see whether a difference is shared or
# unique without turning the figure into a phylogeny.
DEFAULT_NEIGHBOURS = 5


@dataclass
class AlignmentFigure:
    """The data behind one figure: rows, the columns that vary, and what was left out."""

    label: str
    positions: List[int] = field(default_factory=list)      # 1-based frame positions
    rows: List[Dict[str, Any]] = field(default_factory=list)
    n_differing: int = 0                                    # before any truncation
    truncated: bool = False
    # One entry per shown column, describing the change against the nearest lineage:
    # codon position, transition/transversion, whether the amino acid changes, and how
    # common the query's base is at that site across the whole release. These are what
    # the QC verdict is already asserting in prose ("2 transversions", "1 rare base"),
    # and the figure is where they can actually be pointed at.
    columns: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)          # the caption, as sentences
    # How the submitted sequence was placed in the frame. A figure of a *registered*
    # sequence that does not say so is the one thing a reader cannot check: the rows may
    # have been shifted to make them comparable, and a stop-codon warning raised against
    # the sequence as submitted then looks like it contradicts a clean-looking picture.
    offset: Optional[int] = None
    framing: str = ""
    unavailable: Optional[str] = None                       # why there is no figure

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "positions": list(self.positions),
            "rows": list(self.rows),
            "n_differing": self.n_differing,
            "truncated": self.truncated,
            "unavailable": self.unavailable,
            "columns": list(self.columns),
            "notes": list(self.notes),
            "offset": self.offset,
            "framing": self.framing,
        }


def build_figure(
    label: str,
    registered: Optional[str],
    nearest: Sequence[Tuple[str, int, int]],
    reference_seqs: Dict[str, str],
    max_columns: int = MAX_COLUMNS,
    neighbours: int = DEFAULT_NEIGHBOURS,
    offset: Optional[int] = None,
    stops_as_submitted: Optional[int] = None,
) -> AlignmentFigure:
    """Build the figure for one sequence.

    Args:
        label: the submitted lineage name.
        registered: the query placed in the 479 bp frame, or ``None`` if it could not be
            registered at all.
        nearest: ``(lineage, distance, comparable)`` triples, nearest first.
        reference_seqs: lineage name -> its aligned sequence from the release.
        max_columns: stop after this many differing columns.
        neighbours: how many relatives to show.
    """
    if not registered:
        return AlignmentFigure(
            label=label,
            unavailable="This sequence could not be registered against the reference "
                        "alignment, so there is no meaningful alignment to show. That is "
                        "the finding: resolve the framing before comparing it to anything.")

    chosen = [(name, dist, comp) for name, dist, comp in nearest[:neighbours]
              if name in reference_seqs]
    if not chosen:
        return AlignmentFigure(
            label=label,
            unavailable="No lineage in the release overlaps this sequence closely enough "
                        "to align against.")

    rows_seqs = [registered] + [reference_seqs[name] for name, _d, _c in chosen]
    width = min(len(s) for s in rows_seqs)

    # A column is worth showing when the sequences that *define* a base there do not all
    # agree. Columns where a sequence is padded or ambiguous are compared on the rest --
    # a lineage that simply does not cover a position has no opinion about it.
    differing: List[int] = []
    for column in range(width):
        seen = {s[column] for s in rows_seqs if s[column] in _DEFINITE}
        if len(seen) > 1:
            differing.append(column)

    n_differing = len(differing)
    truncated = n_differing > max_columns
    shown = differing[:max_columns]

    def cells(sequence: str, is_query: bool) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for column in shown:
            base = sequence[column]
            query_base = registered[column]
            if is_query:
                state = "query"
            elif base not in _DEFINITE or query_base not in _DEFINITE:
                # Not a difference: one side has nothing to say at this position.
                state = "nodata"
            elif base == query_base:
                state = "same"
            else:
                state = "diff"
            out.append({"base": base, "state": state})
        return out

    figure = AlignmentFigure(
        label=label,
        positions=[c + 1 for c in shown],          # 1-based, as a curator counts
        n_differing=n_differing,
        truncated=truncated,
    )
    figure.offset = offset
    figure.framing = _describe_framing(registered, offset, stops_as_submitted)
    nearest_name, _nd, _nc = chosen[0]
    figure.columns = _describe_columns(
        shown, registered, reference_seqs[nearest_name], reference_seqs)
    figure.notes = _describe_change(figure.columns, differing, width, len(chosen))
    figure.rows.append({
        "name": label,
        "is_query": True,
        "distance": None,
        "comparable": None,
        "cells": cells(registered, True),
    })
    for name, distance, comparable in chosen:
        figure.rows.append({
            "name": name,
            "is_query": False,
            "distance": distance,
            "comparable": comparable,
            "cells": cells(reference_seqs[name], False),
        })
    return figure


# A<->G and C<->T are transitions; every other substitution is a transversion. The
# distinction matters here because transitions are the common, usually silent change and
# transversions are the rarer one, so a difference being a transversion is a small piece
# of evidence that it is real rather than a miscall.
_TRANSITIONS = frozenset({("A", "G"), ("G", "A"), ("C", "T"), ("T", "C")})

# A base occurring at or below this share of the lineages that cover a site is called
# rare there. Not a threshold anything is decided on -- it only decides whether the cell
# is marked, so a curator looks at it.
RARE_BASE_SHARE = 0.01

# The alignment's tip labels carry the parasite genus as a one-letter prefix, the same
# convention config/project.yml records as `alignment.genus_prefixes`.
_GENUS_OF = {"P_": "Plasmodium", "H_": "Haemoproteus", "L_": "Leucocytozoon"}


def _describe_framing(registered: str, offset: Optional[int],
                      stops_as_submitted: Optional[int]) -> str:
    """Whether these rows are the sequence as sent, or as moved into the MalAvi frame.

    Every figure here draws the *registered* sequence, because that is the only version
    comparable to the reference. When registration had to move it, the picture and the QC
    warnings are describing two different arrangements of the same bases, and a reader
    with no way to tell will read a clean-looking alignment as contradicting a stop-codon
    warning. It does not contradict it: the stops are in the submitted arrangement, and
    moving the sequence into frame is what removes them.

    NECMON01 is the case. It arrives at frame position 3, malaviR reports two stop codons,
    and the alignment drawn from the registered copy translates cleanly -- three true
    statements that look like a contradiction until somebody says which arrangement each
    one is about.
    """
    from .sequence_check import count_stops                 # noqa: PLC0415 - avoids a cycle

    if not offset:
        return ("Shown as submitted: this sequence already sat in the MalAvi reading "
                "frame, so nothing below has been moved.")

    # In place: the registered sequence is already in frame, and stripping gaps or Ns
    # out of it shifts everything after them. Doing that reported 14 stop codons for a
    # sequence the screen scores as having none. translate() maps an unreadable codon to
    # X, which is what a gap or an N should produce.
    here = count_stops(registered)
    text = (f"Shown registered into the MalAvi frame. As submitted it began at frame "
            f"position {offset + 1}, so every row below is shifted by {offset} base"
            f"{'' if offset == 1 else 's'} against the file that arrived.")
    if here == 0:
        text += (" In this frame it translates without stop codons — so a stop-codon "
                 "warning anywhere else in this report is about the sequence as "
                 "submitted, not about what is drawn here.")
    else:
        text += (f" It still translates with {here} stop codon"
                 f"{'' if here == 1 else 's'} in this frame, which the shift does not "
                 f"explain away.")
    return text


def _describe_columns(shown: Sequence[int], query: str, nearest: str,
                      reference_seqs: Dict[str, str]) -> List[Dict[str, Any]]:
    """What kind of change each shown column is, measured against the nearest lineage.

    The reading frame is frame 1 of the 479 bp window under genetic code 4, which is what
    ``sequence_check.translate`` uses, so codon position is simply the column index mod 3
    and the codon is the three columns around it. Using a different frame here than the
    one the stop-codon count came from would put two disagreeing statements on one page.
    """
    from .sequence_check import translate                   # noqa: PLC0415 - avoids a cycle

    out: List[Dict[str, Any]] = []
    for column in shown:
        query_base = query[column] if column < len(query) else "-"
        near_base = nearest[column] if column < len(nearest) else "-"
        entry: Dict[str, Any] = {
            "position": column + 1,
            "codon_position": (column % 3) + 1,
            "query_base": query_base,
            "nearest_base": near_base,
            "kind": "nodata",
            "amino_acid": None,
            "rare": False,
            "share": None,
        }
        if query_base in _DEFINITE and near_base in _DEFINITE and query_base != near_base:
            entry["kind"] = ("transition" if (query_base, near_base) in _TRANSITIONS
                             else "transversion")
            start = column - (column % 3)
            q_codon, n_codon = query[start:start + 3], nearest[start:start + 3]
            if len(q_codon) == 3 and len(n_codon) == 3:
                q_aa, n_aa = translate(q_codon), translate(n_codon)
                if q_aa and n_aa and "X" not in (q_aa + n_aa) and q_aa != n_aa:
                    entry["kind"] = "nonsynonymous"
                    entry["amino_acid"] = f"{n_aa}->{q_aa}"

        # How common the query's base is at this site across the whole release -- all
        # three genera, not just the submitted lineage's own. That scope is worth being
        # explicit about: a base that is rare across MalAvi as a whole can be ordinary
        # within one genus, and the two readings support opposite conclusions. So the
        # tally is also broken down by genus, which the alignment's own names carry as a
        # P_/H_/L_ prefix.
        #
        # Counted only over lineages that actually cover the position: a lineage padded
        # here has no opinion, and counting it would make every base look rarer.
        if query_base in _DEFINITE:
            carrying = total = 0
            per_genus: Dict[str, int] = {}
            for name, sequence in reference_seqs.items():
                if column >= len(sequence) or sequence[column] not in _DEFINITE:
                    continue
                total += 1
                if sequence[column] == query_base:
                    carrying += 1
                    genus = _GENUS_OF.get(str(name)[:2].upper(), "other")
                    per_genus[genus] = per_genus.get(genus, 0) + 1
            if total:
                entry["share"] = carrying / total
                entry["rare"] = entry["share"] <= RARE_BASE_SHARE
                entry["carrying"] = carrying
                entry["covering"] = total
                entry["per_genus"] = per_genus
        out.append(entry)
    return out


def _describe_change(columns: Sequence[Dict[str, Any]], differing: Sequence[int],
                     width: int, n_neighbours: int) -> List[str]:
    """The caption, as sentences a curator can act on.

    Replaces a bare count of differing positions, which said nothing the table above it
    did not already show. What is worth saying is the character of the differences: how
    many change the protein, how many are transversions, whether any base is rare at its
    site, and whether the differences are spread along the barcode or bunched -- bunching
    is the pattern a chimera or a mixed template leaves, and it is the one thing a
    curator can judge from this figure that the QC score only asserts.
    """
    notes: List[str] = []
    nonsyn = [c for c in columns if c["kind"] == "nonsynonymous"]
    transversions = [c for c in columns if c["kind"] == "transversion"]
    rare = [c for c in columns if c["rare"]]

    if nonsyn:
        changes = ", ".join(f"{c['position']} ({c['amino_acid']})" for c in nonsyn[:4])
        notes.append(f"{len(nonsyn)} of these change the amino acid: {changes}"
                     + (", and others" if len(nonsyn) > 4 else "") + ".")
    else:
        notes.append("None of these change the amino acid — every difference is silent.")

    if transversions:
        notes.append(f"{len(transversions)} "
                     + ("is a transversion" if len(transversions) == 1
                        else "are transversions")
                     + " rather than the commoner transition.")

    for c in rare[:3]:
        by_genus = c.get("per_genus") or {}
        breakdown = ("; ".join(f"{g} {n}" for g, n in sorted(by_genus.items()))
                     if by_genus else "none in any genus")
        notes.append(
            f"At position {c['position']} the base {c['query_base']} is carried by "
            f"{c.get('carrying', 0)} of the {c.get('covering', 0)} lineages in the whole "
            f"release that cover that site ({c['share'] * 100:.1f}%), counting all three "
            f"genera — {breakdown}.")

    # There was a sentence here describing whether the differences bunched into one
    # stretch, offered as the chimera pattern. It was wrong twice and is deleted rather
    # than repaired. malaviR does not detect chimeras by clustering at all: it slides a
    # 120 bp window along the barcode and asks which lineage is nearest *in that window*,
    # and flags a sequence when the answer keeps changing (see .qc_detect_chimera in
    # malaviR/R/internal.R). A mosaic of parents is a different thing from a run of
    # adjacent differences, and ANAZON02 was called a possible chimera while this note
    # said its differences were spread out -- reading as a contradiction, because the two
    # sentences were about different things. The second fault was that the span test
    # almost never fired, so nearly every lineage got the same "spread across the window"
    # line, which is noise however true it is.
    #
    # The real evidence is per-window parentage, which the QC screen computes and which
    # is now reported beside the chimera call itself.
    return notes


def build_figures(
    screen_reports: Sequence[Dict[str, Any]],
    registered_by_label: Dict[str, str],
    reference_seqs: Dict[str, str],
    **kwargs: Any,
) -> List[AlignmentFigure]:
    """Build a figure for every sequence in a screen report."""
    figures: List[AlignmentFigure] = []
    for report in screen_reports or []:
        for entry in report.get("sequences", []) or []:
            label = entry.get("label") or "sequence"
            nearest = [(n["lineage"], n["distance"], n["comparable"])
                       for n in entry.get("nearest", [])]
            figures.append(build_figure(
                label=label,
                registered=registered_by_label.get(label),
                nearest=nearest,
                reference_seqs=reference_seqs,
                offset=entry.get("offset"),
                stops_as_submitted=entry.get("n_stop_codons_as_submitted"),
                **kwargs))
    return figures
