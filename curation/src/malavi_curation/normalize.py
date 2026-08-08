"""The normalization contract shared by both intake paths.

Two submissions that say the same thing must produce the same object, whether they
arrived as a filled template through the Google Form or were extracted from a PDF.
Schema-validity alone does not give that: one path can write ``""`` where the other
writes ``null``, one can leave a non-breaking space in a host name, and the two
records then compare as different while meaning the same thing.

Every rule here is deliberately one of two kinds, and the distinction matters:

**Hygiene** -- whitespace, Unicode form, empty-to-absent. Applied silently, because
no information is carried by the difference between ``"India "`` and ``"India"``.

**Semantic** -- anything that changes what a value *says*, such as upper-casing a
lineage name. Applied, but always *recorded*, so the curator sees what the submitter
typed next to what the system made of it. ``record_change()`` is how a caller keeps
that record; nothing here rewrites a submitter's meaning without leaving a trace.

Country, locality and site names get hygiene only. They are exactly the fields a
curator may need to correct against a gazetteer, and quietly "fixing" one would hide
the correction that a human should be making.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

# Characters Excel and word processors leave in cells that are spaces to a reader but
# not to ``str.strip()``: non-breaking space, zero-width space, byte-order mark.
_INVISIBLE_SPACE = " ​﻿"

# Accession lists arrive as one cell holding several accessions in whatever separator
# the submitter reached for.
_ACCESSION_SPLIT = re.compile(r"[,;\s]+")

# Everything that is not a nucleotide symbol is stripped when cleaning a sequence:
# spacing, digits from a pasted alignment ruler, and FASTA line breaks.
_NON_NUCLEOTIDE = re.compile(r"[^A-Za-z\-]")


def text(value: Any) -> Optional[str]:
    """Hygiene for any free-text cell: ``None`` if it holds nothing meaningful.

    Normalizes to Unicode NFC (so a precomposed 'é' and a decomposed 'e' + combining
    accent become the same string), converts invisible spaces to ordinary ones,
    collapses runs of whitespace, and strips. An empty result becomes ``None``
    rather than ``""`` -- the schema treats a field as absent or null, never blank,
    and one path emitting ``""`` where the other emits ``None`` is precisely the
    silent divergence this module exists to prevent.
    """
    if value is None:
        return None
    # openpyxl hands back numbers, dates and booleans as their Python types; render
    # them the obvious way rather than refusing to normalize them.
    raw = value if isinstance(value, str) else str(value)
    raw = unicodedata.normalize("NFC", raw)
    for ch in _INVISIBLE_SPACE:
        raw = raw.replace(ch, " ")
    collapsed = " ".join(raw.split())
    return collapsed or None


def lineage_name(value: Any) -> Optional[str]:
    """A lineage name in MalAvi's own casing.

    MalAvi lineage names are upper case throughout the database, so a submitter who
    types ``tumig19`` means ``TUMIG19``. This is a *semantic* change: it makes a name
    match the release index that would otherwise miss. Callers pass the result to
    ``record_change()`` so the curator sees both forms.
    """
    cleaned = text(value)
    if not cleaned:
        return None
    # Internal whitespace is removed, not just trimmed: no MalAvi lineage name contains
    # any (checked against all 5368 names in the 2026-03-23 release), so a submitter who
    # typed "SGS 1" or "TUMIG 19" means the name without the space. Leaving it in made the
    # "this name is already taken" check miss, which is the one miss this project most
    # needs to avoid.
    return re.sub(r"\s+", "", cleaned).upper()


def accession_list(value: Any) -> List[str]:
    """Split one cell into accessions, upper-cased, order preserved, duplicates dropped.

    Submitters separate accessions with commas, semicolons, spaces or line breaks;
    all four mean the same thing. Nothing here judges whether an accession is
    well formed -- that is a check, and a check must be able to report a malformed
    accession rather than have it silently vanish during normalization.
    """
    cleaned = text(value)
    if not cleaned:
        return []
    out: List[str] = []
    for token in _ACCESSION_SPLIT.split(cleaned):
        upper = token.strip().upper()
        if upper and upper not in out:
            out.append(upper)
    return out


def sequence_pair(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(as_submitted, cleaned)`` for a pasted nucleotide sequence.

    Both are kept. The cleaned form is what the checks compare against the release;
    the submitted form is what the report shows the curator, because a stop codon or
    a novel base has to be reported at its position *in the text the submitter
    pasted* for them to be able to find it. Gaps (``-``) survive cleaning: an
    alignment gap is information, not noise.
    """
    if value is None:
        return None, None
    raw = value if isinstance(value, str) else str(value)
    raw = unicodedata.normalize("NFC", raw)
    if not raw.strip():
        return None, None
    cleaned = _NON_NUCLEOTIDE.sub("", raw).upper()
    return raw.strip(), (cleaned or None)


