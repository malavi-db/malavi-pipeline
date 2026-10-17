#!/usr/bin/env python3
# @title Build the synonymy and ambiguity review sheet for the curators
# @purpose Screen the record store for every pair of lineages at pairwise-deletion
#          distance 0, classify each (duplicate / contained / ambiguous), attach the
#          records, hosts, references and sequence facts per side, propose an action
#          under the curators' rule, and write the sheet the curators vote on.
# @why Meeting point 5 of 2026-09-24: simplify the synonymies and ambiguities, but
#      pair by pair, with a vote, and with a public history row for every change. This
#      is the candidate list; apply_lineage_decisions.py applies what comes back.
# @input data/records/*.csv
# @output results/SYNONYMY_REVIEW_<date>.csv, .xlsx and .md (nothing in the store changes)
# @program python
# @critical-var MIN_COMPARABLE
# @critical-var SMALL_LINEAGE_MAX_HOST_RECORDS
"""Usage:

    .venv/bin/python curation/review_synonymies.py [--date 2026-09-27] [--out-dir results]
        [--min-comparable 300] [--small-max-records 1] [--decisions <earlier filled sheet>]

Writes three files: the sheet as CSV (the record), as XLSX (the one the curators fill in:
DECISION is a drop-down of merge / remove / keep / rename, INTO is pre-filled with the
proposed target) and a Markdown summary of the counts and the rule. Reads only the store.
Internal: the sheet names references and record counts, which is fine, but it is not a
public document and results/ is not on the publish allowlist.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from malavi_curation import synonymy_review as sr                  # noqa: E402
from malavi_curation.config import repo_root                        # noqa: E402
from malavi_curation.release_store import read_store, store_dir     # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="Date stamp for the file names (default: today).")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Where to write (default: results/ in the repo).")
    parser.add_argument("--min-comparable", type=int, default=sr.MIN_COMPARABLE,
                        help="Positions determined in both members below which nothing is "
                             "proposed (default: the ingest's known-lineage floor).")
    parser.add_argument("--small-max-records", type=int, default=sr.SMALL_LINEAGE_MAX_HOST_RECORDS,
                        help="A contained lineage with at most this many records is proposed "
                             "for merging (default 1: the curators' 'one host record').")
    parser.add_argument("--decisions", type=Path, default=None,
                        help="An earlier filled-in sheet (CSV or XLSX); its decisions are "
                             "shown in PRIOR_DECISION so they are not asked again.")
    return parser.parse_args(argv)


def read_sheet(path: Path):
    """Rows of a review sheet, CSV or XLSX, as dicts keyed by the header."""
    if path.suffix.lower() == ".xlsx":
        import openpyxl
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheet = book["Review"] if "Review" in book.sheetnames else book[book.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        header = [str(h or "").strip() for h in next(rows)]
        return [dict(zip(header, ["" if v is None else v for v in r])) for r in rows
                if any(v not in (None, "") for v in r)]
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def prior_decisions(path: Path):
    return {sr._name(r.get("LINEAGE")): str(r.get("DECISION") or "").strip().lower()
            for r in read_sheet(path) if str(r.get("DECISION") or "").strip()}


def write_csv(path: Path, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sr.REVIEW_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def write_xlsx(path: Path, rows, summary, rules_text: str) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Review"
    columns = list(sr.REVIEW_COLUMNS)
    sheet.append(columns)
    for row in rows:
        sheet.append([row.get(c, "") for c in columns])
    bold = Font(bold=True)
    fill = PatternFill("solid", fgColor="FFF2CC")          # the three columns to fill in
    for index, name in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index)
        cell.font = bold
        if name in sr.DECISION_COLUMNS:
            cell.fill = fill
            for r in range(2, len(rows) + 2):
                sheet.cell(row=r, column=index).fill = fill
        width = {"PROPOSAL": 60, "CONTAINERS": 24, "CURATOR_NOTE": 40,
                 "LINEAGE_REFERENCE_NAMES": 40, "OTHER_REFERENCE_NAMES": 40}.get(name, 14)
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "D2"
    decision_col = get_column_letter(columns.index("DECISION") + 1)
    validation = DataValidation(type="list", formula1='"' + ",".join(sr.DECISIONS) + '"',
                                allow_blank=True, showErrorMessage=True,
                                error="One of: " + ", ".join(sr.DECISIONS))
    sheet.add_data_validation(validation)
    validation.add(f"{decision_col}2:{decision_col}{max(len(rows) + 1, 2)}")

    rules = book.create_sheet("How to fill this in")
    rules.column_dimensions["A"].width = 110
    for line in rules_text.splitlines():
        rules.append([line])
    for cell in rules["A"]:
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    book.save(path)


RULES = """\
Each row is one pair of MalAvi lineages whose sequences never disagree at a position both determine.
LINEAGE is the name a decision is about; OTHER is the lineage it is compared with.

KIND
  duplicate  - both names carry the same sequence (same positions determined, same bases).
  contained  - LINEAGE determines a subset of OTHER's positions and agrees at all of them (a partial
               sequence contained in a more complete one). N_CONTAINERS / CONTAINERS list every
               complete lineage the partial is compatible with; more than one means the partial
               sequence cannot say which it is.
  ambiguous  - each determines positions the other lacks; neither can stand for both.

