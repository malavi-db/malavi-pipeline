#!/usr/bin/env python3
# @title Build the public feed of lineage names claimed by pending submissions
# @purpose Read every fetched submission's screening report, collect the new
#   lineage names it proposes, and write the name + the date it was claimed to a
#   public JSON feed the website's sequence checker reads.
# @why The submission guide has always promised that sending names before you
#   publish "ensures that the lineage names will be saved for your sequences and
#   that your new sequences will not be named by other researchers that may find
#   the parasites independently". Nothing enforced that: the website could tell a
#   visitor a name was free when a submission received weeks earlier had already
#   claimed it, and the clash surfaced only when a curator noticed. Priority goes
#   by the date the submission arrived, so the site has to know what has arrived.
# @input curation/intake/submissions/<dir>/metadata.json
# @input curation/intake/submissions/<dir>/screen.json
# @input docs/assets/data/lineage_sequences.json
# @output docs/assets/data/reserved_names.json
# @program python3
# @critical-var PUBLIC_FIELDS
# @critical-flag build_name_reservations.py "" --dry-run
# =============================================================================
# Privacy
# -------
# The feed carries A NAME AND A DATE. Nothing else. No submitter, no email, no
# institution, no sequence, no host, no locality, no reference. That is a
# deliberate limit, not an oversight: these are names from studies that are
# usually unpublished, and the whole justification for publishing them at all is
# the submission guide's own -- "without information on host species and location
# the data is of no use". A name and a date cannot be scooped; a sequence can.
#
# PUBLIC_FIELDS below is the entire contract, and the writer builds each record
# from it rather than filtering a richer object down, so a future edit cannot
# leak a field by forgetting to strip it.
# =============================================================================
"""Generate reserved_names.json from fetched community submissions."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation.config import load_config, repo_root  # noqa: E402
from malavi_curation.enrollment import claimed_names, name_suggestions  # noqa: E402
from malavi_curation.feeds import write_feed  # noqa: E402

# The complete public shape of one reservation. See the privacy note above.
PUBLIC_FIELDS = ("name", "claimed")


def parse_timestamp(meta: Dict[str, str], fallback: str) -> str:
    """The Google Form timestamp as a plain date, which is what priority runs on.

    Mirrors build_site_feeds.py: the sheet writes US-style dates, but a re-export
    or a locale change can hand us ISO instead, so both are accepted and the
    directory name (itself built from the timestamp) is the last resort.
    """
    raw = (meta.get("Timestamp") or "").strip()
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d")
    except ValueError:
        # Directory names are <YYYYMMDD>T<HHMMSS>_<slug>.
        stem = fallback[:8]
        return f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}" if len(stem) == 8 else fallback


def release_names(root: Path) -> Tuple[set, Optional[str]]:
    """Every lineage name in the pinned release, from the checker's own index.

    Read from the built index rather than from malaviR so this script needs no R
    and can run anywhere the repo is checked out -- including CI.
    """
    index_path = root / "docs" / "assets" / "data" / "lineage_sequences.json"
    if not index_path.is_file():
        return set(), None
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    names = {name for entry in payload.get("entries", []) for name in entry.get("names", [])}
    return names, payload.get("release")


def claims_in(sub_dir: Path) -> List[str]:
    """The new lineage names one submission proposes.

    Delegates to malavi_curation.enrollment, which is also what the review ledger reads.
    They were two copies of the same parse until 2026-08-07; the names this feed
    advertises publicly and the names the ledger reserves internally have to be the same
    names, and two readers of one file are two chances to disagree.
    """
    return claimed_names(sub_dir)


def suggestions_in(sub_dir) -> dict:
    """Free names the screen offered in place of names MalAvi already owns.

    Shared with the review ledger for the reason given on claims_in above.
    """
    return name_suggestions(Path(sub_dir))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = ap.parse_args(argv)

    root = repo_root()
    cfg = load_config()
    submissions_cfg = cfg.get("submissions") or {}
    inbox = root / (submissions_cfg.get("inbox_dir") or "curation/intake/submissions")

    # Test and withdrawn submissions are excluded by config, with a reason, the
    # same list the public queue honors. A test submission must never hold a name.
    excluded = {e["id"]: e.get("reason", "") for e in (submissions_cfg.get("exclude") or [])}

    known, release = release_names(root)
    if not known:
        print("WARNING: no lineage index found; cannot tell a claim from a name "
              "that is already in the release. Run export/build_sequence_index.R first.",
              file=sys.stderr)

    print("== malavi_rebuild :: build_name_reservations ==")
    print(f"release      : {release}")
    print(f"submissions  : {inbox}")

    # name -> (claim date, submission id). Earliest arrival wins, which is the
    # whole point: priority is the date the submission was received.
    earliest: Dict[str, Tuple[str, str]] = {}
    collisions: Dict[str, List[Tuple[str, str]]] = {}
    already_named: List[Tuple[str, str]] = []
    corrected: List[Tuple[str, str, str]] = []
    n_submissions = 0

    for sub_dir in sorted(p for p in inbox.iterdir() if p.is_dir()) if inbox.is_dir() else []:
        if sub_dir.name in excluded:
            print(f"  skipped {sub_dir.name}: {excluded[sub_dir.name]}")
            continue
        meta_path = sub_dir / "metadata.json"
        if not meta_path.is_file():
            continue
        n_submissions += 1
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        claimed = parse_timestamp(meta, sub_dir.name)

        # A proposed name MalAvi already owns has a free alternative recorded against it
        # by the screen. Publishing the taken name would advertise a reservation that was
        # never going to be granted; publishing nothing would leave the free name
        # unclaimed, so the next submitter could be offered the very name this one is
        # about to be given. So the correction is published in the taken name's place.
        corrections = suggestions_in(sub_dir)

        for name in claims_in(sub_dir):
            name = (name or "").strip().upper()
            if not name:
                continue
            if name in corrections:
                replacement = corrections[name].strip().upper()
                if replacement and replacement not in known:
                    corrected.append((name, replacement, sub_dir.name))
                    name = replacement
            # A name already in the release is not a reservation. It is either an
            # error the curator will raise with the submitter, or a record for an
            # existing lineage; either way the checker already knows the name is
            # taken, from the release itself.
            if name in known:
                already_named.append((name, sub_dir.name))
                continue
            previous = earliest.get(name)
            if previous is None or (claimed, sub_dir.name) < previous:
                if previous is not None:
                    collisions.setdefault(name, []).append(previous)
                earliest[name] = (claimed, sub_dir.name)
            else:
                collisions.setdefault(name, []).append((claimed, sub_dir.name))

    records = [
        dict(zip(PUBLIC_FIELDS, (name, earliest[name][0])))
        for name in sorted(earliest)
    ]

    if corrected:
        print(f"  {len(corrected)} claimed name(s) replaced with a free alternative:")
        for taken, replacement, where in corrected:
            print(f"    {taken:<12} -> {replacement:<12} ({where})")

    print(f"  read {n_submissions} submission(s)")
    print(f"  names claimed and not yet in the release: {len(records)}")
    for name in sorted(earliest):
        print(f"    {name:<12} claimed {earliest[name][0]}  ({earliest[name][1]})")

    # Curator-side only. Two submissions wanting the same name is exactly the
    # case this feed exists to prevent, so it is reported loudly here -- but the
    # public feed still shows one date, the earliest, because that is the answer
    # to "is this name free?" and the rest is nobody else's business.
    if collisions:
        print("\n  NAME COLLISIONS -- more than one submission claims these:")
        for name, others in sorted(collisions.items()):
            winner = earliest[name]
            print(f"    {name}: priority to {winner[1]} ({winner[0]}); also claimed by "
                  + ", ".join(f"{sid} ({date})" for date, sid in others))
    if already_named:
        print("\n  Claimed but ALREADY a release lineage name (not reserved here):")
        for name, sid in sorted(already_named):
            print(f"    {name} ({sid})")

    payload = {
        "release": release,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_names": len(records),
        "names": records,
    }

    out_path = root / "docs" / "assets" / "data" / "reserved_names.json"
    if args.dry_run:
        print(f"\n[dry-run] would write {out_path}")
        return 2 if collisions else 0
    # Written only if a claim actually changed, so a daily run that finds no new
    # submissions leaves the repository alone. See malavi_curation.feeds.
    if write_feed(out_path, payload, ensure_ascii=True, newline="\n"):
        print(f"\nwrote {out_path}")
    else:
        print(f"\n{out_path.name} unchanged; left as it was")

    # The feed is still written -- the earliest claimant's reservation is correct and the
    # website should show it. But the run does NOT report success, because two submitters
    # asking for one name needs a person: one of them has to be told, and this is the only
    # place that knows. It used to be a line of stdout among many, in a program whose exit
    # code was the only thing anyone looked at.
    if collisions:
        print(f"\n{len(collisions)} name(s) are claimed by more than one submission. "
              f"The earliest claim is published; the other submitter(s) must be offered "
              f"another name before their submission is approved.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
