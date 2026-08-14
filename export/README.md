# export/

R scripts that turn the `malaviR` package data into the files the website serves. These
are **thin shims** over `malaviR` — no re-derivation of the data, just serialization.

All six tables come from one shared accessor, `export/lib/tables.R`, so the row count the
site advertises, the table a visitor browses, and the file they download are all built
from the same definition. The derived Table of Lineage Names lives there too.

## Live scripts

| Script | Reads | Writes |
| --- | --- | --- |
| `build_site_stats.R` | `malaviR` | `docs/assets/data/site_stats.json` — every figure on the site |
| `build_tables_json.R` | `malaviR` | `docs/assets/data/tables/<id>.json` (all rows) + `tables_index.json` (column specs) |
| `build_downloads.R` | `malaviR` | `docs/assets/downloads/` — per-table CSV/XLSX, the FASTA alignment, the everything ZIP |
| `build_reports.R` | `malaviR` | `docs/assets/reports/*.csv` + `docs/assets/data/reports.json` |
| `build_sequence_index.R` | `malaviR` | `docs/assets/data/lineage_sequences.json` — the sequence checker's index |
| `build_bird_names.R` | `malaviR` (clootl snapshot) | `docs/assets/data/bird_names.json` — the checklist the name checker validates host names against |

Each accepts `--dry-run`, which loads config + malaviR and prints the plan without writing.

## Run order

Nothing here depends on another script's output, so the order is only a convention:

```bash
Rscript export/build_bird_names.R       # the eBird/Clements checklist the name checker uses
Rscript export/build_site_stats.R       # figures + table row counts
Rscript export/build_tables_json.R      # the browsable tables
Rscript export/build_downloads.R        # the download files
Rscript export/build_reports.R          # the QC reports
Rscript export/build_sequence_index.R   # the checker's lineage index
```

`build_bird_names.R` was missing from this list until 2026-08-14, and `build_downloads.R`
was missing from RUNBOOK §6. Six scripts, one list; RUNBOOK §6 is the same six.

Re-run **all** of them after bumping `malaviR.release` in `config/project.yml`, then
publish with `publish/push_site.sh`. `build_site_stats.R` and `build_tables_json.R` in
particular must agree: the site takes its row counts from the first and its rows from the
second, and a stale pair would advertise a count the table does not contain.

## Two scripts that used to be here

`export_tables.R` and `build_datatables_json.R` were **deleted on 2026-08-14**, noted here
because both are named in older documents and in two comments in `watcher/`.

They were Phase 1 scaffolding whose writing bodies were never finished — stubbed `stop(...)`,
so only `--dry-run` did anything. They targeted the pre-redesign `docs/tables.html`, deleted
2026-08-13. `build_tables_json.R` superseded `build_datatables_json.R`, and the CSV/FASTA
output `export_tables.R` was meant to produce is covered by `build_downloads.R`.

They are also the reason the project notes said "the export pipeline is still a stub", which
was never true of the pipeline that actually runs: the live scripts above all work and were
verified byte-identical against the live site on 2026-08-08. Two dead files were making six
working ones look unfinished.

## Requirements

- R packages: `malaviR` (+ `ape` for FASTA, `openxlsx` for XLSX), `yaml`, `jsonlite`.
- The release is pinned in `config/project.yml` (`malaviR.release`). Keep it in sync with
  the installed `malaviR` (`malaviR::malavi_version()`).

> **HPC note:** these are light (load bundled data, serialize) and are safe to run on a
> compute node inside a Slurm allocation. Do not run heavy work on the login node.
