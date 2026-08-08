# MalAvi maintainer runbook

**Who this is for.** Whoever keeps the MalAvi pipeline running — the *maintainer*, not necessarily a
*curator*.

**Why it exists.** To give the maintainer notes on how to keep things going.

**How it is written.** Commands are recorded as they are actually run, with their real
output (and the document is drafted by Claude Code).

**Status: incomplete.** Some sections are not finished.

Each command carries its verification state:

- ✅ run and output checked
- ⏳ verification in progress or not yet done

---

## 0. Intro on the machine needed and Python version ✅

The project is small (a few MB of data) and uses Python and R. **It does not need a cluster.**
The one heavy piece is the GenBank BLAST sweep, which is not part of routine operations.

On BIOMIX (the UD cluster Vincenzo works on — so this step is specific to that setup),
work from a compute node, not the login node:

```bash
srun --pty bash # interactive session
```

**Two Pythons.** The scripts under `curation/` insert
`curation/src` onto `sys.path` themselves, so they run under the system `python3`. The test
suite does not, so it needs the virtual environment where the package is installed:

```bash
.venv/bin/python -m pytest curation -q     # works
python3 -m pytest curation -q              # ModuleNotFoundError: malavi_curation
```

When in doubt, use `.venv/bin/python` for everything. It works for both.

---

## 1. Screen a submission ✅

The core curation command. Takes a fetched submission directory (or a single `.xlsx`) and
writes three files beside it.

```bash
.venv/bin/python curation/check_template.py curation/intake/submissions/<submission dir>/
```

Useful flags:

| Flag | What it does |
|---|---|
| `--no-r` | Skip the malaviR validators. They are then reported as **skipped, with a reason**. Use when R is unavailable, or for a fast pass. |
| `--online` | Allow the checks that need the network (INSDC accession lookup). Off by default so every other check stays reproducible. |
| `--redact` | Print check ids and counts but no submitted values. |
| `--alignment PATH` | Override the reference alignment FASTA. |

What it writes into the submission directory:

- `screen.json` — the sequence screen: `build_name_reservations.py` reads the claimed lineage names out of it, so what gets publicly reserved is exactly what a curator was shown.
- `submission.json` — the workbook as a `schemas/submission.schema.json` record.
- `checks.json` — the full check run.
- `report.pdf` — **the copy a curator gets.** Written here, into the gitignored intake tree;
  **section 1b** is what puts it where a curator can read it. PDF rather than HTML because
  Drive renders a PDF in the browser and will not render HTML, so an HTML report would be a
  download instead of a click. Needs the `report` extra:
  `pip install -e 'curation[report]'` (WeasyPrint; needs cairo and pango system libs).
  Without it, the run still succeeds and says the PDF was not written.
- `report.html` — the same document for the browser. Self-contained: open it in any browser,
  mail it, or serve it. Written owner-only (0600) and refused anywhere outside the
  gitignored intake tree, because it carries unpublished sequences and the
  submitter's email address.

**Exit codes:** `0` clean, `2` if any check raised a blocking issue **or if the run was
incomplete** (a check failed to execute), `3` if the submission had no data template and a
paper-only report was written. An incomplete run is not a pass and does not exit zero.

Runtime is a few seconds without R, roughly 40 seconds with it.

---

## 1b. Send the report to the curators ✅ (verified end to end 2026-08-08)

Screening writes `report.pdf` onto BIOMIX, where no curator has an account. This publishes
it to Drive and emails them that it is there.

```bash
.venv/bin/python curation/publish_report.py --check                 # is delivery configured?
.venv/bin/python curation/publish_report.py --dry-run <submission_id>
.venv/bin/python curation/publish_report.py <submission_id>
.venv/bin/python curation/publish_report.py --all-pending           # everything unsent
```

| Flag | What it does |
|---|---|
| `--check` | Says whether the endpoint and secret are configured, and stops. Never contacts the endpoint, so it is safe on a machine with no network and cannot email anyone by accident. |
| `--dry-run` | Names the report and its size, sends nothing. |
| `--all-pending` | Every submission with a `report.pdf` and no `report_published` entry in the ledger. |
| `--no-notify` | Writes the file to Drive without emailing, for re-sending during debugging. |

