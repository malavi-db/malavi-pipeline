#!/usr/bin/env python3
# @title Build the website's submission queue and contributor board
# @purpose Turn fetched community submissions into the two JSON files the site
#   reads: the public queue of what is awaiting curation, and the contributor
#   board.
# @why The queue and contributor pages were static placeholders. They must be
#   generated from real submissions so they stay true without anyone editing
#   HTML, exactly as every other number on the site is.
# @input curation/intake/submissions/<dir>/metadata.json
# @input curation/intake/submissions/<dir>/screen.json
# @output docs/assets/data/queue.json
# @output docs/assets/data/contributors.json
# @program python3
# @critical-var LEADERBOARD_CONSENT_YES
# =============================================================================
# Privacy
# -------
# Email addresses are the join key and NEVER leave this script: they identify a
# contributor across submissions, and only the display name and institution are
# written out. A contributor appears on the board only if they answered yes to
# the leaderboard question on the form; the default is exclusion.
# =============================================================================
"""Generate queue.json and contributors.json from fetched submissions."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import form_metadata  # noqa: E402
from malavi_curation.config import load_config, repo_root  # noqa: E402
from malavi_curation.feeds import write_feed  # noqa: E402
from malavi_curation.submission_id import (  # noqa: E402
    ID_PATTERN, load_ledger,
)

LEADERBOARD_CONSENT_YES = "yes"


def _find(meta: Dict[str, str], *needles: str) -> Optional[str]:
    """Fetch a form answer by matching words in the question text.

    Matching on substrings rather than the exact question means light rewording
    of the form does not silently blank a field.
    """
    for question, answer in meta.items():
        q = (question or "").lower()
        if all(n in q for n in needles):
            v = (answer or "").strip()
            if v:
                return v
    return None


# The three form-answer parsers live in malavi_curation.form_metadata, not here.
#
# They were here first, and then the submitter notification emails needed the same three
# answers. Two copies of "what did this person actually select?" is the shape of bug this
# project has already had once -- three different parsers for submissions.exclude, one of
# which raised on a documented-valid entry. So there is one implementation and these are
# thin delegates, kept because the feed code and its tests read better with local names.
def _publication_stage(meta: Dict[str, str]) -> str:
    """Pre- or post-publication. See form_metadata.publication_stage."""
    return form_metadata.publication_stage(meta)


def _records_embargo(meta: Dict[str, str]) -> str:
    """Whether an unpublished submitter's records may go public now.

    See form_metadata.records_embargo, and records_are_held for the "" means hold rule.
    """
    return form_metadata.records_embargo(meta)


def _sending(meta: Dict[str, str]) -> str:
    """What the submitter says they are sending. See form_metadata.sending."""
    return form_metadata.sending(meta)


def _parse_ts(meta: Dict[str, str], fallback: str) -> str:
    raw = (meta.get("Timestamp") or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d")
    except ValueError:
        return fallback


# --------------------------------------------------------------------------------------
# What the public queue is allowed to say about a curator's decision
# --------------------------------------------------------------------------------------
# The queue is a public page. It exists so a submitter can see that their work is moving
# and so the community can see that submissions are being handled -- not to report how any
# individual submission fared.
#
# So the mapping below is deliberately lossy in one direction. A submission that was held,
# flagged, or is waiting on its submitter reads exactly like one that arrived this morning:
# "Under review". Nothing on the public site ever says a submission has problems, was
# questioned, or was turned down. The queue would otherwise be a public record of whose
# work was rejected -- and the entries carry an opaque id precisely because we already
# decided that who submitted what is nobody else's business.
#
# Closed submissions are dropped rather than labeled. A declined submission simply stops
# appearing, which is what "it drops off" means and is the least informative thing the page
# can do about it. A released one stops appearing too: it is in the database now, and the
# database is where it should be read.
PUBLIC_STATUS = {
    "received":          ("In the queue", "p-queue"),
    "screening_failed":  ("Under review", "p-check"),
    "ready_for_review":  ("Under review", "p-check"),
    "in_review":         ("Under review", "p-check"),
    "held":              ("Under review", "p-check"),
    "awaiting_submitter": ("Under review", "p-check"),
    "approved":          ("Accepted", "p-pass"),
}

# States that do not appear on the public queue at all.
DROPPED_STATES = ("declined", "dormant", "withdrawn", "released")


def public_review_state(entry, config: Optional[Dict] = None,
                        now=None) -> str:
    """The state the public queue should treat this entry as having.

    One rule, and it is the same rule ``notify_submitters`` applies before telling a
    submitter their names are confirmed: **an approval is not public until the publish
    hold has run out.**

    The hold exists so a second curator can still object after an approval. A queue that
    said "Accepted" the moment a curator clicked would have to walk it back to "Under
    review" when a late hold landed -- on a public page, in front of the submitter, with
    no explanation available, because the queue is deliberately not allowed to say that a
    submission was questioned. The whole point of the lossy mapping below is that nobody
    outside can tell a held submission from one that arrived this morning; publishing an
    approval early is the one thing that would break that, by making the *retraction*
    legible even though the hold itself is not.

    So an approved submission still inside its hold reads as ``in_review``: true, since a
    curator may still act, and indistinguishable from every other submission in review.

    Embargo is deliberately not consulted. An embargoed submission has genuinely been
    accepted -- what the embargo withholds is its records from a release, not the fact of
    the decision -- and reporting it as still under review would be false rather than
    merely uninformative.
    """
    if entry.state != "approved":
        return entry.state

    # A hold recorded late in the window still wins, exactly as it does for release
    # eligibility and for the submitter's confirmation email.
    if ledger_module().blocking_holds(entry):
        return "in_review"

    elapsed, _why_not = ledger_module().hold_elapsed(entry.approved_at, config, now)
    return "approved" if elapsed else "in_review"


def ledger_module():
    """The ledger, imported late so a missing or unreadable one is never fatal here.

    This program reports what curators decided; it must still build the contributor board
    if the ledger cannot be read at all. See the caller in ``main`` for that fallback.
    """
    from malavi_curation import ledger as _ledger  # noqa: E402
    return _ledger


def public_status(review_state: Optional[str],
                  screened: bool, has_errors: bool) -> Optional[tuple]:
    """(label, pill) for the public queue, or None to leave the submission off it.

    The review ledger wins where it has an opinion, because a curator's decision is more
    current than the screen that preceded it. Where there is no ledger entry yet -- a
    submission fetched but not enrolled -- it falls back to what screening knows.

    Note what the fallback deliberately does NOT do: it never surfaces "Needs attention".
    Blocking screen errors are between the submitter and the curators.
    """
    if review_state in DROPPED_STATES:
        return None
    if review_state in PUBLIC_STATUS:
        return PUBLIC_STATUS[review_state]
    return ("Under review", "p-check") if (screened or has_errors) else ("In the queue",
                                                                         "p-queue")


def summarize(sub_dir: Path, public_id: str,
              review_state: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One submission -> the facts the site needs. None if it is not a submission."""
    meta_path = sub_dir / "metadata.json"
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    submitted = _parse_ts(meta, sub_dir.name[:8])
    kind = _find(meta, "template", "pdf") or ""
    stage = _publication_stage(meta)
    sending = _sending(meta)

    n_lineages = n_records = 0
    n_errors = n_warnings = 0
    screen_path = sub_dir / "screen.json"
    if screen_path.is_file():
        for rep in json.loads(screen_path.read_text(encoding="utf-8")):
            n_lineages += len(rep.get("lineages") or {})
            n_records += int(rep.get("n_host_records") or 0)
            for issue in rep.get("issues") or []:
                if issue["severity"] == "error":
                    n_errors += 1
                elif issue["severity"] == "warn":
                    n_warnings += 1

    # Status is derived, never typed, and the curator's decision outranks the screen.
    # See PUBLIC_STATUS above for what the public page is allowed to say -- and for why
    # "Needs attention", which this used to publish, no longer appears anywhere.
    shown = public_status(review_state, screen_path.is_file(), bool(n_errors))
    if shown is None:
        return None
    status, pill = shown

    files = sorted(p.name for p in sub_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in (".xlsx", ".pdf", ".docx", ".csv"))

    # What screening actually found, in preference to what was declared -- and
    # what was declared when there is nothing else to go on.
    detail = []
    if n_lineages:
        detail.append(f"{n_lineages} new lineage{'s' if n_lineages != 1 else ''}")
    if n_records:
        detail.append(f"{n_records} record{'s' if n_records != 1 else ''}")
    if not detail and sending:
        detail.append(sending.capitalize())
    if not detail:
        detail.append("Paper and supplementary materials" if files else "No files")
    if stage:
        detail.append(stage)

    # The PUBLIC identifier is the minted opaque one. The intake directory name is
    # built from the submitter's own name (fetch_submissions.submission_dir_name),
    # so publishing it names, for every submission in the queue, a person who has
    # unpublished data on a particular parasite -- regardless of whether they
    # consented to appear on the contributor board. submission_id.py exists to keep
    # that name off public surfaces; this feed was written before it did and was
    # never moved across.
    return {
        "id": public_id,
        # private, stripped before writing the public feeds
        "_dir": sub_dir.name,
        "submitted": submitted,
        "status": status,
        "pill": pill,
        "detail": " · ".join(detail),
        "n_lineages": n_lineages,
        "n_records": n_records,
        "n_files": len(files),
        "n_warnings": n_warnings,
        "n_errors": n_errors,
        # private, stripped before writing the public feeds
        "_email": (_find(meta, "email") or "").lower(),
        "_name": _find(meta, "first", "last name"),
        "_institution": _find(meta, "institution"),
        "_consent": (_find(meta, "leaderboard") or "").strip().lower(),
        "_kind": kind,
        "_stage": stage,
        "_sending": sending,
    }


