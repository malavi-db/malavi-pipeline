#!/usr/bin/env python3
# @title Apply curator corrections to a submission
# @purpose Turn corrections a lead curator has approved into a new revision of the
#   submission, so a curator's fix reaches the data without a curator touching a file.
# @why A correction was capturable but not applicable: the form recorded what should
#   change and who confirmed it, a lead approved it, and then a human had to edit a
#   workbook by hand. That is the step where a correction gets forgotten, half-applied, or
#   applied without the record of who authorized it.
# @input curation/intake/submissions/review_ledger.json (via malavi_curation.ledger)
# @input config/curators.yml (resolves the proposer's id back to an address)
# @output a new revision recorded in the ledger, with the correction and its authority
# @program python3
# @critical-var DRY_RUN_DEFAULT
"""Apply the corrections a lead curator has approved.

**What a correction is here.** A curator reading a report sees something wrong that they
can settle — a host synonym, a country spelling, a prevalence figure the paper states
differently. Rather than sending the submission back and waiting, they flag it and describe
the fix on the same form. This turns that description into a new revision.

**Why this reads the ledger and not the verdict sheet.** The correction is already in the
ledger by the time this program has anything to do: ``fetch_verdicts.py`` parses the
curator's response and calls :func:`ledger.record_correction`, and a lead's approval
arrives as a second response and calls :func:`ledger.approve_correction`. Reading the sheet
again here would mean two programs racing to interpret the same rows, two applied-ledgers
to keep in agreement, and a correction that could be applied twice if their idempotency
ever disagreed. The ledger already answers the only question this program asks — which
corrections are approved and not yet applied — and :attr:`Correction.applied_at` is the
record that they have been.

**Why a correction always arrives with a flag.** The form refuses a correction that is not
accompanied by a flag, and so does :func:`ledger.record_correction`, and so does this. A
fix and an acceptance cannot be the same act: accepting *while* correcting would approve a
version that does not exist yet, and would have this script apply a change nobody has
reviewed in its final form. So the sequence is always flag, correct, re-screen, then accept
the corrected report.

**What this does not do.** It does not decide anything and it does not edit the submitted
workbook. The workbook is what the submitter sent and stays exactly that — the appendix in
every report is built from it, and rewriting it would destroy the only record of what was
actually submitted. A correction is recorded *over* it as a new revision, so both the
original and the corrected reading survive, which is what lets a curator years later see
that a value was changed and on whose authority.

**Revisions clear approvals, deliberately.** Applying a correction bumps the revision,
which clears every standing approval including from curators who were perfectly happy.
They approved a different version. The ledger enforces that; this script only triggers it.

**One revision per correction, not one per submission.** A revision carries a single
authority, a single author and a single list of who was consulted. Two corrections on one
submission can disagree on all three — one confirmed with the authors, one settled between
curators — so merging them into one revision would have to misreport at least one of them.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation.config import load_config, repo_root      # noqa: E402
from malavi_curation import curators as curators_mod           # noqa: E402
from malavi_curation import ledger as ledger_mod               # noqa: E402

# Writing nothing is the default. A correction changes what MalAvi will publish about
# somebody's study, so the operator should see the list before it is applied.
DRY_RUN_DEFAULT = True


def pending_work(entries: Dict[str, Any],
                 only: str = "") -> List[Tuple[Any, Any]]:
    """Every ``(entry, correction)`` pair that a lead has approved and nobody has applied.

    Returned in ledger order and, within a submission, in the order the corrections were
    proposed — which is also the order their ids run (C1, C2, …). That ordering is what
    makes the revision numbers read sensibly afterwards: revision 3 is the C1 fix and
    revision 4 is the C2 fix, rather than an order that depends on a dictionary's mood.
    """
    work: List[Tuple[Any, Any]] = []
    for submission_id in sorted(entries):
        if only and submission_id != only:
            continue
        entry = entries[submission_id]
        # pending_corrections() is the ledger's own definition of "approved and not yet a
        # revision". Re-deriving it here would give this program a second opinion about
        # what is ready, which is how the two would eventually drift apart.
        for correction in ledger_mod.pending_corrections(entry):
            work.append((entry, correction))
    return work


def awaiting_approval(entries: Dict[str, Any], only: str = "") -> List[Tuple[str, Any]]:
    """Corrections proposed but not yet approved by a lead — reported, never applied.

    Shown because the operator's real question is "what is stuck", and a correction sitting
    unapproved for a fortnight is invisible otherwise: it is not in the applied list, it is
    not an error, and nothing else in the system mentions it.
    """
    waiting: List[Tuple[str, Any]] = []
    for submission_id in sorted(entries):
        if only and submission_id != only:
            continue
        for correction in entries[submission_id].corrections:
            if not correction.approved and not correction.applied:
                waiting.append((submission_id, correction))
    return waiting


def address_of(curator_id: str, registry: Dict[str, Any]) -> str:
    """The email address for a curator id, or ``""`` if the registry no longer has them.

    :func:`ledger.bump_revision` takes an *address* and resolves it, on purpose — passing
    it a raw id silently disabled the self-approval rule once already. The ledger stores
    the proposer as an id, so the round trip happens here, in one place, where a curator
    who has since left the registry is a reportable outcome rather than a stack trace.
    """
    curator = registry.get(curator_id)
    return curator.email if curator is not None else ""


def apply_correction(entry: Any, correction: Any, registry: Dict[str, Any],
                     at: Optional[str] = None,
                     registry_path: Optional[Path] = None) -> str:
    """Record one approved correction as a new revision. Returns a line describing it.

    Never raises for anything a curator did or a registry says. Every refusal below is a
    printed outcome, because one unusable correction must not stop the good ones behind it
    — and because the corrections that fail here are exactly the ones a maintainer needs to
    read about rather than find in a traceback.
    """
    if not ledger_mod.blocking_holds(entry):
        # The third check of the same rule, after the form's and record_correction's. It is
        # repeated here because a flag can be retracted or overridden in the days between
        # the correction being approved and this program running, and at that point the
        # submission is back in normal review: applying the change now would revise a
        # version that a curator may already have accepted.
        return (f"  REFUSED {entry.submission_id} {correction.id}: no flag is standing on "
                f"it any more. A correction is applied against a flag, so that nobody is "
                f"approving a version that does not exist yet. Re-flag it, or withdraw the "
                f"correction.")

    proposer = address_of(correction.by, registry)
    if not proposer:
        return (f"  SKIPPED {entry.submission_id} {correction.id}: it was proposed by "
                f"{correction.by!r}, who is not in config/curators.yml. The revision would "
                f"have nobody accountable for it.")

    try:
        revision = ledger_mod.bump_revision(
            entry,
            reason=correction.change,
            at=at,
            revised_by=proposer,
            authority=correction.authority,
            consulted=correction.consulted,
            registry_path=registry_path)
    except ledger_mod.LedgerError as exc:
        # Most likely the proposer has been deactivated since they wrote the correction.
        return f"  REFUSED {entry.submission_id} {correction.id}: {exc}"

    # Only now, and only this one. Marking every approved correction applied from a single
    # revision was the shape of the original stub, and it would have silently swallowed the
    # second and third corrections on a submission — recorded as done, never applied.
    correction.applied_at = revision.at

    who = "the authors" if correction.authority == "author" else "another curator"
    consulted = ", ".join(correction.consulted) or "unnamed"
    return (f"  {entry.submission_id} {correction.id}: now revision {revision.number}, "
            f"corrected by {revision.revised_by} on the authority of {who} ({consulted}). "
            f"Every standing approval was cleared; the submission needs re-screening and a "
            f"fresh decision.\n"
            f"      change: {correction.change}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the corrections (default is to list them only)")
    parser.add_argument("--submission", default="",
                        help="apply only this submission's corrections")
    arguments = parser.parse_args(argv)

    configuration = load_config()
    inbox = repo_root() / (configuration.get("submissions") or {}).get(
        "inbox_dir", "curation/intake/submissions")

    print("== malavi_rebuild :: apply_corrections ==")
    print("Reading approved corrections from the review ledger.\n")

    registry = curators_mod.load_registry()

    # write=False on a dry run means nothing is saved even though the entries below are
    # mutated in memory, which is what lets the dry run print the revision numbers the real
    # run would produce rather than a guess at them.
    with ledger_mod.open_ledger(inbox, write=arguments.apply) as entries:
        work = pending_work(entries, arguments.submission)
        waiting = awaiting_approval(entries, arguments.submission)

        if not work and not waiting:
            print("No corrections are waiting.")
            return 0

        if work:
            print(f"{len(work)} approved correction(s) to apply:")
            for entry, correction in work:
                print(apply_correction(entry, correction, registry,
                                       registry_path=curators_mod.registry_path()))

        if waiting:
            print(f"\n{len(waiting)} correction(s) not applied — no lead has approved them "
                  f"yet.\nA correction changes what MalAvi will say about somebody else's "
                  f"study, so it is\napplied on a lead's approval, after discussion with "
                  f"whoever raised it and, where\nthe data itself changes, with the "
                  f"authors.")
            for submission_id, correction in waiting:
                print(f"  {submission_id} {correction.id}: proposed by {correction.by} "
                      f"on {correction.at} — {correction.change}")

    if work and not arguments.apply:
        print("\n[dry-run] nothing was written. Re-run with --apply to record these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
