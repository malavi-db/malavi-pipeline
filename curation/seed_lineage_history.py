#!/usr/bin/env python3
# @title Seed the lineage-history table from what MalAvi already recorded
# @purpose Write the first lineage_history rows from three places the history was
#          kept before the table existed: Staffan's "Sequence updated from ..." notes
#          in the master file, its rows marked DELETED with a reason, and the one
#          lineage that left between the 2026-03-23 and 2026-09-15 releases
#          (STRALU01, folded into STAL3). Dry run by default.
# @why The curators asked that the history not start empty. These events already took
#      effect in the data; this records them, with the sequence before the event where
#      a published release still holds it and blank where it does not.
# @input MALAVI.sqlite (the Lund master database, tables MASTER and SEQUENCES)
# @input docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv
# @input data/records/lineages.csv
# @output data/records/lineage_history.csv
# @program python3
# @critical-flag seed_lineage_history.py "" --apply
# @critical-var PREVIOUS_RELEASE
# @critical-var THIS_RELEASE
"""Usage: .venv/bin/python curation/seed_lineage_history.py [--db MALAVI.sqlite] [--apply]

Refuses to run over a lineage_history table that already has rows: this is the seed,
not a sync. Every row it writes carries ``_source = seed``.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation.config import repo_root                                  # noqa: E402
from malavi_curation.release_store import (                                   # noqa: E402
    SEED, TABLES, assign_ids, read_high_water, read_store, store_dir, write_store,
)

# The two published releases the seed can see. Anything Staffan updated before the
# first of them has no recoverable "before" sequence.
PREVIOUS_RELEASE = "2026-03-23"
THIS_RELEASE = "2026-09-15"

_ACCESSION = re.compile(r"\b([A-Z]{1,2}\d{5,6}(?:\.\d)?)\b")
_UPDATED = re.compile(r"sequence updated", re.I)
# A note that names a study ("Inumaru et al", "Nebel et al 2020") rather than an accession.
_STUDY = re.compile(r"\b([A-Z][A-Za-z-]+ et al\.?(?: \d{4})?)")

# Rows the seed must NOT write, with the verified reason. Checked against GenBank on
# 2026-09-25 (curation/audit_lineage_history_genbank.py): every other accession named
# in a master-file note matches the stored sequence exactly over the aligned part.
EXCLUDE = {
    "MILVUS01": "the master note says 'Sequence updated from HF543635', but the stored "
                "sequence is its own accession JN164716 exactly, and HF543635 is "
                "MILVUS02's sequence (identical over 479 bp). The note is on the wrong "
                "row; no event is recorded until a curator says otherwise.",
}

# The public wording. The master file's notes are Staffan's working shorthand, with
# typos and colleagues' names; what ships is a neutral sentence that says the same
# thing, and the verbatim note is kept in the private seed notes (results/), never in
# the table. See "Publicizing the notes" in results/LINEAGE_HISTORY_seed_notes_2026-09-25.md.
def public_wording(comment: str) -> tuple:
    """(REASON, SOURCE) for a 'sequence updated' note."""
    acc = _ACCESSION.search(comment)
    if acc:
        return (f"Sequence updated from GenBank {acc.group(1)}", acc.group(1))
    study = _STUDY.search(comment)
    if study:
        return (f"Sequence updated from {study.group(1).strip()}", study.group(1).strip())
    if re.search(r"unpublished|provided|:\s*[A-Z][a-z]+ [A-Z][a-z]+|, [A-Z][a-z]+ [A-Z][a-z]+ \d{4}", comment):
        return ("Sequence updated from an unpublished sequence provided by the authors",
                "authors, unpublished")
    return ("Sequence updated (master file note)", "MalAvi master file")


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=repo_root() / "MALAVI.sqlite")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    root = repo_root()
    store = read_store(store_dir(root))
    if store.get("lineage_history"):
        print(f"error: lineage_history already holds {len(store['lineage_history'])} row(s); "
              f"this is the seed and will not run twice.", file=sys.stderr)
        return 2
    current: Dict[str, Dict[str, str]] = {r["LINEAGE_NAME"].strip(): r for r in store["lineages"]}
    previous_csv = root / "docs" / "assets" / "downloads" / "tables" / f"grand_lineage_summary_{PREVIOUS_RELEASE}.csv"
    with open(previous_csv, encoding="utf-8", newline="") as handle:
        previous = {r["LINEAGE_NAME"].strip(): r for r in csv.DictReader(handle)}

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    master_seq = {r["LINEAGE_NAME"].strip(): _text(r["SEQUENCE"]).upper()
                  for r in db.execute("SELECT LINEAGE_NAME, SEQUENCE FROM SEQUENCES")}

    rows: List[Dict[str, str]] = []
    skipped: List[str] = []
    private: List[Dict[str, str]] = []      # verbatim notes, for the private seed notes file

    def add(**kw):
        row = {c: "" for c in TABLES["lineage_history"].columns}
        row.update(kw)
        row.update({"_source": SEED, "_added": THIS_RELEASE})
        rows.append(row)

    # ---- (a) "Sequence updated from ..." notes on live master rows -------------------
    for r in db.execute("SELECT LINEAGE_NAME, GENBANK_ACC, COMMENT, DELETED FROM MASTER "
                        "WHERE COMMENT IS NOT NULL"):
        name, comment = _text(r["LINEAGE_NAME"]), _text(r["COMMENT"])
        if not _UPDATED.search(comment) or _text(r["DELETED"]):
            continue
        if name not in current:
            skipped.append(f"{name}: has an update note but is not in the store")
            continue
        if name in EXCLUDE:
            skipped.append(f"{name}: EXCLUDED: {EXCLUDE[name]}")
            private.append({"lineage": name, "event": "(not recorded)", "note": comment,
                            "why": EXCLUDE[name]})
            continue
        new_seq = _text(current[name].get("SEQUENCE")).upper()
        old_seq = ""
        release = f"{PREVIOUS_RELEASE} or earlier"
        prev = previous.get(name)
        if prev and _text(prev.get("SEQUENCE")).upper() != new_seq:
            # The update happened between the two releases we hold: the before is known.
            old_seq = _text(prev.get("SEQUENCE")).upper()
            release = THIS_RELEASE
        reason, source = public_wording(comment)
        add(LINEAGE_NAME=name, EVENT="sequence_updated", OLD_NAME=name, NEW_NAME=name,
            OLD_SEQUENCE=old_seq, NEW_SEQUENCE=new_seq, DATE="",
            REASON=reason, SOURCE=source, RELEASE=release)
        private.append({"lineage": name, "event": "sequence_updated", "note": comment,
                        "why": f"published as: {reason} / {source}"})

    # ---- (b) master rows marked DELETED, with their reason ---------------------------
    for r in db.execute("SELECT LINEAGE_NAME, COMMENT, DELETED FROM MASTER "
                        "WHERE DELETED IS NOT NULL AND TRIM(DELETED) <> ''"):
        name, comment, deleted = _text(r["LINEAGE_NAME"]), _text(r["COMMENT"]), _text(r["DELETED"])
        if name in current:
            skipped.append(f"{name}: marked DELETED in the master but still in the store")
            continue
        date = deleted[:10] if re.match(r"\d{4}-\d{2}-\d{2}", deleted) else ""
        same_as = re.match(r"same as\s+([A-Za-z0-9_]+)", comment, re.I)
        if same_as and same_as.group(1).strip() in current:
            target = same_as.group(1).strip()
            add(LINEAGE_NAME=target, EVENT="merged_into", OLD_NAME=name, NEW_NAME=target,
                OLD_SEQUENCE=master_seq.get(name, ""),
                NEW_SEQUENCE=_text(current[target].get("SEQUENCE")).upper(), DATE=date,
                REASON=f"Recorded in the master file as the same lineage as {target} and merged into it",
                SOURCE="MalAvi master file", RELEASE=f"{PREVIOUS_RELEASE} or earlier")
            private.append({"lineage": name, "event": "merged_into", "note": comment,
                            "why": f"row marked DELETED {deleted}"})
        else:
            add(LINEAGE_NAME=name, EVENT="removed", OLD_NAME=name, NEW_NAME="",
                OLD_SEQUENCE=master_seq.get(name, ""), NEW_SEQUENCE="", DATE=date,
                REASON="Retired in the master file; its sequence had been replaced",
                SOURCE="MalAvi master file", RELEASE=f"{PREVIOUS_RELEASE} or earlier")
            private.append({"lineage": name, "event": "removed", "note": comment,
                            "why": f"row marked DELETED {deleted}"})

    # ---- (c) lineages in the previous release that the current one no longer has -----
    for name, prev in previous.items():
        if name in current:
            continue
        # The alt_names table says where it went, if anywhere.
        target = next((a["LINEAGE_NAME"].strip() for a in store["alt_names"]
                       if _text(a.get("ALT_NAME")) == name), "")
        if target and target in current:
            ref = next((a["REFERENCE_NAME"] for a in store["alt_names"]
                        if _text(a.get("ALT_NAME")) == name), "")
            add(LINEAGE_NAME=target, EVENT="merged_into", OLD_NAME=name, NEW_NAME=target,
                OLD_SEQUENCE=_text(prev.get("SEQUENCE")).upper(),
                NEW_SEQUENCE=_text(current[target].get("SEQUENCE")).upper(), DATE="",
                REASON=f"In the {THIS_RELEASE} master {name} is recorded as an alternative "
                       f"name of {target}, whose sequence was updated at the same time",
                SOURCE=_text(ref) or "master file", RELEASE=THIS_RELEASE)
        else:
            add(LINEAGE_NAME=name, EVENT="removed", OLD_NAME=name, NEW_NAME="",
                OLD_SEQUENCE=_text(prev.get("SEQUENCE")).upper(), NEW_SEQUENCE="", DATE="",
                REASON=f"Present in release {PREVIOUS_RELEASE}, absent from {THIS_RELEASE}; "
                       f"no reason recorded in the master file",
                SOURCE="master file", RELEASE=THIS_RELEASE)

    # ---- report ---------------------------------------------------------------------
    by_event: Dict[str, int] = {}
    for row in rows:
        by_event[row["EVENT"]] = by_event.get(row["EVENT"], 0) + 1
    with_old = sum(1 for row in rows if row["OLD_SEQUENCE"])
    print(f"lineage_history seed: {len(rows)} row(s): " +
          ", ".join(f"{k} {v}" for k, v in sorted(by_event.items())) +
          f"; {with_old} with the sequence before the event known")
    for row in rows:
        if row["EVENT"] != "sequence_updated" or row["OLD_SEQUENCE"]:
            print(f"  {row['EVENT']:17s} {row['OLD_NAME']:10s} -> {row['NEW_NAME'] or '(removed)':10s} "
                  f"[{row['RELEASE']}] {row['REASON'][:70]}")
    for s in skipped:
        print(f"  skipped: {s}")
    # The private companion: every verbatim note behind a row, and the exclusions.
    notes_path = root / "results" / "LINEAGE_HISTORY_seed_notes_2026-09-25.md"
    lines = ["# Lineage history seed: the master-file notes behind each row (PRIVATE)", "",
             "Internal. `results/` is not on the publish allowlist. The public table carries a",
             "neutral sentence per row; this file keeps Staffan's verbatim note (typos, colleagues'",
             "names and all) so the wording can be traced, and records why any note was NOT turned",
             "into a row. Written by `curation/seed_lineage_history.py`.", "",
             "## Publicizing the notes", "",
             "Reviewed 2026-09-25 at Vincenzo's request. Nothing sensitive was found; the notes are",
             "working shorthand. Three things were kept out of the public table anyway: personal",
             "names of colleagues who supplied sequences (published as 'provided by the authors'),",
             "typos ('sequnce'), and the note on MILVUS01, which GenBank shows is on the wrong row.", "",
             "| lineage | event | verbatim master note | published as / why |", "|---|---|---|---|"]
    for n in private:
        lines.append(f"| {n['lineage']} | {n['event']} | {n['note'].replace('|', '/')} | {n['why'].replace('|', '/')} |")
    if not args.apply:
        print(f"\n[dry-run] nothing was written. Re-run with --apply. (would also write {notes_path.name})")
        return 0
    notes_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store["lineage_history"] = assign_ids(TABLES["lineage_history"], rows,
                                          floor=read_high_water(store_dir(root)).get("lineage_history", 0))
    write_store(store_dir(root), store)
    print(f"\nwrote {len(rows)} row(s) to {store_dir(root) / 'lineage_history.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
