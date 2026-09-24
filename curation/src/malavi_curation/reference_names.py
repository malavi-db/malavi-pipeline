"""The naming convention for references MalAvi holds before they are published.

MalAvi has carried unpublished records since long before this pipeline existed. In the
seed release they are 838 rows across 62 studies -- 669 host records, 107 alternative
names, 58 vector records, 4 morphospecies assignments -- and every one of them cites a
reference of the form::

    Barrow et al unpubl
    Marzal unpubl
    Witt & McNew unpubl
    Rubenstein, Ellis and Ricklefs unpubl
    Rojo et al unpubl b

**None of them has a row in references.csv, and that is deliberate.** A reference row
carries a year, a title, a journal and pages; an unpublished study has none of those, and
inventing them would put a citation into MalAvi that leads nowhere. So the record tables
name the study and the reference table stays silent until there is something to cite.

Two things follow, and this module exists to make both mechanical rather than remembered:

* **The marker has to be spelled one way.** The seed contains 60 rows spelled ``unpubl``
  and 2 spelled ``unpub`` (Romano, Orfanides). Two spellings mean a curator filtering
  unpublished records gets 60 of them and does not notice, so new submissions are held to
  the majority spelling.
* **The name is temporary by design.** When the study appears, ``Barrow et al unpubl``
  becomes ``Barrow et al 2027`` across every table that cites it -- see
  ``curation/publish_reference.py``, which is the supported way to do that.

A disambiguating suffix is allowed and is used when one group has several unpublished
studies in MalAvi at once: ``Rojo et al unpubl a`` / ``b``, ``Hellgren et al unpubl 2``
/ ``3``. It is a single letter or a number, and it means nothing beyond "not the other
one".
"""
from __future__ import annotations

import re
from typing import Optional

# The spelling MalAvi uses. Not a preference -- the majority of the existing rows.
MARKER = "unpubl"

# "<authors> unpubl" with an optional single-letter or numeric disambiguator.
#
# The author part is deliberately unconstrained. The seed shows at least five shapes
# (one surname, two joined by "&", three joined by commas and "and", a full personal
# name, and the usual "X et al"), and a pattern tight enough to reject a bad one would
# reject several good ones. What matters here is the marker, not the authors.
_UNPUBLISHED = re.compile(
    r"^(?P<authors>.+?)\s+" + MARKER + r"(?:\s+(?P<disambiguator>[a-z]|\d+))?$"
)

# The marker spelled some other way. Matched so that the name can be recognized as
# unpublished AND rewritten to the MalAvi spelling, rather than sent back to a curator to
# retype. Seen in real submissions and in the seed: ``unpub`` (Romano, Orfanides),
# ``unpublished`` and ``unpublished data`` (a 2026-09-23 submission), with or without a
# trailing period, and with a comma before the marker ("Ellis et. al., unpublished").
# Case-insensitive because "Unpublished" at the start of a sentence-cased cell is the same
# word. The alternation is ordered longest first so "unpublished" is not read as "unpub"
# followed by junk.
_VARIANT = re.compile(
    r"^(?P<authors>.+?)[\s,]+(?:unpublished(?:\s+data)?|unpubl|unpub)\.?"
    r"(?:\s+(?P<disambiguator>[a-z]|\d+))?$",
    re.IGNORECASE,
)

# Punctuation submitters put around "et al" that MalAvi's citation keys never carry:
# "et al.", "et. al.", "et al," and any combination. Used on the author part of an
# unpublished name; published_form does the equivalent for published keys.
_ET_AL = re.compile(r"\bet\.?\s+al\.?,?", re.IGNORECASE)


def is_unpublished(name: str) -> bool:
    """Does this reference name mark an unpublished study, however it is spelled?

    Accepts the misspelling on purpose. A caller asking "should I expect a reference row
    for this?" wants the answer for ``Romano et al unpub`` too; a caller asking "is this
    written correctly?" should use :func:`problem_with`.
    """
    text = re.sub(r"\s+", " ", (name or "").strip())
    return bool(_UNPUBLISHED.match(text) or _VARIANT.match(text))


def authors_of(name: str) -> str:
    """The author part of an unpublished reference name, or "" if it is not one.

    Used when renaming: ``Barrow et al unpubl`` and ``Barrow et al 2027`` should be
    recognizably the same study, and this is what makes that comparable.
    """
    text = re.sub(r"\s+", " ", (name or "").strip())
    match = _UNPUBLISHED.match(text) or _VARIANT.match(text)
    return _tidy_authors(match.group("authors")) if match else ""


def _tidy_authors(authors: str) -> str:
    """The author part of a reference name with MalAvi's punctuation.

    "Ellis et. al.," becomes "Ellis et al"; "Barrow" stays "Barrow". Only the
    punctuation around "et al" and any trailing comma or period are touched. The
    spelling of the names themselves is the submitter's and is never rewritten.
    """
    text = _ET_AL.sub("et al", authors or "")
    text = re.sub(r"\s+", " ", text).strip().rstrip(",.").strip()
    return text