def _existing_id(inbox: Path, directory: str) -> Optional[str]:
    """The identifier already minted for a directory, without minting one.

    **This program never mints, on any path.** It used to on a real run, and only --dry-run
    took this route, on the argument that minting during a rehearsal would make the
    rehearsal an irreversible act. That argument is right and it applies to the real run
    too, harder:

    An identifier is assigned once and never changes, and the mapping that guarantees it
    lives in ``submission_ids.json`` -- which is gitignored. This program's output,
    ``queue.json``, **is** committed, and the workflow that commits it runs on a clean CI
    runner where that mapping does not exist. So a minting run there invents a sequence
    from 1 in whatever order the directories sort, publishes it, and discards the mapping
    when the job ends. The next run does it again.

    Today the numbering happens to agree with the maintainer's, because intake directories
    are named by timestamp and a full re-fetch reproduces the same order. That is luck, not
    a property: one superseded submission, one directory that exists only on the maintainer's
    machine, or one partial fetch shifts every number after the gap, and a public identifier
    starts pointing at a different person's submission.

    A submission with no identifier is therefore *not published* rather than given one here.
    It has no public name yet, and inventing one is precisely the harm.
    """
    entry = load_ledger(Path(inbox))["ids"].get(directory)
    return entry["id"] if entry else None


