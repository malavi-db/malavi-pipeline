# MalAvi pipeline

This is the code that maintains [MalAvi](https://malavi-db.github.io/), the database of avian
haemosporidian parasites and their host and geographic records.


## What is in here

| Directory | What it does |
|---|---|
| `curation/` | Screening a submission, checking names and sequences against the current release, building the curator's report, and recording curator decisions in a review ledger. |
| `export/` | Turning a release into the data files the website reads. |
| `watcher/` | Scanning the literature (Europe PMC, OpenAlex, Crossref) for papers that report lineages MalAvi does not have. |
| `publish/` | Publishing the website, and publishing this code repository. |
| `schemas/` | What a submitted workbook is allowed to contain. |
| `reference/` | Reference data: the country-to-region map, and a note on the cytochrome *b* barcode frame and the primers that produce it. |
| `backup/` | Copying the irreplaceable parts of the project to permanent storage. |

## How a submission moves through it

```
  a researcher fills in the submission workbook/template file and submits it through the Google form
                  │
                  ▼
  fetch_submissions.py     collects it from the submission form
                  │
                  ▼
  check_template.py        screens it: are the proposed lineage names free?
                           are the sequences already in MalAvi? do the sequences look OK? do the hosts,
                           the geography and the counts seem OK?
                  │
                  ▼
  publish_report.py        sends the resulting report to a curator
                  │
                  ▼
  fetch_verdicts.py        records the curator's decision in the ledger
                  │
                  ▼
  build_release.py         builds the next release from the record store
```

Nothing is added to MalAvi automatically. Every automated check produces a report for a
curator and the curators decide.

## Two principles of the code

**Data extraction is deterministic.** Nothing that reads a submitted
workbook or a paper uses AI. A record that enters MalAvi has to be traceable
to a file somebody submitted. This is the only way to protect against AI hallucinations.

**The sequence checker should not call a known lineage new.** Telling a submitter their
sequence is new when it already exists in MalAvi puts a duplicate name into a paper and
possibly GenBank. The checker compares against every lineage in
the release, never a sample, and is exact and deterministic.

## What is not here

- **The data.** Submissions in progress are not
  published. Submitted data are published in new releases only,
  available at the [MalAvi site](https://malavi-db.github.io/).
- **Credentials, and the identifiers that point at private Google resources.** I've used placeholders here.

## Running it

The curation code is a Python package:

```bash
python -m venv .venv
.venv/bin/pip install -e 'curation[report,google]'
.venv/bin/python -m pytest curation/tests -q
```

Then copy `config/project.example.yml` to `config/project.yml` and fill in your own
values. `RUNBOOK.md` is the operator's guide: it goes step by step through screening a
submission, building a release, and publishing the site, and it says which steps have been
verified and which have not.

The R code comes from [malaviR](https://github.com/vincenzoaellis/malaviR). The report renderer requires WeasyPrint
and its system libraries. The Google scripts `curation/apps_script/` are pasted
into an Apps Script project by hand.

## Credit

MalAvi is really the work of **Staffan Bensch** and colleagues at
Lund University. This repository is just an attempt to make the database sustainable into the future.

## License and contact

MIT — see `LICENSE`.

Questions, corrections, or a submission: **malaviadmin@gmail.com**.

If you use MalAvi in published work, cite the database as described on the
[website](https://malavi-db.github.io/), and see `CITATION.cff` for this repository.
