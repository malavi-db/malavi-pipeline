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

# The same, one letter short. Matched only so it can be reported as a misspelling; note
# it must NOT also match a correctly spelled name, hence the negative lookahead.
_MISSPELLED = re.compile(
    r"^(?P<authors>.+?)\s+unpub(?!l)(?:\s+(?P<disambiguator>[a-z]|\d+))?$"
)


def is_unpublished(name: str) -> bool:
    """Does this reference name mark an unpublished study, however it is spelled?

    Accepts the misspelling on purpose. A caller asking "should I expect a reference row
    for this?" wants the answer for ``Romano et al unpub`` too; a caller asking "is this
    written correctly?" should use :func:`problem_with`.
    """
    text = (name or "").strip()
    return bool(_UNPUBLISHED.match(text) or _MISSPELLED.match(text))


def authors_of(name: str) -> str:
    """The author part of an unpublished reference name, or "" if it is not one.

    Used when renaming: ``Barrow et al unpubl`` and ``Barrow et al 2027`` should be
    recognizably the same study, and this is what makes that comparable.
    """
    text = (name or "").strip()
    match = _UNPUBLISHED.match(text) or _MISSPELLED.match(text)
    return match.group("authors").strip() if match else ""


def problem_with(name: str) -> Optional[str]:
    """What is wrong with this unpublished reference name, or None if nothing is.

    Returns None for a name that is not marked unpublished at all -- judging a published
    citation is not this function's job.
    """
    text = (name or "").strip()
    if not text:
        return None
    if _UNPUBLISHED.match(text):
        return None
    if _MISSPELLED.match(text):
        return (f"{text!r} is spelled 'unpub'. MalAvi spells the marker "
                f"'{MARKER}', so write {canonical(text)!r}.")
    # Something contains the marker but does not sit where the convention puts it --
    # "Unpubl data from Barrow", "Barrow unpublished 2027". Worth a curator's eye rather
    # than a silent pass, because a record filed under it will not group with the rest.
    if MARKER in text.lower():
        return (f"{text!r} mentions '{MARKER}' but does not follow the MalAvi "
                f"convention '<Authors> {MARKER}' (optionally followed by a single "
                f"letter or number to distinguish it from another unpublished study "
                f"by the same group).")
    return None


def canonical(name: str) -> str:
    """The correctly spelled form of an unpublished reference name.

    Returns the name unchanged when it is already correct, or is not an unpublished
    name at all. Only the marker is touched: author strings are the submitter's and are
    never rewritten here.
    """
    text = (name or "").strip()
    if _UNPUBLISHED.match(text):
        return text
    match = _MISSPELLED.match(text)
    if not match:
        return text
    disambiguator = match.group("disambiguator")
    rebuilt = f"{match.group('authors').strip()} {MARKER}"
    return f"{rebuilt} {disambiguator}" if disambiguator else rebuilt