**Re-publishing is how you correct a report.** The file is named from the submission id and
the endpoint *updates it in place*, so a link already sitting in a curator's inbox keeps
working and starts showing the corrected version. Running it twice does not make two
reports.

**Why it is not a service account.** It was designed as one; the design was probed on
2026-08-08 and is impossible. A service account has no Drive storage of its own, so a file
it creates in a consumer account's folder is refused with `403 storageQuotaExceeded`
whatever that folder is shared with. Shared Drives would fix it and need Workspace.
Delivery therefore goes to an Apps Script web app running *as* `malaviadmin@gmail.com`.

**Setup is done.** The folder, the secret, the script and the deployment all exist, and a
real report was published and delivered on 2026-08-08. What follows is the procedure for
rebuilding it from nothing — follow it if an account is lost, or if this has to be set up
on someone else's machine.

**One-time setup** — see the header of
`curation/apps_script/publish_report.gs`, which lists it step by step:

1. Create the curator-reports folder in Drive as `malaviadmin`; share it **Viewer** with
   each curator. Never link-share it.
2. `openssl rand -hex 32` → `~/.config/malavi/report_secret.txt` (chmod 600); paste the same
   value into `SHARED_SECRET` in the `.gs`.
3. Paste the folder id into `REPORTS_FOLDER_ID`, edit `CURATORS`, run `testSetup` once.
4. Deploy as a web app: **Execute as: Me**, **Who has access: Anyone**. Put the `/exec` URL
   into `google.report_endpoint` in `config/project.yml`.

When changing the `.gs` later, edit the **existing** deployment and pick "New version".
Creating a second deployment mints a new URL and leaves the old code serving the old one.

**Worth knowing before you rebuild this:**

- Apps Script does not reliably request `script.external_request`, the permission
  `UrlFetchApp` needs. *Creating* a report uses `DriveApp` and works; *replacing* one uses
  `UrlFetchApp` and fails.
  `curation/apps_script/appsscript.json` declares the scopes explicitly to prevent it.
- Google decides whether to re-prompt, and a web
  app can't consent.
  Run **`forceAuthorization`** from the editor: it calls `UrlFetchApp` for real, so the
  dialog appears where a person can click it.

---

## 1c. Tell the submitter what was decided ⏳ (built 2026-08-08, needs a redeploy)

Two automatic messages, one program. The site's name checker tells every visitor that a
curator will confirm their proposed name before they deposit in GenBank; this is what keeps
that promise, and it also makes sure nobody who was turned down is left waiting.

```bash
.venv/bin/python curation/notify_submitters.py --check      # is delivery configured?
.venv/bin/python curation/notify_submitters.py --dry-run    # who is due, and what they would be told
.venv/bin/python curation/notify_submitters.py              # send to everyone due
.venv/bin/python curation/notify_submitters.py <id>         # just this one, with the reason if not due
```

| Ledger state | Message |
|---|---|
| `approved` | The confirmed lineage names, with any name that **changed** called out separately |
| `declined` | Not accepted in its current form, and please reply — carries **no reason** |

**Nothing is sent until the hold has elapsed**, in either direction, and an approval also
requires that no blocking verdict stands.

That wait is the design, not caution. Screening cannot send the confirmation, because the
name is derived from the host species and that is what curators most often correct — a name
confirmed by machine could be withdrawn by a person. Approval alone cannot send it either:
the hold exists so a second curator can still object, and a confirmation posted into that
window can be contradicted after the submitter has used the name. The same wait is applied
to a decline for the plainer reason that it gives anyone a window to undo a mis-click before
a stranger is told unwelcome news. **A slow email is recoverable; a retracted lineage name,
already in a manuscript, is not.**

**The decline notice carries no reason on purpose.** A curator's written reasoning quotes the
submission and is usually a judgment about the data; in an automatic message it becomes an
assertion the reader cannot answer. So the message says what happened, says most declines are
fixable, and asks them to reply — the reasoning travels in a person's answer.

