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

Each accepts `--dry-run`, which loads config + malaviR and prints the plan without writing.

## Run order

Nothing here depends on another script's output, so the order is only a convention:

```bash
Rscript export/build_site_stats.R       # figures + table row counts
Rscript export/build_tables_json.R      # the browsable tables
Rscript export/build_downloads.R        # the download files
Rscript export/build_reports.R          # the QC reports
Rscript export/build_sequence_index.R   # the checker's lineage index
```

Re-run **all** of them after bumping `malaviR.release` in `config/project.yml`, then
publish with `publish/push_site.sh`. `build_site_stats.R` and `build_tables_json.R` in
particular must agree: the site takes its row counts from the first and its rows from the
second, and a stale pair would advertise a count the table does not contain.

## Unfinished scaffold

`export_tables.R` and `build_datatables_json.R` are Phase 1 scaffolding whose writing
bodies are still stubbed (`stop(...)`); only `--dry-run` works. They targeted the
pre-redesign `docs/tables.html` page, which is no longer published. `build_tables_json.R`
supersedes `build_datatables_json.R`; the CSV/FASTA output `export_tables.R` was meant to
produce is covered by `build_downloads.R`. Both are candidates for deletion.

## Requirements

- R packages: `malaviR` (+ `ape` for FASTA, `openxlsx` for XLSX), `yaml`, `jsonlite`.
- The release is pinned in `config/project.yml` (`malaviR.release`). Keep it in sync with
  the installed `malaviR` (`malaviR::malavi_version()`).

> **HPC note:** these are light (load bundled data, serialize) and are safe to run on a
> compute node inside a Slurm allocation. Do not run heavy work on the login node.
