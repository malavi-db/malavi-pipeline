# curation/templates/

The Excel workbook community members fill in to submit data to MalAvi, generated
reproducibly from `build_submission_template.py`.

```bash
.venv/bin/python curation/templates/build_submission_template.py
# -> ImportMalavi_Template_2026-07.xlsx
```

## Why a generator script instead of a checked-in binary

The template is a contract: its sheet names and column headers are what the submission
validator parses. Generating it from code keeps that contract diffable, keeps the
controlled vocabularies in one place, and means a malaviR release bump can regenerate the
dropdowns rather than requiring someone to hand-edit a spreadsheet.

## What changed from Staffan's version

Built directly on `Gupta_ImportMalavi_Example.xlsx` and `Submitting to
MalAvi_2023-07-06.pdf` (repo root). **Sheet names are unchanged** — the community has
been filling in this workbook since 2012 and that familiarity is an asset.

| Change | Reason |
| --- | --- |
| **Red columns removed** | They were MalAvi-internal index numbers (`PARASITE_GENUS_ID`, `REFERENCE_ID`, `SITE_ID`, `COUNTRY_ID`, `Nr`, `SEQ_L/START/END`, `User`, `Vector`) that only the curator can fill. They were roughly half the columns and made the file look far harder than it is. The curator's tooling adds them on ingest. `HOST_SPECIES_ID` is kept — the legacy instructions invite submitters to fill it from NCBI taxonomy — but marked optional. |
| **Dropdowns added** | `ParasiteGenus`, `HostAge`, `HostStatus`, `HostEnvironment` now offer only values MalAvi actually stores. Free-text spelling variants are the biggest source of cleanup work. |
| **`HostEnvironment` added** | MalAvi stores `Wild`/`Captivity` and the Grand Lineage Summary *excludes* captive records — but the legacy template had no column for it, so submitters had no way to declare it. |
| **`DOI` added** to `Reference` | Exact de-duplication key against the publication watcher. |
| **`JOURNAL_ID` renamed `JOURNAL_NAME`** | It always held the journal name (`Proc. R. Soc. B`), and the `_ID` suffix made it look like one of the red index columns. |
| **Instructions embedded** as a `READ ME` sheet | They previously lived in a separate PDF that got detached as the file was forwarded around. |
| **Per-column help** as cell comments | Hover any header for a note on what belongs in that column. |
| **Example row inline**, gray italic, row 3 | Matches the legacy template's behavior. The validator matches it verbatim and ignores it. |

## Controlled vocabularies

Read from the bundled malaviR release `2026-03-23` (`hosts_and_sites`), so a submitter
picking from a dropdown cannot introduce a value the database has never seen:

- `PARASITE_GENERA` — Plasmodium, Haemoproteus, Leucocytozoon
- `HOST_AGE_VALUES` — Adult, Juvenile, Nestling, Adult + Juvenile, Adult + Nestling, Unknown
- `HOST_STATUS_VALUES` — Resident, Migratory, Unknown
- `HOST_ENVIRONMENT_VALUES` — Wild, Captivity

`HOST_STATUS` and `HOST_ENVIRONMENT` are **different variables** and both are needed;
`schemas/data_dictionary.md` previously conflated them and has been corrected.

## The 470 bp rule

`MIN_SEQUENCE_LENGTH_BP = 470` applies to **new submissions only**. A sequence much
shorter than the 479 bp barcode window cannot be shown to be genuinely distinct from an
existing lineage, so it should be re-sequenced rather than named. Applied retroactively it
would implicate **1,109 of the 5,365 lineages (20.7%)** already in the 2026-03-23 release
— so the matcher must still handle short *reference* sequences gracefully. It is a gate on
what comes in, not a filter on what is already there.
