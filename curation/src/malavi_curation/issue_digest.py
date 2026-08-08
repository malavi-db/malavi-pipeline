"""The short, non-sensitive summary that goes in a GitHub issue body.

**Why this is a separate renderer and not the curator report shortened.**

GitHub emails an issue body to every watcher. That copy lands in several people's mail
providers permanently and cannot be deleted, redacted or recalled by anyone — not by a
curator, not by a maintainer, not by GitHub. So whatever goes in the body has been
published, irreversibly, to everyone watching the repository.

The curator report contains the opposite of what belongs there: submitter name and email,
unpublished sequences, host species, localities, lineage names, accessions, raw workbook
contents. Truncating it would put the first N sensitive lines in the mail rather than all
of them, which is not a fix. So this module builds the digest from an **allowlist** — a
field only appears here if it is named below — rather than by removing things from
something larger. A blacklist forgets; an allowlist has to be deliberately extended.

What the digest is *for*: telling a curator that something needs them, what kind of
attention it needs, and where to click. Everything else is one link away, behind a login.

The rule to hold onto: **check names, never check values.** "One name collision" tells a
curator to look. "RUTMIG02 is already taken" tells every mail server which lineage an
unpublished submission proposes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .checks import CHECKS, CheckRun, Outcome, Severity
from .submission_id import is_opaque

# The complete public shape of a digest. A value reaches the issue body only if it is
# produced by one of these; there is no path that copies a field through from the
# submission. Mirrors the PUBLIC_FIELDS discipline already used for the reservation feed.
DIGEST_FIELDS = (
    "submission_id",       # minted, opaque -- never the intake directory name
    "received",            # date only, no time, no submitter
    "reference",           # author-year style key ONLY, and only when published
    "state",               # workflow label
    "report_revision",
    "counts",              # blocking / warning / info / skipped / errored
    "blocking_checks",     # check IDs, never their findings
    "checklist",
    "report_url",
)

# Progress items. These are not the verdict -- see the plan. A shared checkbox has no
# actor, so it records what has been looked at, not what was decided.
CHECKLIST = (
    "Proposed names checked",
    "Sequences checked",
    "Host and locality checked",
    "Supporting material checked",
)


def _counts(run: CheckRun) -> Dict[str, int]:
    """Findings by the severity of the check that raised them, plus run outcomes.

    Counts are safe to publish: a number cannot identify a bird, a place or a person.
    """
    by_severity = {"blocking": 0, "warning": 0, "info": 0}
    for result in run.results:
        if result.outcome is not Outcome.FINDING:
            continue
        check = CHECKS.get(result.check_id)
        key = check.severity.value if check else "warning"
        by_severity[key] = by_severity.get(key, 0) + len(result.findings)

    outcomes = run.counts()
    by_severity["skipped"] = outcomes["skip"]
    by_severity["errored"] = outcomes["error"]
    by_severity["passed"] = outcomes["pass"]
    return by_severity


def _blocking_check_ids(run: CheckRun) -> List[str]:
    """Which blocking checks fired — by id, with no detail attached.

    The id is a fixed string from the registry (``name_already_in_malavi``), not a
    generated message. That distinction is what keeps a lineage name out of the mail.
    """
    out: List[str] = []
    for result in run.results:
        if result.outcome is not Outcome.FINDING:
            continue
        check = CHECKS.get(result.check_id)
        if check and check.severity is Severity.BLOCKING:
            out.append(result.check_id)
    return sorted(set(out))


def build_digest(
    submission_id: str,
    run: CheckRun,
    received: Optional[str] = None,
    reference_key: Optional[str] = None,
    state: str = "ready-for-review",
    report_revision: int = 1,
    report_url: Optional[str] = None,
    published: bool = False,
) -> Dict[str, Any]:
    """Assemble the digest as data, before any rendering.

    Args:
        submission_id: the minted, opaque identifier. Rejected if it is not one.
        run: the check run, from which only counts and check ids are taken.
        received: the date received, as ``YYYY-MM-DD``. Trimmed to a date if a full
            timestamp is passed, because the hour someone submitted is not triage
            information and does narrow down who they are.
        reference_key: a short author-year citation key, **included only when the work is
            published**. An unpublished title is the submitter's to announce, not ours.
        published: whether the work is already published.
        report_url: the link to the full report.

    Returns a dict whose keys are exactly ``DIGEST_FIELDS``.
    """
    if not is_opaque(submission_id):
        # A guard rather than a coercion: silently accepting a directory name here is
        # precisely the failure this module exists to prevent, and it would be invisible.
        raise ValueError(
            f"{submission_id!r} is not a minted submission id. The issue body must never "
            f"carry an intake directory name — it contains the submitter's name and is "
            f"mailed to every watcher.")

    return {
        "submission_id": submission_id,
        "received": (received or "")[:10] or None,
        "reference": reference_key if (published and reference_key) else None,
        "state": state,
        "report_revision": report_revision,
        "counts": _counts(run),
        "blocking_checks": _blocking_check_ids(run),
        "checklist": list(CHECKLIST),
        "report_url": report_url,
    }


def render_issue_body(digest: Dict[str, Any]) -> str:
    """Render the digest as the Markdown that becomes the issue body."""
    counts = digest["counts"]
    lines: List[str] = []

    # Machine-readable metadata, so automation never has to parse a title. Kept to the
    # opaque id and version numbers -- an HTML comment is still delivered in the email.
    lines.append("<!-- malavi-submission-id: {}\n"
                 "     report-revision: {}\n"
                 "     digest-version: 1 -->".format(
                     digest["submission_id"], digest["report_revision"]))
    lines.append("")

    if counts.get("errored"):
        lines.append("> [!WARNING]")
        lines.append("> **Some checks could not run.** The absence of a finding from those "
                     "checks means nothing. This submission has not been fully screened.")
        lines.append("")

    lines.append(f"**{digest['submission_id']}**")
    facts = []
    if digest.get("received"):
        facts.append(f"received {digest['received']}")
    if digest.get("reference"):
        facts.append(digest["reference"])
    facts.append(f"report revision {digest['report_revision']}")
    lines.append(" · ".join(facts))
    lines.append("")

    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| Blocking findings | {counts.get('blocking', 0)} |")
    lines.append(f"| Warnings | {counts.get('warning', 0)} |")
    lines.append(f"| For information | {counts.get('info', 0)} |")
    lines.append(f"| Checks skipped | {counts.get('skipped', 0)} |")
    lines.append(f"| Checks that failed to run | {counts.get('errored', 0)} |")
    lines.append(f"| Checks passed | {counts.get('passed', 0)} |")
    lines.append("")

    if digest["blocking_checks"]:
        lines.append("**Blocking checks that fired** — details are in the full report:")
        for check_id in digest["blocking_checks"]:
            check = CHECKS.get(check_id)
            # The registry's fixed title and assertion. Both are static strings written
            # by us; neither contains anything from the submission.
            title = check.title if check else check_id
            lines.append(f"- `{check_id}` — {title}")
        lines.append("")

    if digest.get("report_url"):
        lines.append(f"**[Open the full report]({digest['report_url']})** — everything "
                     "submitted, every finding, and the sequences. Sign in with the Google "
                     "account you were given access with.")
    else:
        lines.append("_The full report is not available yet._")
    lines.append("")

    lines.append("### Review progress")
    lines.append("")
    for item in digest["checklist"]:
        lines.append(f"- [ ] {item}")
    lines.append("")
    lines.append("_Ticking these records what has been looked at. They do not approve "
                 "anything — the verdict is recorded separately, so that it carries who "
                 "decided and which revision they decided about._")

    return "\n".join(lines)


def render_issue_title(digest: Dict[str, Any]) -> str:
    """The issue title. Opaque id plus a count; nothing else fits the allowlist.

    Titles are the most-copied part of an issue — they appear in notification subject
    lines, in lists, in search results and in anything integrating with the repository.
    """
    blocking = digest["counts"].get("blocking", 0)
    suffix = f" — {blocking} blocking" if blocking else ""
    return f"{digest['submission_id']}{suffix}"


def sensitive_values(submission: Dict[str, Any]) -> List[str]:
    """Every value from a submission that must never appear in a digest.

    Exists so the guard test can assert against real content rather than a guessed list.
    Collecting it here, next to the renderer, means a new sensitive field added to the
    submission is caught by the existing test rather than needing a new one.
    """
    values: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            values.append(value.strip())

    submitter = submission.get("submitter") or {}
    for key in ("name", "email", "institution"):
        add(submitter.get(key))

    reference = submission.get("reference") or {}
    add(reference.get("title"))
    add(reference.get("doi"))

    for record in (submission.get("records") or []) + (submission.get("vectors") or []):
        for key in ("lineage_name", "host_species", "vector_species", "country", "site",
                    "notes", "coordinates"):
            add(record.get(key))

    for entry in submission.get("proposed_lineages") or []:
        add(entry.get("lineage_name"))
        add(entry.get("host_species"))
        for accession in entry.get("accessions") or []:
            add(accession)

    for sequence in submission.get("sequences") or []:
        add(sequence.get("lineage_name"))
        add(sequence.get("sequence"))
        add(sequence.get("sequence_clean"))

    for accession in submission.get("accessions") or []:
        add(accession)

    provenance = submission.get("provenance") or {}
    add(provenance.get("workbook"))

    return values