def _refuse_to_leak(*payloads: Dict[str, Any]) -> None:
    """Refuse to publish anything carrying a submitter's identity.

    Two checks, both on the payload rather than on the file, so nothing reaches disk:

    * no email address anywhere;
    * every queue item's public id is an opaque minted identifier. That is the check that
      would have caught the intake directory name -- which is built from the submitter's
      own name -- being published as `id` for every submission in the queue. A substring
      search cannot catch a person's name, because a name looks like any other text;
      requiring the field to match a known-safe shape can, and does so for names nobody
      has thought of yet.

    Deliberately not an `assert`: `python -O` removes those, and the one guard standing
    between unpublished work and the public web must not be optional.
    """
    for payload in payloads:
        if "@" in json.dumps(payload, ensure_ascii=False):
            raise SystemExit("refusing to write: an email address reached a public feed")
        for item in payload.get("items", []):
            public_id = str(item.get("id", ""))
            if public_id and not ID_PATTERN.match(public_id):
                raise SystemExit(
                    f"refusing to write: queue id {public_id!r} is not an opaque "
                    f"identifier. The intake directory name carries the submitter's own "
                    f"name and must never be published; mint one with "
                    f"submission_id.submission_id_for().")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = ap.parse_args(argv)

    root = repo_root()
    cfg = load_config()
    inbox = root / (cfg.get("submissions", {}).get("inbox_dir")
                    or "curation/intake/submissions")
    out_dir = root / cfg["paths"]["docs_data_dir"]

    # Submissions the curator has hidden from the public feeds. They stay on
    # disk and in the responses sheet; they are simply not published.
    excluded = {}
    for entry in (cfg.get("submissions", {}).get("exclude") or []):
        if isinstance(entry, dict) and entry.get("id"):
            excluded[entry["id"]] = entry.get("reason", "")
        elif isinstance(entry, str):
            excluded[entry] = ""

    # The review ledger, keyed by the same opaque public id the queue uses (enroll.py
    # enrolls under the minted id). Read-only here: this program reports what curators
    # decided and never touches a decision. A missing or unreadable ledger is not fatal --
    # the queue then falls back to what screening knows, which is what it did before the
    # ledger existed.
    # Read through public_review_state, not off entry.state: an approval inside its
    # publish hold is not public yet. See that function for why.
    review_states: Dict[str, str] = {}
    try:
        _ledger = ledger_module()
        with _ledger.open_ledger(inbox, write=False) as _entries:
            review_states = {sid: public_review_state(entry, cfg)
                             for sid, entry in _entries.items()}
    except Exception as exc:                                    # noqa: BLE001
        print(f"NOTE: could not read the review ledger ({exc}); "
              f"queue status falls back to the screen result.")

    subs = []
    skipped = []
    unminted = []
    for d in sorted(p for p in inbox.iterdir() if p.is_dir()):
        # Never minted here, on either path -- see _existing_id for why the dry-run
        # argument applies to the real run too. A submission with no identifier has no
        # public name, and this program's job is to publish public names.
        public_id = _existing_id(inbox, d.name)
        if not public_id:
            unminted.append(d.name)
            continue

        s = summarize(d, public_id, review_state=review_states.get(public_id))
        if not s:
            continue
        # Matched on the directory name, which is what config/project.yml lists.
        if s["_dir"] in excluded:
            skipped.append((s["_dir"], excluded[s["_dir"]]))
            continue
        subs.append(s)

    print("== malavi_rebuild :: build_site_feeds ==")
    print(f"{len(subs)} submission(s) in {inbox}")
    if skipped:
        print(f"{len(skipped)} excluded from the public feeds:")
        for sid, reason in skipped:
            print(f"    {sid}  ({reason or 'no reason recorded'})")
    if unminted:
        # Loud, because on a machine that has the id ledger this means a submission is
        # missing from the public queue, and on one that does not -- a CI runner -- it
        # means every submission is, which is the correct outcome but not an obvious one.
        print(f"\n{len(unminted)} submission(s) have no identifier yet and are NOT "
              f"published:")
        for name in unminted:
            print(f"    {name}")
        print("  An identifier is assigned once and never changes, so it is minted where\n"
              "  submission_ids.json persists -- by the screen, on BIOMIX -- and never by\n"
              "  this program, whose output is committed. If you are seeing all of them\n"
              "  here, this is running somewhere without the id ledger.")
    print()

    # An id in the exclude list that matches nothing is almost always a typo, and
    # a silent typo here means a test submission stays published.
    present = {p.name for p in inbox.iterdir() if p.is_dir()}
    for sid in excluded:
        if sid not in present:
            print(f"  WARNING: exclude id '{sid}' matches no submission directory.")

    # ---- queue -------------------------------------------------------------
    # Newest first: the page shows current state, not a history.
    queue_items = sorted(subs, key=lambda s: s["submitted"], reverse=True)
    queue = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_submissions": len(queue_items),
        "items": [{k: v for k, v in s.items() if not k.startswith("_")}
                  for s in queue_items],
    }

    # ---- contributors ------------------------------------------------------
    # Keyed on the form's verified email, which is stable; the display name is
    # free text and drifts between submissions. Email is never written out.
    people: Dict[str, Dict[str, Any]] = {}
    for s in subs:
        if s["_consent"] != LEADERBOARD_CONSENT_YES:
            continue
        if not s["_name"]:
            continue
        key = s["_email"] or s["_name"].lower()
        p = people.setdefault(key, {
            "name": s["_name"], "institution": s["_institution"],
            "lineages": 0, "records": 0, "submissions": 0, "since": s["submitted"],
        })
        p["lineages"] += s["n_lineages"]
        p["records"] += s["n_records"]
        p["submissions"] += 1
        p["since"] = min(p["since"], s["submitted"])
        if s["_name"]:
            p["name"] = s["_name"]
        if s["_institution"]:
            p["institution"] = s["_institution"]

    board = sorted(people.values(),
                   key=lambda p: (-p["lineages"], -p["records"], p["name"]))
    contributors = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_contributors": len(board),
        "contributors": board,
    }

    for row in queue["items"]:
        print(f"  [queue] {row['submitted']}  {row['status']:<16} {row['detail']}")
    print()
    for i, p in enumerate(board, 1):
        print(f"  [board] {i}. {p['name']} ({p['institution']}) — "
              f"{p['lineages']} lineages, {p['records']} records, "
              f"{p['submissions']} submission(s)")

    if args.dry_run:
        print("\n[dry-run] nothing written.")
        return 0

    # Written only if something other than the timestamp moved, so a daily run
    # that finds no news leaves the repository alone. See malavi_curation.feeds.
    # Checked BEFORE anything is written. The previous version asserted after the
    # write, so a leak was already on disk (and already published, if the push ran)
    # by the time it fired -- and `assert` vanishes under `python -O`, so the one
    # guard on the one file that reaches the public web was optional.
    _refuse_to_leak(queue, contributors)

    wrote_queue = write_feed(out_dir / "queue.json", queue)
    wrote_board = write_feed(out_dir / "contributors.json", contributors)
    for name, wrote in (("queue.json", wrote_queue), ("contributors.json", wrote_board)):
        if not wrote:
            print(f"  {name} unchanged; left as it was")

    # Re-checked on what is ON DISK, whether or not this run wrote it, so a leak in
    # a file left behind by an earlier run is caught too.
    on_disk = ((out_dir / "contributors.json").read_text()
               + (out_dir / "queue.json").read_text())
    if "@" in on_disk:
        raise SystemExit("an email address is present in a public feed on disk")

    written = [n for n, w in (("queue.json", wrote_queue),
                              ("contributors.json", wrote_board)) if w]
    print(f"\nwrote {', '.join(written)} to {out_dir}" if written
          else f"\nnothing to write; both feeds in {out_dir} were already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