# Recognized parasite genera, longest-qualifying name first: cytochrome b lineages of
# *Parahaemoproteus* are filed under *Haemoproteus* in MalAvi, and that substring must
# be tested before the bare "haemoproteus" match would claim it.
_GENUS_CANON = (
    ("leucocytozoon", "Leucocytozoon"),
    ("parahaemoproteus", "Haemoproteus"),
    ("haemoproteus", "Haemoproteus"),
    ("plasmodium", "Plasmodium"),
)


def clean_genus(value: Any) -> Optional[str]:
    """Map a free-text cell to the schema's ``parasite_genus`` enum, or ``None``.

    Column-header matching in ``table_extract`` can misfire on messy supplements
    (a statistics column landing in "genus"), so anything unrecognized is coerced to
    ``None`` -- a curator-review value -- rather than allowed to fail schema
    validation and crash the whole paper.
    """
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    for needle, canonical in _GENUS_CANON:
        if needle in lowered:
            return canonical
    return None


def clean_count(value: Any) -> Optional[float]:
    """Coerce a count cell to a whole number, or ``None`` if it is not one.

    Guards ``number_tested`` / ``number_found`` against non-count cells ("30/47",
    "Sensitivity = 0.64") that a mis-parsed supplement table can drop here.

    Non-integral values are rejected rather than kept. These fields count individuals
    -- MalAvi's NUMBER_TESTED and NUMBER_FOUND -- so 3.5 birds is not a low-confidence
    count, it is a cell that came from somewhere else (a percentage, a mean, a ratio).
    The column gate in ``table_extract`` already requires integer-looking values, and
    accepting floats here would contradict it: the two checks would disagree about
    what a count is.
    """
    if value is None:
        return None
    if isinstance(value, bool):          # bool is an int subclass; not a count
        return None
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value if value.is_integer() else None
    as_text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d+(?:\.0+)?", as_text):
        return None
    return float(as_text)


def source_ref(sheet: Optional[str] = None, row: Optional[int] = None,
               file: Optional[str] = None, table: Optional[str] = None) -> Dict[str, Any]:
    """Where a value came from, with the empty parts left out.

    A finding that says "Hosts_and_Sites, row 19" sends the curator straight to the
    cell. A finding that only names a lineage makes them go hunting for it, and on a
    sheet with hundreds of rows that is the difference between a report they use and
    one they skim.
    """
    ref = {"sheet": sheet, "row": row, "file": file, "table": table}
    return {key: val for key, val in ref.items() if val is not None}


def record_change(changes: List[Dict[str, Any]], field: str, submitted: Any,
                  normalized: Any, source: Optional[Dict[str, Any]] = None) -> None:
    """Append a normalization to ``changes`` if it actually changed the value.

    Appends nothing when the value survived unaltered, so the list carries only the
    differences a curator might want to challenge. Hygiene-only changes are recorded
    too: a trailing space that vanished is not worth a curator's attention, but
    knowing the system touched the value costs nothing to report and settles any
    argument about whether a mismatch came from the submitter or from us.
    """
    if submitted == normalized:
        return
    # An empty cell becoming ``None`` is the schema's own convention, not a change
    # anyone needs to see.
    if normalized is None and (submitted is None or not str(submitted).strip()):
        return
    entry: Dict[str, Any] = {
        "field": field,
        "submitted": submitted if isinstance(submitted, (str, int, float, type(None)))
        else str(submitted),
        "normalized": normalized,
    }
    if source:
        entry["source"] = source
    changes.append(entry)