Each message is sent once, guarded by its own history event (`name_confirmation_sent`,
`decline_notice_sent`). They are tracked separately, so a submission that was declined,
reopened and then approved still gets the confirmation it never had.

**Before this works, the Apps Script has to be redeployed.** The endpoint gained two new jobs
and the deployed version has neither: paste the current
`curation/apps_script/publish_report.gs` into the project, then Deploy → Manage deployments
→ edit → Version: **New version**. Until then this program fails with `unusable filename`,
which is the old code refusing a request it does not understand — a safe failure, but an
opaque one if you have forgotten why.

---

## 2. Regenerate the database snapshot ✅

The pre-ingestion gate answers "is this lineage name taken?" and "is this accession already
curated?" from a compact snapshot, so no R call is needed at submission time. Regenerate it
whenever the pinned release changes:

```bash
Rscript curation/r/gate_reference.R > curation/src/malavi_curation/data/db_snapshot.json
```

Without it, the two snapshot-backed checks report as **skipped with a reason**, not as
passed. Verify:

```bash
.venv/bin/python -c "
import json; d = json.load(open('curation/src/malavi_curation/data/db_snapshot.json'))
print(d['source_release'], d['n_lineages'], 'lineages')"
```

---

## 3. Check R is available ✅

Four checks need R and malaviR: `host_name_resolves`, `host_geography_plausible`,
`lineage_previously_recorded` and `sequence_qc`. Without them they are skipped.

```bash
Rscript -e 'suppressMessages(library(malaviR)); cat("malaviR", as.character(packageVersion("malaviR")), "\n"); print(malavi_version())'
```

---

## 3b. Seed the record store ✅ (verified 2026-08-06)

Runs **once**. Turns the last externally produced release into the record store that
`build_release` will generate future releases from.

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'curation/src')
from malavi_curation.config import load_config, repo_root
from malavi_curation.release_seed import seed_store, verify_round_trip
from malavi_curation.release_store import store_dir, write_store
r = load_config()['malaviR']['release']
d = repo_root()/'docs'/'assets'/'downloads'/'tables'
store, report = seed_store(d, r)
check = verify_round_trip(d, r, store)
assert check['clean'], 'seed lost something - do not write it'
write_store(store_dir(repo_root()), store)
print('seeded', {k: len(v) for k,v in store.items()})"
```

The check compares every stored table back against the release CSV it came from; a difference means the import lost
something, and the store is about to become the authority.

---

## 3c. The curator registry ✅ (verified 2026-08-06)

Who (curator) may record a decision lives in `config/curators.yml`. Editing it is a **maintainer**
task. A verdict arriving from an address that is not listed there is filed under
`unrecognized` and never acted on.

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'curation/src')
from malavi_curation.curators import load_registry, leads
print({k: (v.role, v.active) for k, v in load_registry().items()})
print('leads:', [c.id for c in leads()])"
```

Retire a curator with `active: false` rather than deleting the entry; deleting it orphans
every verdict they recorded previously.

---

## 3d. The review loop ✅ (operator commands added 2026-08-07)

`curation/src/malavi_curation/ledger.py` holds the states, the verdict rules and the two
clocks (`config/project.yml` → `review:`: 24h publish hold, 60-day awaiting-submitter
timeout). Three programs drive it, and they run **in this order**:

```bash
# 1. Put every screened submission into the ledger. Idempotent; run it after the screen.
.venv/bin/python curation/enroll.py --dry-run
.venv/bin/python curation/enroll.py

# 2. Read the curator verdict form and apply the decisions. Needs the Drive credential.
.venv/bin/python curation/fetch_verdicts.py --dry-run
.venv/bin/python curation/fetch_verdicts.py

# 3. Run the clocks and refresh the committed decision record.
.venv/bin/python curation/promote.py --dry-run
.venv/bin/python curation/promote.py
```

Exit codes are the same convention throughout: **0** clean, **2** something wants a
person's attention (a response that could not be parsed, a submission sitting untouched),
anything else means the program itself could not run. A `2` is not a failure.

