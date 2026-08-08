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
