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
    unavailable: Optional[str] = None                       # why there is no figure

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "positions": list(self.positions),
            "rows": list(self.rows),
            "n_differing": self.n_differing,
            "truncated": self.truncated,
            "unavailable": self.unavailable,
        }


def build_figure(
    label: str,
    registered: Optional[str],
    nearest: Sequence[Tuple[str, int, int]],
    reference_seqs: Dict[str, str],
    max_columns: int = MAX_COLUMNS,
    neighbours: int = DEFAULT_NEIGHBOURS,
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
                **kwargs))
    return figures