def problem_with(name: str) -> Optional[str]:
    """What is wrong with this unpublished reference name, or None if nothing is.

    Returns None for a name that is not marked unpublished at all -- judging a published
    citation is not this function's job.
    """
    text = re.sub(r"\s+", " ", (name or "").strip())
    if not text:
        return None
    if _UNPUBLISHED.match(text):
        return None
    # A recognizable variant ("unpub", "unpublished", "et. al., unpublished") is not a
    # problem any more: canonical() rewrites it and the ingest stores the rewritten form,
    # so there is nothing for a curator to do. The screen reports the rewrite as
    # information (check_template: reference_unpubl_form) rather than as a warning.
    if _VARIANT.match(text):
        return None
    # Something contains the marker but does not sit where the convention puts it --
    # "Unpubl data from Barrow", "Barrow unpublished 2027". Worth a curator's eye rather
    # than a silent pass, because a record filed under it will not group with the rest.
    if "unpub" in text.lower():
        return (f"{text!r} mentions '{MARKER}' but does not follow the MalAvi "
                f"convention '<Authors> {MARKER}' (optionally followed by a single "
                f"letter or number to distinguish it from another unpublished study "
                f"by the same group).")
    return None


def canonical(name: str) -> str:
    """The correctly spelled form of an unpublished reference name.

    Returns the name unchanged when it is already correct, or is not an unpublished
    name at all. The marker and the punctuation around "et al" are all that is touched:
    the spelling of the authors is the submitter's and is never rewritten here.
    """
    text = re.sub(r"\s+", " ", (name or "").strip())
    match = _UNPUBLISHED.match(text) or _VARIANT.match(text)
    if not match:
        return text
    disambiguator = match.group("disambiguator")
    # Rebuilt even for a name that already matched the strict pattern, because the
    # authors part may still carry "et al." with a period: "Barrow et al. unpubl" is
    # recognized as unpublished and is still not how MalAvi spells it.
    rebuilt = f"{_tidy_authors(match.group('authors'))} {MARKER}"
    return f"{rebuilt} {disambiguator.lower()}" if disambiguator else rebuilt


# ---------------------------------------------------------------------------
# Published citation keys
# ---------------------------------------------------------------------------

# "<Authors> <year>[letter]" -- the shape of every published reference name MalAvi holds:
# "Hellgren 2005", "Baillie & Brunton 2011", "Beadell et al 2009", "Loiseau et al 2012b".
# No comma before the year, no period after "al". The authors part is left free for the
# same reason as above.
_PUBLISHED_KEY = re.compile(r"^(?P<authors>.+?)\s+(?P<year>(?:19|20)\d\d)(?P<letter>[a-z])?$")


def looks_like_citation_key(name: str) -> bool:
    """Does this name end in a year the way every published MalAvi citation key does?

    "Hellgren 2005", "Vieira et al., 2023" (punctuation is respelled elsewhere) -> True.
    "Jane Smith, John Jones", a title, "Sasaki et al" -> False. Says nothing
    about unpublished names; use :func:`is_unpublished` for those.
    """
    text = re.sub(r"\s+", " ", (name or "").strip()).rstrip(".").rstrip(",").strip()
    return bool(text and _PUBLISHED_KEY.match(text))


def published_form(name: str) -> str:
    """The spelling MalAvi uses for a published citation key, from whatever was typed.

    ``Vieira et al., 2023`` becomes ``Vieira et al 2023``; ``Smith, 2023`` becomes
    ``Smith 2023``; an unpublished name is spelled by :func:`canonical`; a name that
    does not end in a year is returned with only its whitespace tidied. Only punctuation around "et al" and before
    the year is touched. Author spellings are the submitter's and are never rewritten.

    Every table joins on this string, and the site and malaviR read it as an identity, so
    a second spelling of one study is a second study to every consumer (review of
    2026-09-15, 3.2).
    """
    text = re.sub(r"\s+", " ", (name or "").strip())
    if not text:
        return text
    if is_unpublished(text):
        # An unpublished name gets the same treatment from canonical(): "Ellis et.
        # al., unpublished" is stored as "Ellis et al unpubl". Until 2026-09-23 it
        # was returned as typed, so a variant spelling reached the store and would have
        # been missed by every filter on 'unpubl'.
        return canonical(text)
    if not _PUBLISHED_KEY.match(text.rstrip(".").rstrip(",").strip()):
        return text
    text = text.rstrip(".").strip()
    text = re.sub(r"\bet\s+al\.?\s*,?\s*", "et al ", text)  # "et al.," / "et al," -> "et al"
    text = re.sub(r",\s*(?=(?:19|20)\d\d[a-z]?$)", " ", text)  # "Smith, 2023" -> "Smith 2023"
    text = re.sub(r"\s+", " ", text).strip()
    return text


def problem_with_published(name: str) -> Optional[str]:
    """How a published citation key will be respelled, or None if it is already MalAvi's way.

    Informational: the respelling is applied automatically at ingest. It is returned so the
    curator report can say what will be stored, not so anyone corrects the workbook.
    """
    text = (name or "").strip()
    if not text or is_unpublished(text):
        return None
    form = published_form(text)
    if form == text:
        return None
    # Phrased as what will happen, not as a request: the ingest applies published_form
    # at every place it stores a citation key, so nothing is asked of anyone. Until
    # 2026-09-23 this said "Write '...'", which read to a curator as a job for them.
    return (f"{text!r} will be filed as {form!r}, the way MalAvi spells a citation key: "
            f"no comma before the year and no period after 'et al'.")
