# watcher/

A lightweight publication scanner. It queries the literature for new avian haemosporidian
papers, merges hits across sources, drops anything already seen (and, optionally, anything
already in MalAvi), and produces a batch the curator triages.

Intended to run in **GitHub Actions** (`.github/workflows/watcher.yml`) — but the query path
is fully usable locally today (delivery is still stubbed, so CI stays on `--dry-run`).

| File | Role | Status |
| --- | --- | --- |
| `scan_publications.py` | Query sources, merge/de-dupe, optional MalAvi filter, write a batch | **live** |
| `extract_malavi_refs.R` | Dump MalAvi reference titles → JSON for the "not in MalAvi" filter | live |
| `notify.py` | Batch → GitHub issue (default) or email | formatting done; delivery stubbed |
| `seen.json` | Persisted IDs already reported (committed, so de-dupe survives runs) | — |

## Sources

Queried in one pass and merged on a shared DOI-based de-dup key (a paper found in more than
one source appears once, tagged with every source that found it). All are open, keyless APIs:

- **europepmc** — PubMed/MEDLINE + PMC + preprints + Agricola. Uses the boolean
  `europepmc_query`.
- **openalex** — broad cross-publisher index. Uses `keyword_query`; its `search` is a real
  full-text filter.
- **crossref** — DOI-level cross-publisher metadata. Uses `keyword_query`. Crossref only
  *ranks* (does not filter), so its results get a title-relevance filter
  (`relevance_terms`) to strip journal front-matter/TOC noise.

## Config

All tunables live in `config/project.yml` under `watcher:` — the source list, the two
queries, `lookback_days`, the relevance filter, delivery method, and batch cap.

## Run locally

```bash
# Show config + seen count, no network:
python watcher/scan_publications.py --dry-run

# See what's out there that is NOT yet in MalAvi (recommended local workflow):
#   1. Export the current MalAvi reference titles (reads the pinned malaviR release):
Rscript watcher/extract_malavi_refs.R /tmp/malavi_references.json
#   2. Scan a wide window, cross-check against MalAvi, without touching the committed seen-list:
python watcher/scan_publications.py \
    --lookback-days 180 \
    --malavi-refs /tmp/malavi_references.json \
    --seen /tmp/seen_explore.json --no-save-seen \
    --out /tmp/batch.json

# Render the batch as a Markdown digest:
python watcher/notify.py --batch /tmp/batch.json --dry-run
```

Useful flags for ad-hoc local runs: `--lookback-days N` (override the window),
`--malavi-refs PATH` (drop papers already in MalAvi, matched on normalized title —
MalAvi has no DOI column yet), `--seen PATH` + `--no-save-seen` (keep the committed
`seen.json` untouched during exploration).

## Tests

```bash
python -m pytest watcher/test_scan_publications.py
```

Network-free: the source functions are monkeypatched, so tests cover normalization, the
cross-source de-dup, the relevance/MalAvi filters, and the merge in `scan()`.

## Remaining before enabling CI live

- **Delivery**: `notify.py` issue/email sending is still a stub.
- **State**: `seen.json` would not persist across Actions runs — needs the workflow to
  commit it back (or a different store) before the scheduled scan runs without `--dry-run`.