PROPOSAL is the rule applied mechanically; it is a suggestion, not a decision:
  merge into X   - a duplicate, or a contained lineage with at most {small_max} record(s) and exactly
                   one container, over at least {min_comparable} comparable positions.
  decide: ...    - the rule stops: several containers, many records would move, or the two genus
                   labels differ.
  review by hand - an ambiguous pair.
  none: ...      - fewer than {min_comparable} comparable positions; too little to say.

Fill in the three yellow columns:
  DECISION      merge / absorb / remove / keep / rename (leave blank = not decided yet).
  INTO          for merge: the surviving name (pre-filled with the proposed one; change it if you
                prefer another container). For absorb: the name that folds INTO this lineage
                (pre-filled with OTHER). For rename: the new, unused name.
  CURATOR_NOTE  required for remove and rename: it becomes the PUBLIC reason in the Lineage History
                table, so write it as you would for a reader outside the group. Optional otherwise.

What each decision does when applied (one public Lineage History row each):
  merge   LINEAGE's host, vector, morphospecies and alternative-name rows move to INTO; LINEAGE's own
          row is dropped; LINEAGE becomes an alternative name of INTO so old citations still resolve.
  absorb  the other way round: INTO folds into LINEAGE, which keeps its name. When INTO's sequence
          determines more bases, LINEAGE's stored sequence is first completed from it (a
          sequence_updated row, then a merged_into row). This is the 2026-09-24 rule for a complete
          sequence that matches a partial lineage, applied to two names MalAvi already holds:
          the name with the literature survives, the more complete sequence is kept.
  remove  LINEAGE and every row citing it are dropped.
  rename  every row now carries the new name; the old name becomes an alternative name.
  keep    nothing changes; the decision is kept on this sheet so it is not asked again.

Ambiguity codes (R, Y, N ...) count as undetermined here. AMBIGUITY_CODE_MATCHES says how many
private positions are a code compatible with the other side's base; the submission screen would
count those as matches.
"""


def main(argv=None) -> int:
    args = parse_args(argv)
    root = repo_root()
    out_dir = args.out_dir or (root / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    prior = prior_decisions(args.decisions) if args.decisions else {}

    store = read_store(store_dir(root))
    rows, summary = sr.build_review(store, min_comparable=args.min_comparable,
                                    small_max=args.small_max_records, prior=prior)
    rules_text = RULES.format(small_max=args.small_max_records, min_comparable=args.min_comparable)

    stem = out_dir / f"SYNONYMY_REVIEW_{args.date}"
    write_csv(stem.with_suffix(".csv"), rows)
    write_xlsx(stem.with_suffix(".xlsx"), rows, summary, rules_text)

    by_genus_pair = {}
    for r in rows:
        key = " / ".join(sorted({r["LINEAGE_GENUS"] or "?", r["OTHER_GENUS"] or "?"}))
        by_genus_pair[key] = by_genus_pair.get(key, 0) + 1
    lines = [
        f"# Synonymy and ambiguity review, {args.date}",
        "",
        f"Built by `curation/review_synonymies.py` from the record store "
        f"({summary['n_lineages']:,} lineages). The sheet to fill in is "
        f"`{stem.name}.xlsx`; the CSV is the same content. Apply the decisions with "
        f"`curation/apply_lineage_decisions.py` (RUNBOOK §8e).",
        "",
        "| Count | |",
        "|---|---|",
        f"| pairs at distance 0 | {summary['n_pairs']} |",
        f"| duplicate pairs | {summary['by_kind']['duplicate']} |",
        f"| contained pairs | {summary['by_kind']['contained']} "
        f"({summary['n_contained_lineages']} partial lineages, "
        f"{summary['n_contained_unique']} with exactly one container) |",
        f"| ambiguous pairs | {summary['by_kind']['ambiguous']} |",
        f"| lineages appearing in any pair | {summary['n_lineages_in_pairs']} |",
        f"| pairs below the {summary['min_comparable']}-position floor (no proposal) | {summary['n_below_floor']} |",
        f"| pairs whose genus labels differ | {summary['n_cross_genus']} |",
        f"| pairs already linked in alt_names | {summary['n_already_alt_name']} |",
        f"| merges proposed by the rule | {summary['n_proposed_merges']} |",
        "",
        "Pairs by genus: " + ", ".join(f"{k} {v}" for k, v in sorted(by_genus_pair.items())) + ".",
        "",
    ]
    if summary["names_stored_twice"]:
        lines += [f"Names stored twice in the lineages table (not pairs, fix in the store): "
                  f"{', '.join(summary['names_stored_twice'])}.", ""]
    lines += ["## The rule, as written on the sheet", "", "```", rules_text.rstrip(), "```", ""]
    stem.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")

    print(f"{summary['n_pairs']} pairs: {summary['by_kind']['duplicate']} duplicate, "
          f"{summary['by_kind']['contained']} contained, {summary['by_kind']['ambiguous']} ambiguous; "
          f"{summary['n_proposed_merges']} merges proposed. Wrote {stem}.csv / .xlsx / .md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
