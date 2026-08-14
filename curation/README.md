# curation/

The paper-first curation helper. MalAvi is **paper-first**: you find a publication,
extract the sequence accessions, hosts, and geography, and add them to the database. This
package turns that manual workflow into review-ready candidate records for a curator.

Python does the parsing; R (via `malaviR`) does the validation. Nothing here is auto-ingested
— everything is staged for a human curator to accept or reject.

## Pipeline

```
   PDF
    │  pdf_extract.py        text + tables
    ▼
   text ─┬─ accession_mine.py     NCBI accession tokens        [implemented]
         └─ hosts_geography.py    host species + localities
                    │
                    ▼  record_builder.py
            submission record (schemas/submission.schema.json)
                    │
       ┌────────────┴───────────────┐
       ▼ r/validate_record.R        ▼ r/host_geo_flag.R
   malaviR::lineage_qc /         improbable host/locality
   match_taxonomy                flag (the malaviR gap)
                    │
                    ▼  curator_report.py
              review-ready Markdown digest
```

## Layout

| Path | Role | Status |
| --- | --- | --- |
| `src/malavi_curation/accession_mine.py` | Regex miner for NCBI accessions **+ range expansion** | **implemented + tested** |
| `src/malavi_curation/config.py` | Loads `config/project.yml` | implemented |
| `src/malavi_curation/pdf_extract.py` | PDF → text (pdftotext) + tables (pdfplumber) | **implemented** |
| `src/malavi_curation/hosts_geography.py` | text → host binomials + countries (malaviR-grounded) | **implemented** |
| `src/malavi_curation/record_builder.py` | → submission record (schema-validated) | **implemented** |
| `src/malavi_curation/curator_report.py` | → curator Markdown digest | **implemented** |
| `src/malavi_curation/pipeline.py` | orchestrate PDF folder → review report | **implemented** |
| `r/validate_record.R` | reconcile host names (`match_taxonomy`) + run host_geo_flag | **implemented** |
| `r/host_geo_flag.R` | improbable host/locality flag → upstream to malaviR | **implemented** |
| `src/malavi_curation/validate.py` | Python→R bridge; folds flags into the report | **implemented** |
| `r/benchmark_truth.R` | emit MalAvi ground truth per reference (JSON) | **implemented** |
| `r/gazetteer.R` | emit malaviR genera/binomials/countries → `data/gazetteer.json` | **implemented** |
| `benchmark/run_benchmark.py` | score extraction vs. truth | **implemented** |

## Benchmark

```bash
Rscript curation/r/benchmark_truth.R --all-benchmark > curation/benchmark/truth.json
python curation/benchmark/run_benchmark.py \
  --truth curation/benchmark/truth.json --pdf-dir curation/benchmark/pdfs \
  --report curation/benchmark/benchmark_report.md
```

Current result across all five benchmark papers: **100% recall on in-paper accessions**
(driven by accession-**range** expansion — papers report ranges like `PV948475-PV948494`
but never the interior accessions MalAvi curates), **100% host recall**, and **100% country
recall**. Host precision is low by design (papers name many non-host birds; the curator
prunes). See `benchmark/benchmark_report.md` and `results/METHODS_draft.md`.

> ⚠️ **Those are entity-level numbers on five papers the extractor was developed against.
> Do not read them as how well this works on a paper nobody has seen.** On the held-out
> corpus drawn in August 2026 — twelve references, never tuned against — entity recall held
> up at roughly 100%, and **record-level recall was 10.7%**: the extractor reliably finds
> *which* accessions, hosts and countries a paper mentions, and rarely assembles them into
> the lineage × host × site rows MalAvi actually stores. `CURATION_STATUS.md` carries the
> measured figure and is the one to quote. The gap is not a bug to be tuned away; it is
> the reason curation is a person's job and the extractor is an assistant.

> Regenerate the gazetteer after a malaviR release bump:
> `Rscript curation/r/gazetteer.R > curation/src/malavi_curation/data/gazetteer.json`

## Install & test

```bash
cd curation
pip install -e ".[dev,pdf]"   # dev = pytest/jsonschema; pdf = pdfplumber
pytest                        # 27 tests
```

## Run the whole pipeline

```bash
# PDFs in a folder -> one Markdown review report for the curator
python -m malavi_curation.pipeline path/to/pdfs --out review_report.md

# add malaviR validation (host-name reconciliation + improbable host/locality flags)
python -m malavi_curation.pipeline path/to/pdfs --validate --out review_report.md
```

Each paper becomes a schema-valid submission record (`schemas/submission.schema.json`):
reference, all candidate accessions (interior ones recovered from ranges), candidate host
binomials, localities, and review flags — staged for a curator to accept/reject. Nothing is
auto-ingested.

## Design notes

- **Reuse, don't reinvent.** Accession patterns mirror `malaviTree/scripts/03_mine_accessions.sh`;
  all sequence/host validation delegates to `malaviR`.
- **Recall over precision** in mining — surface candidates for a human, don't auto-decide.
- **The one new capability** is `host_geo_flag.R` (improbable host/locality), which does not
  exist in malaviR yet; once proven it should be upstreamed there.