**Run these on BIOMIX, by hand or from a local scheduler.**
The review ledger has to persist and it lives in the gitignored intake tree,
which the Actions runner discards at the end of every job. A scheduled run there would
start from an empty ledger every time: the 24-hour hold would never elapse, because the
approval that started it would be gone. The submissions workflow is stateless on purpose
and this part is not.

`promote.py` writes `data/decisions.json`, which **is** committed. Commit it after a run
that changed something.

Three things to know before anything writes to the ledger:

- The ledger lives in the **gitignored** intake tree (`review_ledger.json`), because verdict
  reason text quotes unpublished data. What gets committed is the *decision record* —
  `ledger.decision_record()` which carries no reason text, so a withdrawn submission can
  be erased while "what did we decide, and when" stays answerable.
- `due_actions()` returns **proposals**, never applied automatically. `promote.py` applies
  only the awaiting-submitter timeout. It reports release-eligibility rather than acting on
  it because `released` means *published* and only a release build can make that true.
- Nothing in this loop decides anything. Every rule is re-checked by `transition()` at the
  moment of the write, so a hold recorded between a scan and a write still wins.

To check the rules themselves without touching real data:

```bash
.venv/bin/python -m pytest curation/tests/test_ledger.py curation/tests/test_curators.py \
    curation/tests/test_verdicts.py curation/tests/test_fetch_verdicts.py \
    curation/tests/test_enrollment.py curation/tests/test_promote.py -q
```

---

## 4. Community submissions ✅ (the two feed builders) / ⏳ (the fetch)

```bash
# ⏳ not yet verified here (it reaches out to the Form; safe, read-only GETs)
.venv/bin/python curation/fetch_submissions.py

# ✅ both verified
.venv/bin/python curation/build_name_reservations.py --dry-run   # names that would be claimed
.venv/bin/python curation/build_site_feeds.py --dry-run          # queue + contributors feeds
```

`--dry-run` prints and writes nothing. Run it before the real thing; the reservation feed
is public and carries a name and a date only.

Fetched submissions are gitignored because they hold unpublished data and submitter email
addresses. They are re-fetchable at any time.

---

## 5. Tests ✅ (all three, verified 2026-08-08)

Run all three suites.

```bash
.venv/bin/python -m pytest curation -q                  # ✅ Python: the curation package
node docs/assets/js/tests/test_sequence_check.mjs       # ✅ JS: the browser checker
cd /mnt/ellisbiostore/malaviR && Rscript -e 'devtools::test()'   # ✅ R: malaviR
```

The Python suite takes about 90 seconds and passed 686 tests as of 2026-08-08.

The JS suite runs **every lineage in the release** through the browser checker, so allow
minutes rather than seconds; it reported 135 passed / 0 failed on 2026-08-08. It is a
required pre-publish step: the error it exists to prevent is telling someone a sequence is
new when MalAvi already names it.

The R suite reported 321 passed / 0 failed on 2026-08-08, in about two minutes, with **3
skipped**: the BLAST tests need DECIPHER >= 3.0 and Biostrings, which are not installed
here. Skipped is not passed — if the BLAST path ever matters, install those two and re-run
rather than assuming the suite covered it.

---

## 6. Rebuild the site's data files ✅ (all five, verified 2026-08-08)

Run after the pinned release changes. Each writes into `docs/assets/data/`.

```bash
Rscript export/build_bird_names.R      # ✅ the eBird/Clements checklist the name checker uses
Rscript export/build_site_stats.R      # ✅ every figure on the site
Rscript export/build_sequence_index.R  # ✅ the sequence checker's index
Rscript export/build_tables_json.R     # ✅ the browsable tables
Rscript export/build_reports.R         # ✅ the QC report CSVs
```

**All five were run on 2026-08-08 and every output was byte-identical to what the site was
already serving, apart from a `generated` timestamp that no page displays.** That is the
result worth having: the live figures were not stale, and these scripts reproduce them
exactly. The timestamp-only changes were reverted rather than committed, so that the next
diff in `docs/assets/data/` means something.

