"""Read the Google Form answers that arrive beside a submission.

``fetch_submissions.py`` writes each response row to ``metadata.json`` with the *form
question* as the key, verbatim -- "What is your first and last name?" rather than
``name``. Keeping the questions as written is deliberate: the responses sheet is the
record of what a submitter was actually asked, and renaming fields on the way in would
quietly discard that.

The cost is that every reader has to match questions rather than look up keys, which is
what this module does in one place. Matching on substrings means light rewording of the
form -- and the form will be reworded, by whoever curates next -- does not silently blank
a field.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def find_answer(metadata: Dict[str, Any], *needles: str) -> Optional[str]:
    """First answer whose question contains all of ``needles`` (case-insensitively).

    Returns ``None`` when nothing matches or the answer is blank, so a caller can tell
    "not asked" and "asked but unanswered" apart from an empty string.
    """
    for question, answer in metadata.items():
        lowered = (question or "").lower()
        if all(needle in lowered for needle in needles):
            value = (str(answer) if answer is not None else "").strip()
            if value:
                return value
    return None


def _is_embargo_question(question: str) -> bool:
    """Is this the "may we add the records now?" question?

    The single place that identifies it, used both to READ it and to rule it out of the
    publication-stage match. Two independent descriptions of the same question would let
    one drift and reintroduce exactly the collision this prevents.
    """
    lowered = (question or "").lower()
    return "unpublished" in lowered and "add" in lowered


def stage_answer(metadata: Dict[str, Any]) -> str:
    """The submitter's own words on the publication-stage question, verbatim.

    Separate from :func:`publication_stage` because two callers need different things from
    the same answer: the ledger wants it normalized to one vocabulary, and the
    confirmation email quotes it back ("You selected ..."), where normalizing would put
    words in the submitter's mouth.

    **The embargo question is excluded here, once, for both.** "If your data are
    unpublished, may we add the records to MalAvi now?" contains "unpublished", which
    contains "published", so a bare substring match can return that question's answer
    instead -- and :func:`find_answer` returns the FIRST match in sheet order, so whether
    it does depends on where the question sits in the form. notify_submitters used to do
    this lookup itself, without the exclusion, which left a curator reordering the form one
    drag away from an email telling a submitter they had selected "Add them now, credited
    as unpublished" as their publication stage.
    """
    without_embargo = {question: answer for question, answer in metadata.items()
                       if not _is_embargo_question(question)}
    return (find_answer(without_embargo, "publication")
            or find_answer(without_embargo, "published") or "")


def publication_stage(metadata: Dict[str, Any]) -> str:
    """Pre- or post-publication, from either wording of the form question.

    The question was reworded on 2026-08-05, from "Are you submitting published or
    unpublished data?" to "Is this submission pre-publication or post-publication?".
    Neither wording contains the other's keyword -- "published" is not a substring of
    "publication" -- so matching on one alone silently blanks the field for every
    submission asked the other. Submissions already fetched carry the old question text,
    so both have to keep working, and the ANSWERS are normalized to one vocabulary here.

    **The embargo question is excluded explicitly.** "If your data are unpublished, may we
    add the records to MalAvi now?" contains "unpublished", which contains "published", so
    a bare substring match can return that question's answer instead. Today it does not,
    only because the stage question sits earlier in the form and is therefore earlier in
    metadata.json -- and a correctness that depends on question ORDER is one form edit away
    from silently blanking the field for every submission. So it is ruled out by name.
    """
    raw = stage_answer(metadata).lower()
    if raw.startswith("pre") or raw.startswith("unpublished"):
        return "pre-publication"
    if raw.startswith("post") or raw.startswith("published"):
        return "post-publication"
    return ""


def sending(metadata: Dict[str, Any]) -> str:
    """What the submitter says they are sending: names, records, or both.

    Added to the form on 2026-08-05. It is what the submitter DECLARED, not what the
    workbook turned out to contain -- screening is the authority on that -- but it is the
    only description available for a submission that has not been screened yet.
    """
    raw = (find_answer(metadata, "sending") or "").lower()
    if raw.startswith("new lineage"):
        return "names and sequences"
    if raw.startswith("records"):
        return "records for a manuscript"
    if raw.startswith("both"):
        return "names, sequences and records"
    return ""


def records_embargo(metadata: Dict[str, Any]) -> str:
    """Whether an unpublished submitter's RECORDS may go public now. Added 2026-08-09.

    ``"hold"``      wait until the submitter says the study is accepted
    ``"add-now"``   put the records in credited as "<Authors> unpubl"
    ``""``          not applicable, unanswered, or a submission predating the question

    **An empty answer means hold** -- see :func:`records_are_held`, which is where that
    default is applied rather than left to each caller to remember.
    """
    for question, answer in metadata.items():
        if not _is_embargo_question(question):
            continue
        raw = (str(answer) if answer is not None else "").strip().lower()
        if raw.startswith("hold"):
            return "hold"
        if raw.startswith("add"):
            return "add-now"
    return ""


def records_were_included(metadata: Dict[str, Any]) -> bool:
    """Did the submitter say they were sending host and geography records at all?

    False for a names-and-sequences submission, which is the case where the records are
    still to come and the submitter needs telling so.
    """
    return sending(metadata) in ("records for a manuscript", "names, sequences and records")


def records_are_held(metadata: Dict[str, Any]) -> bool:
    """Are this submission's records waiting on publication rather than on the next release?

    Two conditions, and both matter:

    * the data are unpublished -- a published study's records are never held, whatever
      was answered to the embargo question;
    * the submitter did not explicitly say "add them now".

    **The default is to hold.** Every submission fetched before 2026-08-09 predates the
    embargo question and answers it with nothing, and reading that silence as consent
    would publish unpublished records whose owners were never asked. Holding wrongly is
    undone by writing to somebody; publishing wrongly is undone by retracting from a
    release that people have already downloaded.
    """
    stage = publication_stage(metadata)
    if stage == "":
        # UNREADABLE IS HELD, not published. This returned False until 2026-08-10, so a
        # submission whose stage question was missing, reworded a third time, or answered
        # with anything the two prefix tests do not match became publishable -- and
        # blanking that field is exactly what publication_stage's own docstring warns a
        # single form edit can do. It has already been reworded once, on 2026-08-05.
        #
        # The failure directions are not symmetric, which is the whole argument: holding a
        # published study's records wrongly is undone by somebody noticing they are
        # missing and saying so, while publishing an unpublished study's records wrongly
        # is undone by retracting from a release people have already downloaded.
        return True
    if stage != "pre-publication":
        return False
    return records_embargo(metadata) != "add-now"


def submitter_from_metadata(metadata: Dict[str, Any]) -> Dict[str, str]:
    """The submitter block for a submission, from the form answers.

    The name question is matched while excluding "institution", because the form asks
    for both and a bare "name" match would otherwise return the institution for whoever
    happens to have it listed first.

    Falls back to a placeholder name rather than raising: a submission missing its name
    is still a submission a curator must see, and the missing field is something a check
    can report far more usefully than an exception during intake.
    """
    name = None
    for question, answer in metadata.items():
        lowered = (question or "").lower()
        if "name" in lowered and "institution" not in lowered:
            value = (str(answer) if answer is not None else "").strip()
            if value:
                name = value
                break

    submitter: Dict[str, str] = {"name": name or "unknown submitter"}
    email = find_answer(metadata, "email")
    if email:
        # Kept because the curator report is where a curator writes back to someone about
        # their submission. It never reaches a public feed: build_site_feeds.py asserts
        # against exactly that, and the report lives only in the gitignored intake tree.
        submitter["email"] = email
    institution = find_answer(metadata, "institution")
    if institution:
        submitter["institution"] = institution
    return submitter
