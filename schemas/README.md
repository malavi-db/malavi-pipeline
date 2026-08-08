# schemas/

JSON Schema (Draft 2020-12) definitions for every MalAvi table plus the submission
record, with a human-readable companion in `data_dictionary.md`.

## Why these exist

- **Contract for exports.** `export/` writes the `data/` artifacts the website serves;
  these schemas document and validate the shape of those artifacts.
- **Contract for submissions.** `submission.schema.json` is the target of the curation
  helper and mirrors the GitHub Issue form, so machine- and human-submitted data converge
  on one structure.
- **Drift detection.** Column lists are pinned to a malaviR release (currently
  `2026-03-23`). If a future release changes columns, validation fails loudly.

## Files

| File | Source table (`malaviR::extract_table` key) | Columns |
| --- | --- | --- |
| `hosts_and_sites.schema.json` | Hosts and Sites Table | 20 |
| `grand_lineage_summary.schema.json` | Grand Lineage Summary | 24 |
| `morpho_species.schema.json` | Morpho Species Summary | 5 |
| `references.schema.json` | Table of References | 6 |
| `vector_data.schema.json` | Vector Data Table | 6 |
| `submission.schema.json` | *(curation queue, not a MalAvi table)* | — |

## Validating

```bash
# Python (jsonschema): validate one exported row
python -c "import json, jsonschema; \
  s=json.load(open('schemas/references.schema.json')); \
  jsonschema.validate({'REFERENCE_NAME':'Bensch et al 2009','PUBLICATION_YEAR':2009}, s)"
```

## Updating after a malaviR release bump

1. Bump `malaviR.release` in `config/project.yml`.
2. Re-derive columns:
   `Rscript -e 'library(malaviR); str(extract_table("Hosts and Sites Table"))'`.
3. Update the affected schema(s) + `data_dictionary.md`. Keep `additionalProperties: false`
   so new columns are caught rather than silently passed through.
