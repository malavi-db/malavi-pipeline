"""Read curator verdict form responses, and build the links that produce good ones.

Two halves of the same join. :func:`prefill_url` builds the link a curator clicks, which
carries the submission id and the revision so neither is typed. :func:`parse_row` reads what
comes back out of the responses sheet and turns it into something
:mod:`malavi_curation.ledger` can act on.

**Nothing here decides anything.** A parsed response is a *request*: the ledger still checks
that the address belongs to an active curator, that a hold blocks, that whoever typed a
revision may not approve it. The form link is unlisted rather than private, so assume anyone
can submit through it and let the registry make that harmless.

**Nothing here raises on bad input either.** A curator can type anything into a text field,
and a response that cannot be understood must be *filed* rather than thrown — both because
discarding it would silently lose a real curator's decision, and because one unparseable row
must not abort a fetch that has fifty good rows behind it. Every failure returns a
:class:`Rejected` carrying the reason.

The column names below are the real ones, read from a live test response on 2026-08-06 —
Google Forms derives them from the question titles, so they are what
``curation/apps_script/create_verdict_form.gs`` asks. Change a question title there and you
must change the matching constant here; the test suite pins them against the script.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

# --- Column names, exactly as Google Forms writes them --------------------------------
COL_TIMESTAMP = "Timestamp"
COL_EMAIL = "Email Address"
COL_SUBMISSION = "Submission id"
COL_REVISION = "Revision"
COL_ACTION = "What are you recording?"

COL_VERDICT = "Your verdict on this revision"
COL_WHY = "Why?"
COL_CHECKED = "What did you check?"

COL_HOLD_ID = "Which hold are you clearing?"
COL_CONSULTED = "Who did you consult?"
COL_CONSULTED_ON = "When did that conversation happen?"
COL_CONSULTED_HOW = "How?"
COL_RESOLVED = "What was resolved?"

COL_CORRECTION_KIND = "What kind of correction is this?"
COL_CONFIRMED_BY = "Who confirmed it?"
COL_CONFIRMED_ON = "When did you hear back?"
COL_CHANGE = "What should change?"

COL_RETRACT_ID = "Which flag are you withdrawing?"
COL_RETRACT_WHY = "What changed your mind?"

COL_CORRECTION_ID = "Which correction are you approving?"
COL_DISCUSSED_WITH = "Who did you discuss it with?"
# NOT "What was resolved?", which is what this question was called until 2026-08-14 -- the
# same title as the override page's question above. Google writes a question title verbatim
# as the response sheet's column header, so two identically titled questions produce two
# identically named columns, and csv.DictReader keeps the LAST one. Column order follows
# item order, so this page's column (item 24) silently answered for the override page's
# (item 12): a lead cleared another curator's hold, typed the justification the form calls
# required, and it was stored as "". No test could see it -- the fixtures build a row dict,
# and a dict cannot hold one key twice.
COL_CONCLUDED = "What did the discussion conclude?"

COL_CLOSE_REASON = "Why is it being closed?"
COL_CLOSE_NOTE = "Anything to add?"

#: Every column this module reads out of a response row.
#:
#: Built by collecting the constants above rather than by listing them again, so a column
#: added to the parser cannot be left out of it. Used by fetch_verdicts.duplicate_columns
#: to decide which repeated headers in the responses sheet actually matter: the sheet
#: collects an orphan column from every hand edit to the form and most are harmless, but a
#: repeat of one of THESE is read wrong rather than ignored -- csv.DictReader keeps the
#: rightmost, which is usually the dead one.
COLUMNS_READ = frozenset(
    value for name, value in list(globals().items())
    if name.startswith("COL_") and isinstance(value, str)
)

# --- Answer values, as the form offers them -------------------------------------------
ACTION_VERDICT = "Record a verdict on a submission"
ACTION_OVERRIDE = "Clear another curator’s hold (lead curators only)"
ACTION_CORRECTION = "Submit a correction on behalf of a submitter"

# The two actions below close paths the curator instructions already promise. Withdrawing
# your own flag is the *first* of the two ways that document says a block ends ("this
# BLOCKS the submission until you withdraw it or a lead curator clears it"); only the
# second had a route. And a correction is applied "after the lead curator approves", which
# is a real gate in the ledger — :func:`ledger.approve_correction` — that no interface
# could reach, so every correction stopped at proposed.
#
# Both are separate actions rather than options inside another branch, because each has a
# different actor and a different precondition, and a form branch that sometimes needs a
# lead and sometimes does not is one a curator has to be told how to read.
ACTION_RETRACT = "Withdraw a flag you placed yourself"
ACTION_APPROVE_CORRECTION = "Approve a correction (lead curators only)"

# The act that actually finishes a rejected submission. "Reject" above lands on `held`, on
# purpose, so that a rejection gets a second look; nothing then moved it any further, so a
# rejected submission stayed live forever -- holding its reserved lineage names, never
# telling the submitter anything. A maintainer CLI closed that gap on 2026-08-13; this is
# the curator's own route to it, so that ending a submission does not require somebody
# with a shell.
ACTION_CLOSE = "Close a submission for good (lead curators only)"

# Why it is being closed. A closed vocabulary, mapped from what a lead reads to what the
# decision record stores, for the same reason VERDICT_LABELS exists: "out_of_scope" tells a
# maintainer what happened and "Not avian haemosporidian data" tells a curator what they
# are choosing. The stored side is ledger.DECLINE_REASON_CODES, and a test holds the two
# in agreement.
CLOSE_REASONS = {
    "It is already in MalAvi": "duplicate",
    "Not avian haemosporidian data": "out_of_scope",
    "A flag on it was never answered": "unresolved_objection",
    "The records could not be checked against the source": "data_not_verifiable",
    "Another submission replaces it": "superseded",
}

# The form speaks to curators; the ledger speaks in its own vocabulary. Mapped here rather
# than by making the form say "approve", because "Flag for further review" tells a curator
# what the button does and "hold" does not.
VERDICT_LABELS = {
    "Accept": "approve",
    "Flag for further review": "hold",
    "Reject": "decline",
}

# The correction branch's two answers, and what each licenses. Another curator can confirm
# a judgment fix; only the authors can confirm a change to what the data claims.
CORRECTION_AUTHORITY = {
    "Judgment — confirmed with another curator": "curator",
    "Data — confirmed with the authors": "author",
}

# Matches an opaque submission identifier. A prefilled link always supplies one; a curator
# who reached the form some other way can type anything, and did in the first live test
# ("testing1,2,3").
SUBMISSION_ID_RE = re.compile(r"^MALAVI-SUB-\d{4}-\d{6}$")


@dataclass
class Rejected:
    """A response that could not be turned into an action, and why.

    Filed rather than raised. The reason is written for a maintainer reading a log, not for
    a curator: by the time anyone sees it the curator has already submitted and gone.
    """

    reason: str
    row: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return False


@dataclass
class Action:
    """A parsed, well-formed request to change one submission's review state."""

    kind: str                     # "verdict" | "override" | "correction" | "close" | …
    submission_id: str
    address: str                  # the Google-verified address; resolved by the registry
    at: str                       # ISO 8601 UTC
    revision: Optional[int] = None
    # verdict
    verdict: str = ""
    reason_text: str = ""
    # close: the closed-vocabulary disposition code, which reaches decisions.json. Kept
    # apart from reason_text, which is free text and stays in the gitignored ledger.
    reason_code: str = ""
    checked: List[str] = field(default_factory=list)
    # override
    hold_id: str = ""
    consulted: List[str] = field(default_factory=list)
    consulted_on: str = ""
    consulted_how: str = ""
    # correction
    authority: str = ""
    change: str = ""
    # retraction and correction approval. A separate slot from `hold_id` on purpose: a
    # correction id (C1) and a verdict id (V1) are drawn from different sequences, and one
    # field carrying either would let a caller pass a correction id to something that
    # resolves verdicts and get whichever record happened to share the number.
    target_id: str = ""

    @property
    def ok(self) -> bool:
        return True


