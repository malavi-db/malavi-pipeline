"""Resolve a paper's own sequence identifiers to the MalAvi lineage names they are.

A paper very often names its haemosporidian sequences in its own private
namespace. McNew et al 2021's Dataset S1 is the clearest case in the ground-truth
corpus: its ``Haplotype`` column runs ``T001``..``T262`` and its ``Lineage``
column ``G-001``..``G-309``, neither of which is a MalAvi name. MalAvi curated the
same 1,249 rows as ``ADEMEL02``, ``AFR025``, ``COLBUC01`` and so on.

Reading those rows is not the problem -- the extractor already reads them
correctly. The problem is purely that the *join key* does not exist, and the
2026-07-29 benchmark therefore scored 1,219 perfectly correct host x parasite
associations as false positives.

The paper states the missing link itself: Dataset S1 carries the raw cytochrome b
sequence in a column of its own. A sequence that is identical to a named MalAvi
lineage over the positions both cover **is** that lineage -- this is the same rule
``sequence_check`` already applies in the other direction to refuse to call a
known lineage new. So the resolution is a lookup, not an inference:

    T009  ->  CAACAGGTGCATCTTTTGTATTTATTTT...  ->  COLBUC01

Everything here is deterministic and offline. There is no model, no similarity
threshold and no scoring: a name is assigned only on **exact identity** over a
substantial overlap, and only when exactly one MalAvi lineage matches. Every other
outcome (no match, several matches, too little overlap, unplaceable sequence)
resolves to *no name* plus a note for the curator, never to a guess.

Why a dedicated module rather than ``sequence_check.check_sequence``: that
function compares one query against all ~5,400 release sequences to rank the
nearest neighbors, which costs roughly half a second per query in pure Python.
A paper's supplement can carry hundreds of distinct sequences. Exact identity can
be found far more cheaply with an anchor index (see ``LineageResolver``), so the
fast path is used to find the match and ``check_sequence`` is called only for the
sequences that turn out to be novel -- which are exactly the ones where a curator
wants the full nearest-neighbor report anyway.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .sequence_check import (
    BASES, MAX_OFFSET, Reference, check_sequence, clean, _distance, _place,
    _register,
)

# Positions used to pre-filter the reference alignment for exact matches. Each
# window is a slice of the alignment; a reference whose slice is identical to the
# query's is a candidate for full comparison.
#
# Three windows are kept so a query with an ambiguous base inside one can still
# use another. They sit well away from both ends of the 479 bp frame, because
# short deposits are padded there and would be ambiguous in every window if the
# windows were near the edges.
_ANCHOR_WINDOWS: Tuple[Tuple[int, int], ...] = ((140, 170), (240, 270), (330, 360))

# Comparable positions required before an exact match is allowed to *assign* a
# name. A 60 bp fragment identical to a 479 bp lineage is consistent with that
# lineage but does not identify it -- dozens of MalAvi lineages are identical over
# any short stretch. The barcode is 478-479 bp, so this is a generous floor that
# still admits the genuinely short deposits in the release.
MIN_COMPARABLE_TO_ASSIGN = 300


def lineage_name_of(fasta_header: str) -> str:
    """MalAvi lineage name from an alignment FASTA header.

    The release alignment names records ``<genus letter>_<lineage>[_<morphospecies>]``:
    ``H_COLBUC01_Haemoproteus_multipigmentatus`` is lineage ``COLBUC01``, and
    ``P_MYRAXI01`` is ``MYRAXI01``. Verified against the 2026-03-23 release: this
    rule recovers a name present in the release lineage list for 5,364 of the
    5,365 alignment records.
    """
    body = fasta_header.strip().lstrip(">")
    body = re.sub(r"^[A-Za-z]+_", "", body, count=1)   # drop the genus letter
    return body.split("_")[0]                          # drop any morphospecies suffix


@dataclass
class LineageMatch:
    """The outcome of resolving one sequence against the release alignment.

    ``lineage_name`` is filled **only** for the ``resolved`` verdict. Every other
    verdict carries a note explaining what the curator is looking at.
    """

    verdict: str                                  # see VERDICTS below
    lineage_name: Optional[str] = None            # assigned MalAvi name, or None
    matched: List[str] = field(default_factory=list)   # every exact match found
    comparable: int = 0                           # positions compared for the match
    offset: Optional[int] = None                  # frame offset used (see sequence_check)
    # Exact matches that cover *less* of the sequence than the assigned one: the
    # release's partial deposits, identical as far as they go. Reported, not
    # treated as a conflict -- see ``resolve``.
    also_consistent: List[Tuple[str, int]] = field(default_factory=list)
    nearest: List[Tuple[str, int, int]] = field(default_factory=list)  # novel path only
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "lineage_name": self.lineage_name,
            "matched": list(self.matched),
            "comparable": self.comparable,
            "offset": self.offset,
            "also_consistent": [{"lineage": n, "comparable": c}
                                for n, c in self.also_consistent],
            "nearest": [{"lineage": n, "distance": d, "comparable": c}
                        for n, d, c in self.nearest],
            "note": self.note,
        }


# Every verdict this module can return, and what a curator should read into it.
VERDICTS = {
    "resolved": "identical to exactly one MalAvi lineage; the name was assigned",
    "ambiguous": "identical to several MalAvi lineages; no name assigned",
    "match_too_short": "identical to a MalAvi lineage but over too little overlap to assign",
    "novel": "not identical to any MalAvi lineage; may be a new lineage",
    "unplaceable": "could not be registered to the MalAvi reading frame",
    "empty": "no sequence to check",
}


class LineageResolver:
    """Exact-identity lookup of a sequence against the pinned release alignment.

    Usage::

        resolver = LineageResolver.from_release(repo_root(), "2026-03-23")
        match = resolver.resolve("CAACAGGTGCATCTTTTGT...")
        match.lineage_name        # "COLBUC01"

    The index makes the common case cheap. For each anchor window it maps the
    reference's slice at that window to the reference records carrying it; a
    reference that is ambiguous inside a window (an ``N``, a gap, or padding) is
    kept in a separate per-window list and always treated as a candidate.

    That pre-filter is **exhaustive for exact matches**, which is what makes it
    safe to use in place of a full scan: a reference identical to the query over
    all comparable positions either agrees with the query across a window where
    both are unambiguous -- and is therefore found in the index under the query's
    own slice -- or is ambiguous somewhere in that window and is therefore in the
    window's ambiguous list. No exact match can be missed.
    """

    def __init__(self, reference: Reference) -> None:
        self._ref = reference
        # Lineage name per alignment record, in the same order as reference.seqs.
        self._names = [lineage_name_of(name) for name in reference.names]
        # Per window: slice -> record indexes, and the records too ambiguous to index.
        self._index: List[Dict[str, List[int]]] = []
        self._ambiguous: List[List[int]] = []
        self._build_index()

    # --- construction ------------------------------------------------------

    def _build_index(self) -> None:
        """Index every reference record at each anchor window."""
        for start, end in _ANCHOR_WINDOWS:
            by_slice: Dict[str, List[int]] = {}
            ambiguous: List[int] = []
            for record_index, sequence in enumerate(self._ref.seqs):
                fragment = sequence[start:end]
                # Only a fragment made entirely of unambiguous bases can be
                # looked up by equality; anything else must always be compared.
                if len(fragment) == end - start and all(b in BASES for b in fragment):
                    by_slice.setdefault(fragment, []).append(record_index)
                else:
                    ambiguous.append(record_index)
            self._index.append(by_slice)
            self._ambiguous.append(ambiguous)

    @classmethod
    def from_alignment(cls, path: str | Path) -> "LineageResolver":
        """Build a resolver from a release alignment FASTA."""
        return cls(Reference.from_fasta(Path(path)))

    @classmethod
    def for_pinned_release(cls) -> Optional["LineageResolver"]:
        """Resolver for the release pinned in ``config/project.yml``, or None.

        Cached, because building the index reads a 2.6 MB alignment. Returns None
        when the alignment has not been exported, which leaves every caller
        working exactly as it did before this module existed -- study-local
        lineage names simply stay unresolved.
        """
        return _pinned_resolver()

    @classmethod
    def from_release(cls, repo: Path, release: str) -> Optional["LineageResolver"]:
        """Build a resolver from the pinned release's alignment, or None if absent.

        Returns None rather than raising so a caller without the alignment file
        keeps working -- it simply cannot resolve study-local names, which is
        exactly the behavior before this module existed.
        """
        from .sequence_check import default_alignment_path

        path = default_alignment_path(repo, release)
        return cls.from_alignment(path) if path else None

    # --- resolution --------------------------------------------------------

    def _candidates(self, placed: str) -> Sequence[int]:
        """Reference records that could be identical to this placed query.

        Each usable window contributes a complete candidate set (see the class
        docstring), so **intersecting** the windows is still complete and is much
        more selective than any one of them: the cytb barcode is conserved enough
        that the 5,365 records share only 166 distinct fragments at the first
        window, and a single window's bucket can hold thousands of records.
        """
        candidates: Optional[set] = None
        for window_index, (start, end) in enumerate(_ANCHOR_WINDOWS):
            fragment = placed[start:end]
            # Only a window where the query itself is unambiguous can be matched
            # by equality; skip any window where it is not.
            if len(fragment) != end - start or not all(b in BASES for b in fragment):
                continue
            window = set(self._index[window_index].get(fragment, ()))
            window.update(self._ambiguous[window_index])
            candidates = window if candidates is None else (candidates & window)
        if candidates is None:
            # The query is ambiguous in every window (very short, or heavily
            # masked), so nothing can be ruled out and every record is compared.
            return range(len(self._ref.seqs))
        return sorted(candidates)

    def resolve(self, sequence: str, detail_on_miss: bool = True) -> LineageMatch:
        """Resolve one sequence to a MalAvi lineage name, or explain why not.

        ``detail_on_miss`` runs the full ``sequence_check`` nearest-neighbor
        report when no exact match is found. That is the slow path, and it is
        wanted for a novel sequence (the curator needs to see what it is close
        to) but pointless in bulk, so a caller processing thousands of rows can
        switch it off.
        """
        query = clean(sequence)
        if not query:
            return LineageMatch(verdict="empty", note="no sequence supplied")

        # Register the query to the MalAvi reading frame. Deposits are routinely
        # offset by a base or two; comparing unregistered would be meaningless.
        #
        # A sequence printed in a supplement can also be a longer amplicon with
        # the barcode window inside it -- 25 of McNew et al 2021's 515 haplotypes
        # are 818 bp -- so the slide has to reach far enough back to find the
        # window. Allowing the query to start up to
        # ``len(query) - MIN_COMPARABLE_TO_ASSIGN`` bases before the frame covers
        # that while still requiring an assignable overlap once placed.
        max_offset = max(MAX_OFFSET, len(query) - MIN_COMPARABLE_TO_ASSIGN)
        offset, _mismatch = _register(query, self._ref, max_offset=max_offset)
        if offset is None:
            return LineageMatch(
                verdict="unplaceable", offset=None,
                note="could not be registered to the MalAvi reading frame; it may "
                     "not be a haemosporidian cytochrome b barcode, may be reverse "
                     "complemented, or may carry indels")
        placed = _place(query, offset, self._ref.width)

        # Exact matches only: identical over every position both cover.
        exact: Dict[str, int] = {}          # lineage name -> comparable positions
        for record_index in self._candidates(placed):
            distance, comparable = _distance(placed, self._ref.seqs[record_index])
            if comparable and distance == 0:
                name = self._names[record_index]
                exact[name] = max(exact.get(name, 0), comparable)

        if not exact:
            return self._novel(sequence, placed, offset, detail_on_miss)

        # Several distinct names matching exactly is common, and is usually not a
        # real conflict: about a fifth of the release is a partial deposit, and a
        # partial lineage is identical to a full-length one as far as it goes.
        # McNew's T025 matches BAEBIC02 over 478 positions, SEIAUR02 over 367 and
        # TABI07 over 285 -- all three agree with each other wherever they
        # overlap, so the full-length match is simply the better-supported
        # identification, not a competing one.
        #
        # The name compared over the *most* positions therefore wins, and the
        # shorter matches are reported alongside it. A genuine tie at the maximum
        # overlap is the case where the sequence really cannot tell two names
        # apart, and that stays unresolved.
        names = sorted(exact)
        best_comparable = max(exact.values())
        best_names = sorted(name for name, comparable in exact.items()
                            if comparable == best_comparable)
        shorter = sorted(((name, comparable) for name, comparable in exact.items()
                          if comparable < best_comparable),
                         key=lambda item: (-item[1], item[0]))

        if len(best_names) > 1:
            return LineageMatch(
                verdict="ambiguous", matched=names, comparable=best_comparable,
                offset=offset, also_consistent=shorter,
                note="identical to " + ", ".join(best_names) + f" over the same "
                     f"{best_comparable} positions; the sequence does not "
                     f"distinguish them, so no name was assigned")

        name = best_names[0]
        if best_comparable < MIN_COMPARABLE_TO_ASSIGN:
            return LineageMatch(
                verdict="match_too_short", matched=names, comparable=best_comparable,
                offset=offset, also_consistent=shorter,
                note=f"identical to {name} but over only {best_comparable} "
                     f"comparable positions ({MIN_COMPARABLE_TO_ASSIGN} required to "
                     f"assign a name)")

        note = f"identical to {name} over all {best_comparable} comparable positions"
        if shorter:
            note += ("; also identical to the shorter release deposit(s) "
                     + ", ".join(f"{n} ({c} bp)" for n, c in shorter))
        return LineageMatch(
            verdict="resolved", lineage_name=name, matched=names,
            comparable=best_comparable, offset=offset, also_consistent=shorter,
            note=note)

    def _novel(self, sequence: str, placed: str, offset: int,
               detail_on_miss: bool) -> LineageMatch:
        """No exact match: report the nearest lineages if the caller wants them."""
        if not detail_on_miss:
            return LineageMatch(
                verdict="novel", offset=offset,
                note="not identical to any MalAvi lineage")
        detail = check_sequence(sequence, self._ref, label="query")
        nearest = [(lineage_name_of(name), distance, comparable)
                   for name, distance, comparable in detail.nearest]
        note = "not identical to any MalAvi lineage"
        if nearest:
            name, distance, comparable = nearest[0]
            note += (f"; nearest is {name} at {distance} bp of {comparable} "
                     f"compared")
        return LineageMatch(verdict="novel", offset=offset, nearest=nearest, note=note)

    @lru_cache(maxsize=4096)
    def _resolve_cached(self, sequence: str, detail_on_miss: bool) -> LineageMatch:
        return self.resolve(sequence, detail_on_miss=detail_on_miss)

    def resolve_cached(self, sequence: str, detail_on_miss: bool = True) -> LineageMatch:
        """``resolve`` memoized on the raw sequence string.

        A supplementary table repeats the same sequence on every row that shares a
        haplotype -- McNew et al 2021 has 1,695 rows carrying 515 distinct
        sequences -- so caching is the difference between one pass and three.
        """
        return self._resolve_cached(sequence, detail_on_miss)


# --- applying resolution to extracted rows ---------------------------------

# Fields written onto a row when its lineage name is resolved from a sequence.
# The printed value is never discarded: the curator has to be able to see that
# the paper said "T009" and that the name came from the sequence, not the page.
RESOLUTION_FIELDS = ("lineage_name_source", "lineage_resolution", "lineage_resolution_note")


def resolve_rows(rows: List[Dict[str, Optional[str]]], resolver: LineageResolver,
                 known_lineages: Optional[frozenset] = None,
                 detail_on_miss: bool = False) -> Dict[str, int]:
    """Fill in MalAvi lineage names for rows that carry a sequence. Mutates ``rows``.

    A row is resolved only when it needs to be. If the paper printed a name
    MalAvi already knows (``GRW04``), that name stands -- the paper is the
    authority on its own records and re-deriving it from the sequence could only
    introduce disagreement. Resolution is for the rows whose printed name is in
    the paper's private namespace, which is precisely the set MalAvi cannot join.

    Returns a count per verdict, for the run report.
    """
    known = known_lineages if known_lineages is not None else load_known_lineages()
    tally: Dict[str, int] = {}

    for row in rows:
        sequence = row.get("sequence")
        if not sequence:
            continue
        printed = (row.get("lineage_name") or "").strip()
        # Already a MalAvi name: nothing to resolve.
        if printed and printed.upper() in known:
            tally["already_named"] = tally.get("already_named", 0) + 1
            continue

        match = resolver.resolve_cached(sequence, detail_on_miss=detail_on_miss)
        tally[match.verdict] = tally.get(match.verdict, 0) + 1
        row["lineage_resolution"] = match.verdict
        row["lineage_resolution_note"] = match.note
        if match.verdict == "resolved":
            # Keep what the paper printed; put the MalAvi name in the field the
            # rest of the pipeline joins on.
            row["lineage_name_source"] = printed or None
            row["lineage_name"] = match.lineage_name

    return tally


@lru_cache(maxsize=1)
def _pinned_resolver() -> Optional[LineageResolver]:
    """Build (once) the resolver for the release pinned in config/project.yml."""
    from .config import load_config, repo_root

    release = str(load_config().get("malaviR", {}).get("release", "latest"))
    return LineageResolver.from_release(repo_root(), release)


@lru_cache(maxsize=1)
def load_known_lineages() -> frozenset:
    """Lineage names in the packaged DB snapshot; empty if it is unavailable.

    Used only to decide whether a printed name needs resolving at all, so an
    empty set is safe: every sequence-bearing row is then resolved, which is
    slower but not wrong.
    """
    from .gate import load_snapshot

    snapshot = load_snapshot() or {}
    return frozenset(str(name).upper() for name in snapshot.get("lineages", []))