Useful numbers from that run, for recognizing a wrong one later: 18,493 host-and-site rows,
5,368 lineage-summary rows, 526 references, 5,367 distinct lineage names over 5,359 distinct
sequences (7 sequences carry more than one name), 123 countries and 564 studies.

`build_bird_names.R` writes ~319 KB (88 KB gzipped, which is what Pages serves):
11,167 species, 2,377 genera and 1,088 resolvable synonyms from the clootl 2025
snapshot bundled in malaviR. Add `--dry-run` to any of them to report without writing.

---

## 7. Publishing the site ✅ (every step verified 2026-08-08)

```bash
Rscript export/build_site_stats.R
Rscript export/build_sequence_index.R
node docs/assets/js/tests/test_sequence_check.mjs      # must pass
publish/push_site.sh --dry-run                         # inspect first
publish/push_site.sh
```

Each of these ran successfully on 2026-08-08, though as separate steps over the course of a
session rather than as one continuous pass — so the individual commands are verified and the
sequence as a whole has not been executed start to finish in one go.

The publish itself was confirmed against the live site, not just by the script's own exit
code: the changed strings were fetched back from `https://malavi-db.github.io/` and the
replaced ones confirmed absent. That is the check worth repeating — GitHub Pages redeploys
a minute or two after the push, so a green push is not yet a published page.

Only `docs/` is ever copied to the public repo. See `publish/README.md`.

The site now lives at **<https://malavi-db.github.io/>**, served from
`malavi-db/malavi-db.github.io`. The old personal-account address stopped working the moment
the repository was transferred: GitHub redirects repository URLs but **not** Pages sites.

---

## 8. Building a release ✅ (verified 2026-08-07)

Produces the `MalAvi_<date>.zip` that `malaviR/data-raw/process_release.R` consumes, from
the record store seeded in step 3b. Always dry-run first and read the diff:

```bash
# what would change, writing nothing
.venv/bin/python curation/build_release.py --dry-run \
    --diff-against docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv

# build it
.venv/bin/python curation/build_release.py --release 2026-08-07 \
    --diff-against docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv
```

Writes `data/releases/MalAvi_<release>.zip` (five `.xlsx` tables + the `.fas` alignment)
and `release_report_<release>.json`. Both are gitignored; the archived ZIP for a release
that ships belongs in `malaviR/data-raw/`.

**The Grand Lineage Summary is regenerated every build.** In other words, it is not made by
hand. That is useful because it catches problems: if some lineages never got their location
data added, the rebuild fixes them and the difference shows up immediately. Seeing those
differences is what `--diff-against` is for, and it is why you should read the diff before
shipping a release.

The region columns come from `reference/country_regions.csv`, which maps all 125 country
names in the release to MalAvi's twelve regions. It is curator-maintained: 98 rows were
inferred from the release itself, 3 settled by elimination, and 24 authored by hand where
neither was possible. Those 24 are the only rows that are somebody's decision rather than a
reading of Staffan's, so they are the ones to check; each row records its own basis and
evidence. A country in the records that is not in that file sets **no** region, and the
build warns.

To confirm the archive reads the way malaviR reads it:

```bash
Rscript -e 'suppressMessages(library(readxl)); \
  d <- tempfile(); unzip("data/releases/MalAvi_2026-08-07.zip", exdir = d); \
  f <- list.files(d, "GrandLineageSummary.*xlsx$", recursive = TRUE, full.names = TRUE); \
  x <- read_excel(f); cat(nrow(x), "rows x", ncol(x), "cols\n")'
```

---

## Not yet written up

- The publication watcher (`watcher/`), which runs weekly in dry-run and has never
  announced a paper. **Testing it is deliberately deferred (decided 2026-08-08):** it is
  secondary to getting the rest of the loop running with real curators. The watcher's job is
  to find *new* work, and there is no point generating a queue while ~15 papers from the
  existing forward set are still unprocessed. Turn it on when curators are keeping up.
- The PDF/table extraction path (`python -m malavi_curation.intake`).