def _text(value: Any) -> str:
    """A cell as trimmed text. Google hands back None, str, int, float and datetime."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip()


def _timestamp(value: Any, sheet_timezone: timezone = timezone.utc) -> Optional[str]:
    """One form timestamp as ISO 8601 UTC, or None if it cannot be read.

    **The timezone is the subtle part.** Google stamps a response in the *spreadsheet's*
    timezone and records no offset, so a bare "2026-08-06 13:21:26" is ambiguous by up to a
    day. That ambiguity lands directly on the 24-hour publish hold and the 60-day
    awaiting-submitter timeout, which is why the ledger refuses timestamps it cannot read
    rather than guessing. Set the responses sheet to UTC (File > Settings > Time zone) and
    this assumption is correct by construction; ``sheet_timezone`` exists so a sheet that is
    set to something else can still be read honestly.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=sheet_timezone)
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    text = _text(value)
    if not text:
        return None
    # ISO first, then the two shapes Google exports to CSV.
    for parse in (
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        lambda s: datetime.strptime(s, "%m/%d/%Y %H:%M:%S"),
        lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            moment = parse(text)
        except (ValueError, TypeError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=sheet_timezone)
        return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return None


def _checkbox(value: Any) -> List[str]:
    """Split a Forms checkbox answer.

    Forms joins selected options with ", " and does not escape anything, so an option
    containing a comma would be unsplittable. None of ours does — that is a property of the
    form definition worth keeping, and the test suite pins it.
    """
    text = _text(value)
    return [part.strip() for part in text.split(",") if part.strip()] if text else []


def parse_row(row: Dict[str, Any],
              sheet_timezone: timezone = timezone.utc) -> Any:
    """Turn one response-sheet row into an :class:`Action` or a :class:`Rejected`."""
    address = _text(row.get(COL_EMAIL))
    if not address:
        # Without a verified address the response cannot be attributed to anybody, which
        # makes it unusable regardless of what it says. The likeliest cause is the form's
        # email collection having been switched off.
        return Rejected("no email address on the response; is the form still collecting "
                        "verified addresses?", row)

    at = _timestamp(row.get(COL_TIMESTAMP), sheet_timezone)
    if at is None:
        return Rejected(f"unreadable timestamp {row.get(COL_TIMESTAMP)!r}", row)

    submission_id = _text(row.get(COL_SUBMISSION))
    if not SUBMISSION_ID_RE.match(submission_id):
        # A prefilled link always supplies a well-formed id. Anything else means the curator
        # reached the form another way and typed one, and acting on a guess about which
        # submission they meant would attach a decision to somebody else's work.
        return Rejected(
            f"submission id {submission_id!r} is not an opaque identifier; the curator "
            f"did not use a prefilled link", row)

    action = _text(row.get(COL_ACTION))

    if action == ACTION_VERDICT:
        return _parse_verdict(row, submission_id, address, at)
    if action == ACTION_OVERRIDE:
        return _parse_override(row, submission_id, address, at)
    if action == ACTION_CORRECTION:
        return _parse_correction(row, submission_id, address, at)
    if action == ACTION_RETRACT:
        return _parse_retraction(row, submission_id, address, at)
    if action == ACTION_APPROVE_CORRECTION:
        return _parse_correction_approval(row, submission_id, address, at)
    if action == ACTION_CLOSE:
        return _parse_close(row, submission_id, address, at)
    return Rejected(f"unknown action {action!r}", row)


# A verdict id as the report prints it (V1, V2, ...), and a correction id likewise (C1).
# Matched rather than passed through so that a curator typing "hold 1" or "the second one"
# is refused here, where the reason can be recorded, rather than deep inside the ledger
# where the failure is an id lookup that finds nothing.
VERDICT_ID_RE = re.compile(r"^V\d+$")
CORRECTION_ID_RE = re.compile(r"^C\d+$")


def _revision(row: Dict[str, Any]) -> Optional[int]:
    """The revision the curator was shown, or None if it is not a number."""
    text = _text(row.get(COL_REVISION))
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _parse_verdict(row, submission_id, address, at):
    label = _text(row.get(COL_VERDICT))
    verdict = VERDICT_LABELS.get(label)
    if verdict is None:
        return Rejected(f"unknown verdict {label!r}", row)

    revision = _revision(row)
    if revision is None:
        return Rejected(f"revision {_text(row.get(COL_REVISION))!r} is not a number", row)

    reason = _text(row.get(COL_WHY))
    if verdict in ("hold", "decline") and not reason:
        # The form marks this required, so an empty one means the form was edited. Refused
        # here as well, because an objection nobody can read cannot be answered by the
        # submitter or weighed by a lead.
        return Rejected(f"a {verdict!r} carries no written reasoning", row)

    return Action(kind="verdict", submission_id=submission_id, address=address, at=at,
                  revision=revision, verdict=verdict, reason_text=reason,
                  checked=_checkbox(row.get(COL_CHECKED)))


def _parse_override(row, submission_id, address, at):
    hold_id = _text(row.get(COL_HOLD_ID))
    consulted = _text(row.get(COL_CONSULTED))
    consulted_on = _text(row.get(COL_CONSULTED_ON))
    consulted_how = _text(row.get(COL_CONSULTED_HOW))

    missing = [name for name, value in (
        ("which hold", hold_id), ("who was consulted", consulted),
        ("when", consulted_on), ("how", consulted_how)) if not value]
    if missing:
        return Rejected(f"override is missing {', '.join(missing)}", row)

    return Action(kind="override", submission_id=submission_id, address=address, at=at,
                  revision=_revision(row), hold_id=hold_id,
                  consulted=[part.strip() for part in re.split(r"[,;]", consulted)
                             if part.strip()],
                  consulted_on=consulted_on, consulted_how=consulted_how,
                  reason_text=_text(row.get(COL_RESOLVED)))


def _parse_correction(row, submission_id, address, at):
    authority = CORRECTION_AUTHORITY.get(_text(row.get(COL_CORRECTION_KIND)))
    if authority is None:
        return Rejected(
            f"unknown correction kind {_text(row.get(COL_CORRECTION_KIND))!r}", row)

    change = _text(row.get(COL_CHANGE))
    if not change:
        return Rejected("correction does not say what should change", row)

    # The "must accompany a flag" rule is NOT checked here, and the form no longer asks
    # about it. ledger.record_correction refuses a correction when blocking_holds(entry) is
    # empty -- from the ledger's own record of what is flagged, rather than from what the
    # curator remembered to tick. Checking here as well was a second copy of a rule that has
    # to hold at the write regardless of what reached it, and it turned an honest "no" on
    # the form into a silently discarded correction. See _parse_correction_approval, which
    # declines to duplicate the ledger's rules for the same reason.

    confirmed_by = _text(row.get(COL_CONFIRMED_BY))
    if not confirmed_by:
        # The distinction between "a curator agreed" and "the authors agreed" is the whole
        # basis on which a correction is allowed, so it cannot be left blank.
        return Rejected("correction does not name who confirmed it", row)

    return Action(kind="correction", submission_id=submission_id, address=address, at=at,
                  revision=_revision(row), authority=authority,
                  consulted=[confirmed_by], consulted_on=_text(row.get(COL_CONFIRMED_ON)),
                  change=change)


def _parse_retraction(row, submission_id, address, at):
    """A curator withdrawing an objection they placed themselves.

    Needs no lead and no consultation record: nobody's judgment is being set aside but the
    curator's own. What it does need is *which* objection, because a curator may have more
    than one standing and clearing the wrong one would leave a live block looking resolved.
    """
    target = _text(row.get(COL_RETRACT_ID)).upper()
    if not VERDICT_ID_RE.match(target):
        return Rejected(
            f"{target!r} is not a flag id; the report prints them as V1, V2 and so on. "
            f"Withdrawing a guess would clear whichever objection happened to match", row)

    return Action(kind="retraction", submission_id=submission_id, address=address, at=at,
                  revision=_revision(row), target_id=target,
                  reason_text=_text(row.get(COL_RETRACT_WHY)))


def _parse_correction_approval(row, submission_id, address, at):
    """A lead agreeing that a proposed correction may be applied.

    The ledger enforces that the responder is a lead and that they are not the person who
    proposed it; neither is checked here, because a check in the parser would be a second
    copy of a rule that has to hold at the write regardless of what reached it.
    """
    target = _text(row.get(COL_CORRECTION_ID)).upper()
    if not CORRECTION_ID_RE.match(target):
        return Rejected(
            f"{target!r} is not a correction id; the report prints them as C1, C2 and so "
            f"on", row)

    discussed = _text(row.get(COL_DISCUSSED_WITH))
    if not discussed:
        # A correction changes what MalAvi publishes about somebody else's study. The
        # instructions promise the lead approves "after discussing with you and potentially
        # the author"; an approval with nobody named is that promise unkept.
        return Rejected("a correction approval must name who was consulted", row)

    return Action(kind="correction_approval", submission_id=submission_id, address=address,
                  at=at, revision=_revision(row), target_id=target,
                  consulted=[part.strip() for part in re.split(r"[,;]", discussed)
                             if part.strip()],
                  # No fallback to COL_RESOLVED if this column is absent. A fallback would
                  # read the override page's column instead and quietly restore the bug it
                  # was renamed to fix -- and it would hide whether the live form has
                  # actually been renamed, which nothing here can otherwise tell.
                  reason_text=_text(row.get(COL_CONCLUDED)))


def _parse_close(row, submission_id, address, at):
    """A lead ending a submission MalAvi will not include.

    That the responder is a lead, and that the submission is in a state it can be closed
    from, are both checked by :func:`ledger.decline` at the write. Not here: a rule copied
    into the parser is a rule that has to hold in two places, and the one that matters is
    the one nearest the write.

    What *is* checked here is the reason, because the parser is the last place that knows
    what the curator was shown. It reaches ``data/decisions.json``, so it is a fixed list
    rather than a sentence somebody typed.
    """
    label = _text(row.get(COL_CLOSE_REASON))
    reason = CLOSE_REASONS.get(label)
    if reason is None:
        return Rejected(
            f"unknown closing reason {label!r}; the form offers "
            f"{', '.join(sorted(CLOSE_REASONS))}", row)

    return Action(kind="close", submission_id=submission_id, address=address, at=at,
                  revision=_revision(row), reason_code=reason,
                  reason_text=_text(row.get(COL_CLOSE_NOTE)))


def prefill_url(form_url: str, entries: Dict[str, Any], submission_id: str,
                revision: int) -> str:
    """The link a curator clicks, carrying the submission and the revision.

    This is what makes an approval bind to the version of the report the curator actually
    read. Without it they type both values, and the first live test of this form produced
    a submission id of "testing1,2,3" — which is exactly what typing a stable identifier by
    hand looks like in practice.

    Note the prefilled fields remain *editable* by the responder; Forms offers no way to
    lock them. So this improves the common case and :func:`parse_row` still refuses
    anything that is not a well-formed identifier.
    """
    base = form_url.split("?")[0]
    parameters = [
        f"entry.{entries['submission_id']}={quote(submission_id)}",
        f"entry.{entries['revision']}={quote(str(revision))}",
    ]
    return f"{base}?usp=pp_url&" + "&".join(parameters)
