#!/usr/bin/env python3
# @title Apply curator corrections to a submission
# @purpose Turn corrections recorded on the verdict form into a new revision of a
#   submission, so a curator's fix reaches the data without a curator touching a file.
# @why A correction was capturable but not applicable: the form recorded what should
#   change and who confirmed it, and then a human had to edit a workbook by hand. That is
#   the step where a correction gets forgotten, half-applied, or applied without the
#   record of who authorised it.
# @input curation/intake/submissions/<dir>/  (submission.json, review_ledger.json)
# @input the verdict responses sheet, via curation/src/malavi_curation/verdicts.py
# @output a new revision recorded in the ledger, with the correction and its authority
# @program python3
# @critical-var DRY_RUN_DEFAULT
"""Apply corrections a curator recorded on the verdict form.

**What a correction is here.** A curator reading a report sees something wrong that they
can settle — a host synonym, a country spelling, a prevalence figure the paper states
differently. Rather than sending the submission back and waiting, they flag it and describe
the fix on the same form. This turns that description into a new revision.

**Why a correction always arrives with a flag.** The form refuses a correction that is not
accompanied by a flag, and so does this. A fix and an acceptance cannot be the same act:
accepting *while* correcting would approve a version that does not exist yet, and would
have this script apply a change nobody has reviewed in its final form. So the sequence is
always flag, correct, re-screen, then accept the corrected report.

**What this does not do.** It does not decide anything and it does not edit the submitted
workbook. The workbook is what the submitter sent and stays exactly that — the appendix in
every report is built from it, and rewriting it would destroy the only record of what was
actually submitted. A correction is recorded *over* it as a new revision, so both the
original and the corrected reading survive, which is what lets a curator years later see
that a value was changed and on whose authority.

**Revisions clear approvals, deliberately.** Applying a correction bumps the revision,
which clears every standing approval including from curators who were perfectly happy.
They approved a different version. The ledger enforces that; this script only triggers it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation.config import load_config, repo_root      # noqa: E402
from malavi_curation import ledger as ledger_mod               # noqa: E402
from malavi_curation.verdicts import parse_row                 # noqa: E402

# Writing nothing is the default. A correction changes what MalAvi will publish about
# somebody's study, so the operator should see the list before it is applied.
DRY_RUN_DEFAULT = True


def corrections_from(rows: List[Dict[str, Any]]) -> List[Any]:
    """Every well-formed correction in a batch of verdict-form responses."""
    out = []
    for row in rows:
        parsed = parse_row(row)
        if getattr(parsed, "ok", False) and parsed.kind == "correction":
            out.append(parsed)
    return out


def apply_correction(entries: Dict[str, Any], action: Any,
                     registry_path=None) -> str:
    """Record one correction as a new revision. Returns a line describing what happened."""
    entry = entries.get(action.submission_id)
    if entry is None:
        return f"  SKIPPED {action.submission_id}: no such submission in the ledger"

    if not ledger_mod.blocking_holds(entry):
        # The form asks, and the parser refuses without it; this is the third check,
        # against the ledger itself, because the form's answer is a curator's assertion
        # and the ledger is the fact.
        return (f"  REFUSED {action.submission_id}: no flag is standing on it. A "
                f"correction must accompany a flag, so that nobody is approving a version "
                f"that does not exist yet.")

    approved = ledger_mod.pending_corrections(entry)
    if not approved:
        return (f"  WAITING {action.submission_id}: recorded, but no lead curator has "
                f"approved it yet. A correction changes what MalAvi will say about "
                f"somebody else's study, so it is applied on a lead's approval after "
                f"discussion with whoever raised it and, where the data itself changes, "
                f"with the authors.")

    revision = ledger_mod.bump_revision(
        entry,
        reason=action.change,
        at=action.at,
        revised_by=action.address,
        authority=action.authority,
        consulted=action.consulted,
        registry_path=registry_path)

    for correction in approved:
        correction.applied_at = revision.at
    who = "the authors" if action.authority == "author" else "another curator"
    return (f"  {action.submission_id}: now revision {revision.number}, corrected by "
            f"{revision.revised_by} on the authority of {who} "
            f"({', '.join(action.consulted) or 'unnamed'}). Every standing approval was "
            f"cleared; the submission needs re-screening and a fresh decision.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the corrections (default is to list them only)")
    args = parser.parse_args(argv)

    cfg = load_config()
    inbox = repo_root() / (cfg.get("submissions") or {}).get(
        "inbox_dir", "curation/intake/submissions")

    print("== malavi_rebuild :: apply_corrections ==")
    print("Reading corrections recorded on the verdict form.\n")
    print("NOTE: reading the verdict sheet is not wired up yet — see GOOGLE_ACCESS.md and")
    print("      the fetch job. This script applies corrections once they can be read;")
    print("      the rules it enforces are the part that matters and they are tested.\n")

    rows: List[Dict[str, Any]] = []          # the fetch step goes here
    actions = corrections_from(rows)
    if not actions:
        print("No corrections to apply.")
        return 0

    with ledger_mod.open_ledger(inbox, write=args.apply) as entries:
        for action in actions:
            print(apply_correction(entries, action))

    if not args.apply:
        print("\n[dry-run] nothing was written. Re-run with --apply to record these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
