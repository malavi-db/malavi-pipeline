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

## The order things actually happen in

**Read this before using any section below.** The rest of this file is a reference manual —
one section per job, numbered in the order the jobs were built, which is **not** the order
they are run in. Section 7 publishes the site and section 8 builds the release; doing them
in that order publishes the old release and then builds the new one. Section 4 is where a
submission arrives, and it sits after section 3e, where one is ingested.

This is the sequence. Written 2026-08-14, after a review pointed out that the last mile of a
release was documented nowhere and four of its steps appeared in no section at all.

### A. A submission arrives and is reviewed

Steps 1 to 11 can be run as one command, `curation/intake_cycle.py` (§4b): a preview
by default, `--live` to act. The table is still the reference for what each step is.

| # | Do this | § |
|---|---|---|
| 1 | `fetch_submissions.py` — pull new responses out of Drive | §4 |
| 2 | `check_template.py` per new directory — **this mints the permanent public id** | §4 |
| 3 | `build_site_feeds.py` and `build_name_reservations.py`, `--dry-run` first | §4 |
| 4 | commit the three feed files | §4 |
| 5 | **`publish/push_site.sh`** — or the public queue and the reserved-name feed stay stale, and that feed is what protects a submitter's claim | §7 |
| 6 | `enroll.py` — put the submission in front of the curators | §3d |
| 7 | `publish_report.py --check`, then `--dry-run`, then for real | §1b |
| — | *wait for curators* | |
| 8 | `fetch_verdicts.py` — **writes by default**, so dry-run first | §3d |
| 9 | if a correction was filed: `apply_corrections.py --apply`, then re-run steps 2 and 7 for the new revision | §3da |
| 10 | `promote.py`, then commit `data/decisions.json` | §3d |
| 11 | `notify_submitters.py` — **only after the three-day hold has elapsed, and nothing schedules it** | §1c |
| 12 | `close_submission.py --apply` if it ended; `lift_embargo.py --apply` if it was held | §3db, §3f |
| 12b | if it was already ingested when it ended: `ingest_submissions.py --retract <id> --apply`, or no release can be built at all | §3e |

### B. Building and publishing a release

| # | Do this | § |
|---|---|---|
| 12c | **only when Lund sends a database**: `import_master_db.py --db MALAVI.sqlite --release <date>` dry-run, read it, then `--apply`; then `lund_changes_report.py` and send Staffan the PDF | §3bb |
| 13 | `ingest_submissions.py --release <date> --apply` | §3e |
| 14 | fill the blank order/family/continent/`SEQ_LENGTH` columns in `data/records/`, and commit | §3e |
| 15 | `build_release.py --dry-run --diff-against …`, **read the diff**, then build for real | §8 |
| 16 | read the edition report; copy the *public* one into `docs/` by hand | §8b |
| 17 | archive the ZIP into `malaviR/data-raw/` | §8 |
| 18 | in malaviR: `Rscript data-raw/process_release.R` **under the R 4.5 conda environment** (`source /opt/miniforge3/etc/profile.d/conda.sh && conda run -p ~/conda_envs/malaviR Rscript data-raw/process_release.R`; system R 4.3 has DECIPHER 2.30 and the BLAST index step fails with `IndexSeqs is not an exported object`), then `Rscript data-raw/build_taxonomy.R`, bump `DESCRIPTION` + `NEWS.md`, `R CMD INSTALL --no-multiarch .`, run the tests | *no section — see the header of that script* |
| 19 | bump `malaviR.release` in `config/project.yml` to the new date | *no section* |
| 20 | `Rscript curation/r/gate_reference.R > curation/src/malavi_curation/data/db_snapshot.json` | §2 |
| 21 | `Rscript curation/r/gazetteer.R > curation/src/malavi_curation/data/gazetteer.json` | *only in `curation/README.md`* |
| 22 | **all seven** export scripts | §6 |
| 23 | all three test suites | §5 |
| 24 | `push_site.sh --dry-run`, then for real | §7 |
| 25 | `bash backup/lifeboat.sh` | §9b |

### Things to know before you start

- **Two opposite conventions for writing.** `enroll`, `fetch_verdicts`, `promote`,
  `build_site_feeds` and `build_name_reservations` **write by default** and take
  `--dry-run` to preview. `ingest_submissions`, `apply_corrections`, `close_submission`,
  `lift_embargo` and `correct_store` **refuse to write** without `--apply`. Habit formed on
  one family is a trap in the other; the direction that costs you is running
  `fetch_verdicts.py` expecting a preview and actually recording the decisions.
- **Step 11 is the one that gets skipped**, and its failure is silence: a submitter waits
  indefinitely for the name confirmation this site promised them before they deposit in
  GenBank. Nothing runs it for you.
- **Nothing logs what you ran, unless you use the intake cycle (§4b),** which writes
  every command and its output to `logs/`. Otherwise only the two bash scripts write to `logs/`. If you stop
  half way, the record of where you stopped is `data/decisions.json`, `data/corrections.csv`
  and your own shell history.
- **Four things must agree at the end**: the release the site advertises
  (`site_stats.json`), the download files that exist, `malaviR.release` in
  `config/project.yml`, and the release `db_snapshot.json` was built from. Each is a
  separate manual step above (22, 22, 19, 20) and nothing checks that they match.
- **Budget two sittings.** The three-day hold between approval and notification means a
  release that starts with a new submission cannot finish the same day.

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

### Building `.venv` from scratch ✅ (written and checked 2026-08-13)

Every command in this runbook starts `.venv/bin/python`, and until 2026-08-13 nothing said
how to make it. Four different extras sets appeared across the docs and none was the union,
so rebuilding from the instructions gave a partial install that failed at the first
WeasyPrint or Drive call.

```bash
cd /mnt/biostore-all/Vellis/malavi_rebuild
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e "./curation[all]"
```

`[all]` is the union of every extra, held that way by a test — add an extra to
`curation/pyproject.toml` and `all` has to reference it or `test_packaging.py` fails. The
pieces, if you ever want one on its own: `pdf` (pdfplumber), `tables` (openpyxl,
python-docx), `fetch` (beautifulsoup4), `report` (WeasyPrint), `google` (google-auth),
`dev` (pytest).

Check it:

```bash
.venv/bin/python -m pytest curation -q     # see §5 for the current count
```

Two things this does **not** cover:

- **WeasyPrint needs system libraries** (cairo, pango). They are present on BIOMIX. Without
  them `report` installs but the PDF step reports that it could not run — the HTML report is
  still written, so this is degraded, not broken.
- **The watcher does not use this environment.** It runs under the system `python3` and
  needs `biopython`, which is deliberately not here. See `watcher/`.

There is no lock file. The versions that work today are recorded in
`results/METHODS_draft.md`; nothing pins them.

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

### The full alignment in the report (added 2026-10-02)

Under each alignment figure the report carries the whole 479-column window of the
submitted sequence and its nearest lineages, folded in the HTML ("Show the full
alignment") and printed in the PDF. It is the same rows the figure and the distances were
computed from. The PDF gets about a page per sequence longer (000008: 15 to 24 pages); to
drop it from the PDF only, add `details.fullaln { display:none; }` to the `@media print`
block of `report_html.py`.

---

## 1b. Send the report to the curators ✅ (verified end to end 2026-08-08)

Screening writes `report.pdf` onto BIOMIX, where no curator has an account. This publishes
it to Drive and emails them that it is there. Since 2026-10-16 a first report is also
**assigned** to one curator and the submitter's contact goes with it to the status sheet
(§3g); the program prints the assignee.

```bash
.venv/bin/python curation/publish_report.py --check                 # is delivery configured?
.venv/bin/python curation/publish_report.py --check --live          # is the DEPLOYED script the repository's?
.venv/bin/python curation/publish_report.py --dry-run <submission_id>
.venv/bin/python curation/publish_report.py <submission_id>
.venv/bin/python curation/publish_report.py --all-pending           # everything unsent
```

| Flag | What it does |
|---|---|
| `--check` | Says whether the endpoint and secret are configured, and stops. Never contacts the endpoint, so it is safe on a machine with no network and cannot email anyone by accident. |
| `--check --live` | Sends one signed, read-only request (the endpoint's `describe` action) and prints the deployed script's `CODE_VERSION`, the actions it answers and how many curators and how many lead addresses its live lists hold, beside what the repository expects. Emails nobody, writes nothing. Exit 0 = current, 2 = a redeploy is owed, 1 = the check could not run. Added 2026-10-02, because nothing used to say which code was live. |
| `--dry-run` | Names the report and its size, sends nothing. |
| `--all-pending` | Every report that may be sent and has not been: the submission is not in `submissions.exclude`, is in the review ledger, is in a live state, and has no `report_published` entry. Whatever is held back is printed with the reason. (Until 2026-10-02 it selected every directory with a `report.pdf`, test submissions included.) Since 2026-10-02 each publish records the revision it described, and a submission whose latest revision is newer than its last report is pending again (the file is replaced in place), so a correction or an update is re-sent without a typed command. A report that fails to send no longer loses the record of the ones sent before it; the run ends 1 and the failed one is retried next time. Naming a submission id on the command line bypasses these filters on purpose. |
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
2. `openssl rand -hex 32` → `~/.config/malavi/report_secret.txt` (chmod 600).
3. In the project: paste `publish_report.gs` and, as a second file, `settings.gs`. Under
   Project Settings → Script Properties set `REPORTS_FOLDER_ID`, `SHARED_SECRET` (the
   same value as on BIOMIX), `CURATORS` and `ATTENTION_TO` (comma-separated). Run
   `showSettings` until it says READY, then `testSetup` once. No live value is ever
   written in the `.gs` (since 2026-10-14; before that they were pasted into the code).
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

## 1c. Tell the submitter what was decided ⏳ (built 2026-08-08; the redeploy is step 10 of results/CHECKLIST_browser_2026-10-16.md)

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

> ⚠️ **The first redeploy after 2026-10-14 is a migration: set the Script Properties BEFORE
> pasting.** Until 2026-10-14 the live values (folder id, secret, curator list, attention
> list) were constants in the code, and the repository copy carried placeholders, so
> pasting it blanked them all. The scripts now read every live value from the project's
> Script Properties (`curation/apps_script/settings.gs`), which survive a paste. Order:
>
> 1. In the live project, open the code and copy the four current values (also in
>    `CUSTODY_PRIVATE.md`). For the two lists, the addresses only, comma-separated: no
>    quotes, no brackets (`showSettings` flags an item that is not a plain address).
> 2. Project Settings (gear) → Script Properties → add `REPORTS_FOLDER_ID`,
>    `SHARED_SECRET`, `CURATORS` (comma-separated), `ATTENTION_TO` (comma-separated).
> 3. Files → "+" → Script, name it `settings`, paste `settings.gs` in.
> 4. Paste the current `publish_report.gs` over `Code.gs`.
> 5. Run `showSettings`: it must end READY. Then `testSetup`: it checks the folder and the
>    secret and sends you one sample email. A `CURATORS` list of one address is reported
>    as one; `publish_report.py --check --live` (below) also prints how many addresses the
>    live list holds.
> 6. Deploy → Manage deployments → edit → New version.
>
> From then on, pasting a script blanks nothing, and a change is: paste, `showSettings`,
> new version.
>
> **After deploying, check it from BIOMIX:** `.venv/bin/python curation/publish_report.py
> --check --live` must end "The deployment is current." On 2026-10-02 it answered
> "REDEPLOY NEEDED": the live script predates the `describe` action itself. A deployment
> that old refuses the request with `unusable filename` before writing or emailing
> anything, and the check reports that as an old deployment. When you change what the
> script does, change `CODE_VERSION` in the `.gs` so this check can tell.
>
> The same migration applies to the other projects, each with its own Script Properties.
> **For a notifier there is no step 6: a saved paste is live at once**, so steps 2 and 3
> must be finished before step 4, or every form response in between fails. They are:
> the Submission Notifier (`CURATORS`, `SUBMISSION_FORM_ID`), the verdict notifier
> (`CURATORS`, `VERDICT_FORM_ID`), the update form (`CURATORS`, `UPDATE_FORM_ID`) and the
> inspector (`VERDICT_FORM_ID`; the id is `review.verdict_form_edit` in
> `config/project.yml`). Each project needs `settings.gs` pasted in beside its script.

---

## 1d. Deploying the Apps Script projects with clasp ⏳ (written 2026-10-14, not yet set up)

Optional. Pasting is safe since the Script Properties change (§1c), so nothing depends on
this; clasp turns "paste, showSettings, new version" into one command run from BIOMIX or a
laptop. It changes nothing about where the scripts run: they run in Google, on their
triggers, whatever uploads them.

**What clasp is.** Google's command-line tool for Apps Script. A project is its `.gs`
files plus `appsscript.json`; `clasp push` uploads the local copies over the project's
files, `clasp deploy` makes a new version of a web app live. It needs Node (BIOMIX has
v18) and a one-time browser login as `malaviadmin`.

**One-time setup (about 15 minutes, you at a browser, Claude at a terminal):**

1. On BIOMIX, in a directory outside the repository: `npm install @google/clasp` (local
   install; `npx clasp` runs it). Do not install it globally on a shared cluster.
2. Turn the Apps Script API on for the account: as `malaviadmin`, open
   https://script.google.com/home/usersettings and switch "Google Apps Script API" on.
   Without it every `clasp push` is refused with a one-line error that names this page.
3. `npx clasp login --no-localhost`. It prints a URL: open it in the browser **signed in
   as malaviadmin**, approve, paste the code back. The credential lands in
   `~/.clasprc.json` (mode 600; it is an OAuth token for the account with Drive and
   Apps Script scopes, so treat it like the report secret: never in the repository,
   `backup/lifeboat.sh` does not copy it on purpose, revoke it from the account's
   security page if the machine is lost).
4. For each project, once: find its script id (Project Settings → IDs → Script ID) and
   record it in `CUSTODY_PRIVATE.md`. Then, in a scratch directory per project:
   `npx clasp clone <script id>` pulls the live files down. Compare them with the
   repository copies (`diff`) before the first push: the live project may hold edits that
   never reached the repository.
5. **`clasp push` replaces the project's whole file set with the local directory**, and
   deletes what is not there. So each project needs its own directory holding exactly
   its main script, `settings.gs` and that project's own `appsscript.json`: never the
   flat `curation/apps_script/` directory (nine files, several of which define the same
   function names, and the form *builders*, which must never sit in a notifier project),
   and never a directory without `settings.gs` (the push would delete it from the live
   project and every function would fail with `settingList_ is not defined`). The
   repository's `appsscript.json` lists the publisher's scopes (Drive, external
   requests, mail); a notifier project needs Forms and the script service instead, so
   do not push the publisher's manifest into a notifier. A `.clasp.json` per project
   holds only the script id and the root directory; decide whether the ids may be
   committed (they are not credentials, but they are not in the public manifest today).

**Deploying a change, afterwards:**

```bash
cd <project dir>; npx clasp push           # uploads the project's files; the live "Code" file is renamed to the repository name (publish_report etc.), which is expected
npx clasp deploy -i <deployment id> -d "2026-10-14"   # publisher only: a new version of the EXISTING web app
.venv/bin/python curation/publish_report.py --check --live   # must end "The deployment is current."
```

The notifiers run from triggers and need only the push. `clasp deploy` without `-i`
creates a *new* deployment with a new URL, which is the same trap as in the editor (§1b);
the deployment id is under Deploy → Manage deployments.

**What it does not do.** It does not set Script Properties (those stay in the browser or
are set once by a tiny function), does not install triggers (`installTrigger` in the
editor, or `npx clasp run installTrigger` after enabling the run API) and does not merge
the four projects into one. Merging is the natural next step once clasp exists, because
it makes `CURATORS` one property instead of four, and it is a separate, planned change.

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

## 3bb. Import an updated master database from Lund ✅ (built and run 2026-09-15)

Runs when Staffan sends `MALAVI.sqlite`, the relational database the flat releases used
to be exported from. Done once on 2026-09-15 with every dataset from his waiting list;
from that import on the store is where MalAvi is curated, so this should not be a routine
step. It is written to be repeatable in case it has to be.

The database is **private and never published**: `*.sqlite` is gitignored, it is not in
`publish/public_manifest.txt`, and the export written from it goes to the gitignored
`data/master_exports/<release>/`. Only the committed report
`data/master_imports/import_<release>.json` (hash, counts, findings, merge, corrections
replayed) and the store itself are tracked.

```bash
# what the import would do, writing nothing -- read the reproduction check first
.venv/bin/python curation/import_master_db.py --db MALAVI.sqlite --release 2026-09-15

# do it
.venv/bin/python curation/import_master_db.py --db MALAVI.sqlite --release 2026-09-15 --apply

# the document for Staffan: everything that differs between the last release he
# produced and the database as we read it, plus the corrections applied on top
.venv/bin/python curation/lund_changes_report.py --release 2026-09-15 --previous 2026-03-23
```

**Read the reproduction check before `--apply`.** The export rules were measured against
the 2026-03-23 release (the docstring of `master_db.py` lists the ones that are not
obvious from the schema). Against that release the check should show vector data
identical and the other tables gaining rows and losing almost none; hundreds of rows
differing on one derived column means a rule is wrong, not that Staffan edited hundreds
of rows. Then read the merge: identities are kept for identical rows and for edited rows
paired by natural key; new rows are `_source=seed`, `_added=<release>`; removed rows are
named. Then the corrections replay: every decision in `data/corrections.csv` is planned
again and re-applied where the database still carries the fault, so the import cannot
quietly undo a correction. A row **deleted** here by hand (the duplicated Pramual
reference) is not a logged correction and does come back; `ops/DATA_ISSUES.md` says so.

After the import the release steps run as usual from step 15. The Staffan document lands
in `data/releases/lund_changes_<release>.{html,pdf,json}` (gitignored: it names
unpublished studies). Send him the PDF; the merge report is ours, that document is his.

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

## 3ca. Appoint a curator — the whole checklist ✅ (written 2026-09-01)

Adding someone to `config/curators.yml` makes their verdicts **count**. It does not make
them **arrive**, and it does not let them **read** anything. Those are three separate
grants in three different places, and a curator who has only the first will be trusted by
the software and never hear from it. Do all five steps, in this order.

Everything except step 1 is done in a browser, signed in as the operational account.

**1. The registry — the only step in this repository.**
Add an entry to `config/curators.yml` (see §3c for the shape). `role: curator` unless they
have agreed to be a lead; `active: true`. Commit it. Verify with the snippet in §3c: the
new id must appear with the role and `active=True` you intended.

**2. The Apps Script notification lists — this is what sends email.**
`script.google.com`, signed in as the operational account. Each project holds a `CURATORS`
Script Property (Project Settings → Script Properties), and each needs the address:

| Project | File | After editing the property |
|---|---|---|
| MalAvi report publisher | `publish_report.gs` | run `showSettings` (READY); no new version needed, the property is read on every request |
| MalAvi Submission Notifier | `notify_on_submission.gs` | run `showSettings`; live at once |
| MalAvi verdict notifier | `notify_on_verdict.gs` | run `showSettings`; live at once (once installed; see §3d) |
| MalAvi update form | `create_update_form.gs` (its notifier) | run `showSettings`; live at once (once the form exists; see §4a) |

> **Since 2026-10-14 the list is the `CURATORS` Script Property of each project**
> (Project Settings → Script Properties), not an array in the code: edit the property, run
> `showSettings`, done; no redeploy is needed for a notifier, and the publisher needs a
> new version only when its code changed. Before 2026-10-14 the list was an array in the
> code and pasting the repository copy blanked it along with the folder id and the secret;
> that trap is gone (§1c describes the one-time migration). Each project holds its own copy
> of the property, so a new curator is added in every project that has one.

Someone in the registry but not in these lists is a curator whose verdicts count and who
is never told a submission arrived. That is the right state for someone who has agreed to
help but has not been told yet — and the wrong state for anyone else.

**3. Drive sharing — this is what lets them read.** Three folders, all **Viewer**, all
**General access: Restricted**, none ever link-shared. Ids are in `CUSTODY_PRIVATE.md`.

- **The curator-reports folder.** Without it the report link in their email opens a
  permission error, which is the failure they will hit first and the one that looks most
  like the system being broken.
- **The two submission upload folders.** The arrival email lists the submitter's uploads
  as links; a curator who cannot open them is shown a door with no key. Note the standing
  exception in `config/project.yml`: these two are for **appointed** curators only, not for
  someone helping test the machinery.

**Verify by reading the Share list, not by remembering having done it.** For each of the
three folders, open Share and read the list of people: every active curator's address must
be there as Viewer, *including their aliases* (a curator whose registry record carries a
Gmail alias needs the Gmail on the list, because that is the account they will be signed in
as when they click). Found the hard way on 2026-09-24: the lead's own udel address was
missing from an upload folder and the "submitted files" link in a report answered "request
access". A pending access request on a folder is a sign that this step was skipped for
somebody; approve it as Viewer, never Editor.

**4. Check they can actually answer the verdict form.** The form asks Google to verify the
responder's address, so the address in the registry has to be one they can *sign in to
Google with*. A personal Gmail always is. An institutional address only is if the
institution runs Google Workspace, or they have attached it to a Google account.

Check this before the first report goes out, not after. If an address will not work, the
two ways out are:

- add a Google-capable address as an `aliases:` entry on their registry record — the
  verdict still resolves to them (§3c); or
- have them reply by email and record the verdict yourself with `--actor` (§3db), which
  attributes it correctly and leaves the same audit trail.

**5. Send them the guide.** `docs/curating.html` on the live site, and the example report
PDF it links to. That page is the whole job description; there is nothing else to teach.

**And warn them about spam.** The first MalAvi emails to a new address (the arrival notice
and the report link) come from an account their mail provider has never seen, and Gmail in
particular files them under spam. Tell every new curator, in the appointment email: *search
your spam folder for "MalAvi" and mark the sender as safe*. This happened to a curator on
2026-09-23; two reports sat unread in spam and the missing email was investigated as a
pipeline fault for a day before anyone looked there.

### Afterwards: check the number of leads

Only a **lead** can clear a hold placed by another curator. With one lead, every
disagreement between two other curators has to come back to that one person, and if they
are unavailable the submission sits. Appointing curators is the moment to ask whether a
second lead is wanted — it is a one-word change in `config/curators.yml`, and it is not a
courtesy to hand someone unasked.

### Removing a curator

The reverse of the above, and it is four places, not one: `active: false` in the registry
(never delete — see §3c), the address out of **every** `CURATORS` property (§3ca step 2
lists the projects), and access removed on all three Drive folders.

---

## 3d. The review loop ✅ (operator commands added 2026-08-07)

`curation/src/malavi_curation/ledger.py` holds the states, the verdict rules and the two
clocks (`config/project.yml` → `review:`: 72 h (three-day) publish hold, 60-day awaiting-submitter
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
start from an empty ledger every time: the three-day hold would never elapse, because the
approval that started it would be gone.

The submissions fetch was believed to be stateless and therefore safe there. It was not:
it mints the permanent public identifiers, which is state of exactly the same kind. It moved
to BIOMIX on 2026-08-13 — §4.

`promote.py` writes `data/decisions.json`, which **is** committed. Commit it after a run
that changed something.

It is currently `{"decisions": []}` — no submission has been enrolled yet, so there is
nothing to record. That is deliberate rather than an oversight: until 2026-08-13 the program
returned early on an empty ledger and the file did not exist at all, which is
indistinguishable from "this has never run". It is the **only committed thing** that will
resolve a submission id later — the review ledger and `submission_ids.json` are both
gitignored — so it exists from now on, and the first real decision arrives as a diff to a
tracked file rather than as a new file nobody has reviewed the shape of.

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

### The verdict notifier ✅ (written 2026-10-02; part of the curation project since 2026-10-16)

A response on the verdict form used to tell nobody anything until step 2 above was run by
hand (`results/PROTOCOL_REVIEW_2026-10-02.md` §1). `curation/apps_script/notify_on_verdict.gs`
is the form-submit trigger that fixes that: the moment a response is submitted it emails
every curator, the one who clicked included, with who recorded what and every answer, and
one honest line: this is what was entered on the form, and it takes effect when the ledger
next reads the form. Since 2026-10-16 it also writes the status sheet and, on a **flag**
whose "Message to the submitter" field is filled, emails the submitter that text as
written (§3g). It is installed with everything else by `installAllTriggers` (§3g); the old
separate "verdict notifier" project was never installed and is not needed.

An entry from an address that is not in the `CURATORS` property is sent to the first
address only, with a line saying so, and never produces a message to a submitter: the form
accepts a response from anyone who has its link.

It does not replace `fetch_verdicts.py` and changes nothing about what a verdict does. The
fetch still applies the rules and still sends the ledger's own notice (verdict id,
resulting state, the link that answers a flag), and since 2026-10-16 it records the
submitter message in the ledger (`Verdict.note_to_submitter`) and says in its notice whether
one was written.

The email's wording and the submitter path are tested without Google
(`curation/tests/test_apps_script_verdict_notice.py` runs the script under Node with stand-ins
for the mail service and an in-memory spreadsheet), and the same file checks that the
verdict words and question titles it reads are the ones `create_verdict_form.gs` builds.

To check the rules themselves without touching real data:

```bash
.venv/bin/python -m pytest curation/tests/test_ledger.py curation/tests/test_curators.py \
    curation/tests/test_verdicts.py curation/tests/test_fetch_verdicts.py \
    curation/tests/test_enrollment.py curation/tests/test_promote.py \
    curation/tests/test_apply_corrections.py curation/tests/test_close_submission.py -q
```

---

## 3g. The curation project: status sheet, assignment, inbox triage, daily clock ✅ (built 2026-10-16; switched on by the browser checklist)

**What it is.** One Apps Script project, "MalAvi curation" (the report publisher with more
files; same `/exec` URL), that does on Google's own triggers what used to need a person:

| Piece | When | What |
|---|---|---|
| Status sheet | always | A private Google Sheet, "MalAvi submissions", Viewer for each curator: one row per submission with the ledger's state, what it is waiting on, the **assignee**, the report link, the verdicts, when the submitter last wrote and was last written to; plus Verdicts, Mail, Arrivals, Curators and Log tabs. The ledger stays the authority; `synced_at` says how old the sheet's copy is. |
| Assignment | when a first report is published | The endpoint picks the least-loaded curator from `ASSIGNEES` (default `CURATORS`), ties by rotation, skipping anyone whose Curators-tab row says `away_until` a future date or who has reached their `max_open`. The assignee's email says "needs your verdict"; the others are "in copy". A corrected report keeps its assignee. `reassign` (edit two lines, run) moves one by hand. |
| Submitter message | on a flag | The verdict form's "Message to the submitter" goes to the submitter verbatim, from MalAvi, flagging curator and assignee in copy, recorded on the sheet. Not on Accept/Reject, not for a stranger's entry, not without an address on the sheet (then the curators are told so). |
| Inbox triage | every 15 min | Each new message in malaviadmin's inbox is matched to a submission (id in the text, else the sender's address against the sheet), labelled `MalAvi/filed` + `MalAvi/<id>` and logged on the Mail tab. Only a **submitter's own** mail is **forwarded to the assignee and the leads** (reply-to set to the submitter; nobody already on the message; at most 10 forwards a run and never when the daily send quota is nearly spent). A curator's reply-all or a stranger quoting a public id is filed, not forwarded. Unmatched mail is labelled `MalAvi/unfiled`, logged, never forwarded, listed in the digest. Hand-written mail from the account to a submitter is logged as outgoing. |
| Daily clock | 07:00 UTC | Assigns the unassigned backlog; reminds the assignee at 7 days and everyone at 14 (counted from the assignment); **reassigns at 21** (the relieved curator is told, not nagged); reminds the flagging curator to write after 3 days if the submitter has neither been written to nor replied; reminds a waiting submitter at 14 and 30 days from `waiting_since`, never after they have replied (then the leads are told a curator must file the answer); warns 7 days before the 60-day dormancy; sends the leads one **digest** with the whole queue, the alerts and the unfiled mail. One email per person per day, throttled by the row's `last_reminder`. |
| Ledger sync | end of every intake cycle | `curation/push_status.py` sends the ledger's view (state, waiting-on, hold end, report, submitter contact, the registry) to the endpoint's `status` action, which writes the sheet. Never a reason's text (`report_delivery.build_status_payload` refuses any other key). |

```bash
.venv/bin/python curation/push_status.py --dry-run     # the rows the sheet would get
.venv/bin/python curation/push_status.py               # write them (also step 12 of the cycle)
.venv/bin/python curation/apps_script/build_bundle.py  # rebuild dist/malavi_curation.gs after any .gs edit
.venv/bin/python curation/publish_report.py --check --live   # is the deployed code 2026-10-16 with `status`?
```

**Switching it on (browser, as malaviadmin, about 40 minutes):**
`results/CHECKLIST_browser_2026-10-16.md`, in short: set the Script Properties; replace the
manifest with `curation/apps_script/appsscript.json`; paste `curation/apps_script/dist/malavi_curation.gs`
over `Code.gs`; run `showSettings` (READY), `testSetup` (two [SAMPLE] emails to the leads),
`createStatusSheet` (sets `STATUS_SHEET_ID`; record it in `CUSTODY_PRIVATE.md`),
`addSubmitterMessageQuestion`, `installAllTriggers`, `showTriggers`; Deploy → Manage
deployments → **New version**; delete the old submission notifier project's trigger. Then
from BIOMIX: `publish_report.py --check --live`, `push_status.py`, and in the project
`triageInboxNow` and `dailyClockNow` once.

**Updating it later:** edit the `.gs` file in the repository, `build_bundle.py`, run the
tests (`test_apps_script_verdict_notice.py` keeps the bundle current), paste the bundle
over `Code.gs`, save; for a change to the endpoint also push a New version. Script
Properties survive every paste. If `build_bundle.py` refuses, two files declare the same
function: rename one.

**Knobs without code:** `ASSIGNEES` (who is in the rotation), `ARRIVAL_TO` (who hears an
arrival), `UPDATE_FORM_URL` (the link in submitter messages), and the Curators tab's
`away_until` / `max_open` columns, which the ledger sync never overwrites. The reminder
days are constants at the top of `daily_clock.gs`.

**What it does not do.** It never changes a verdict, a state or a name: the ledger does
that, on BIOMIX or in Actions. It never forwards or relays unmatched mail. It never sends a
submitter the "Why?" text. Gmail's own forwarding of malaviadmin's mail to the custodian is
untouched; switch that off by hand when the digest is enough.

**Appointing a curator** (§3ca) now also means: the `CURATORS` property of this one
project (grant 2), and the Curators tab fills itself from `config/curators.yml` at the next
cycle. Removing one: the property, the registry; their open assignments are reassigned by
`reassign` or by the clock at 21 days.

---

## 3h. The mail guard rails ✅ (built 2026-10-16, after the nine duplicate notices; reviewed; NOT yet pasted or live)

**What happened.** On 2026-10-16 the first live cycle with the review state in the
repository re-applied all 13 historical verdict-form responses and emailed the curators 9
duplicate notices (a new form column had changed every row's fingerprint). Vincenzo's rule
since then: no incorrect or unneeded email, in development or in production. Design and
the two reviews: `results/GUARDRAILS_PLAN_2026-10-16.md`.

**Where mail can be stopped, from the outside in:**

| Switch | Where | Values | Unset means |
|---|---|---|---|
| `MAIL_MODE` | Script Property of the "MalAvi curation" project | `live` sends; `redirect` sends everything to `MAIL_REDIRECT_TO` with the real recipient in the subject; `dry` sends nothing, records and counts | **dry**: a pasted bundle sends nothing until the property is set; `showSettings` says so |
| `MALAVI_MAIL_MODE` | environment variable on BIOMIX or the runner | `live`, `redirect`, `dry` | **dry**: a program posts nothing that carries mail and says `held`; the cycle REFUSES `--live` until it is set |

Every message on the Google side goes through `mail.gs` (`sendMail_`, `forwardMail_`):
the recipient rules (a curator message only to an address on `CURATORS`/`ATTENTION_TO`/
`ASSIGNEES`; a submitter never sees another submitter's id), the mode, the duplicate check
(each message has a key: the report's bytes, the response announced, the day of a reminder;
the key plus the recipient is remembered 30 days in Script Properties, written BEFORE the
send and forgotten if the send throws), the per-execution cap `MAIL_MAX_PER_RUN` (25) and
the daily quota reserve `MAIL_QUOTA_RESERVE` (20). A held message is recorded on the Mail
tab with its reason (`kind`, `mode`, `key` columns), counted for the day, reported to the
Python side (`held: [{to, reason}]`), and ONE alarm a day goes to the first lead saying how
many were held. The daily digest carries the mail-guard line. A test refuses any `.gs`
file that sends without `mail.gs`.

On the machine side `report_delivery` raises `MailHeld` for anything the endpoint held
(a duplicate is not an error), so nothing is recorded as sent that was not: a report is
sent on the next run that can send; a verdict's notice is kept `pending` in
`verdicts_applied.json` and retried on each run for 14 days; a submitter notice is not
recorded in the history until it went.

**The bulk refusals (config/project.yml, `review:`):** `fetch_verdicts.py` refuses more
than `max_new_verdicts_per_run` (12) new responses, and ANY new response older than the
newest already applied (the signature of a re-apply; this rule alone stops the 10-16
class at zero emails); `publish_report.py --all-pending` refuses more than
`max_reports_per_run` (6); `notify_submitters.py` more than
`max_submitter_notices_per_run` (5). Each refusal is exit 2 with the list. Override one
deliberate run with `--allow-bulk` / `--allow-old`; for the unattended cycle set the
repository variable `MALAVI_ALLOW_BULK_UNTIL=YYYY-MM-DD`.

**A stale checkout may not write.** With the state in the repository, `fetch_verdicts.py`,
`publish_report.py` and `notify_submitters.py` fetch and refuse when the checkout is behind
its remote (`malavi_curation.repo_state`); the cycle pulls first and marks its children
with `MALAVI_IN_CYCLE=1`; `--standalone` skips the check when the remote is unreachable.

**The ledger's verdict notice** now goes to the leads only for a flag or a reject (the
form's own trigger already told every curator; the hold id and the clear-hold link are a
lead's tools), to everyone for an accept, an override and a retraction.

```bash
# Development: nothing is sent. The default.
.venv/bin/python curation/fetch_verdicts.py --dry-run
# A live run that may send: say so.
MALAVI_MAIL_MODE=live .venv/bin/python curation/intake_cycle.py --live
# Receive everything yourself first: the request asks the endpoint to redirect, which it
# honors whatever its own MAIL_MODE says (a request can only make the mode stricter):
MALAVI_MAIL_MODE=redirect .venv/bin/python curation/intake_cycle.py --live
.venv/bin/python curation/publish_report.py --check --live    # the endpoint's mode and today's held count
.venv/bin/python -m pytest curation/tests/test_mail_guard.py curation/tests/test_apps_script_mail.py -q
```

**Switching it on (browser, as malaviadmin):** Project Settings > Script Properties: add
`MAIL_MODE` = `redirect` and `MAIL_REDIRECT_TO` = your address; paste
`curation/apps_script/dist/malavi_curation.gs` over `Code.gs` (only a bundle
`build_bundle.py` reports as REVIEWED); run `showSettings` (READY), `showMailStatus`,
`sendMailTest` (arrives at your address with `[REDIRECT to ...]` in the subject); Deploy >
Manage deployments > New version (the endpoint's replies changed); from BIOMIX
`publish_report.py --check --live` (version 2026-10-16-mailguard, mail mode redirect); run one
cycle with `MALAVI_MAIL_MODE=redirect` and read what you receive; then set `MAIL_MODE` to
`live`. The checklist with every click: `results/CHECKLIST_browser_2026-10-17.md`.

**The review rule (CLAUDE.md, `curation/REVIEWS.md`).** Every change to a guarded file
(all `.gs`; `GUARDED_PYTHON` in `ops/record_review.py`) gets an independent review before
it is committed, pasted or run live. `test_review_log.py` fails until
`ops/record_review.py` has recorded the review of the current hashes; the Actions
workflow runs that test before a live cycle; `build_bundle.py` prints REVIEWED or NOT
REVIEWED.

---

## 3da. Apply a correction a curator filed ✅ (built 2026-08-13)

A curator who spots a fixable error flags the submission and describes the fix on the same
form. `fetch_verdicts.py` records that as a *proposed* correction; a lead curator approves
it with a second response. Neither of those changes anything. **This is the step that turns
an approved correction into a revision**, and until 2026-08-13 it did not run at all.

```bash
# what is approved and waiting, and what a lead has not looked at yet
.venv/bin/python curation/apply_corrections.py

# record them
.venv/bin/python curation/apply_corrections.py --apply

# or just one submission
.venv/bin/python curation/apply_corrections.py --submission MALAVI-SUB-2026-000004 --apply
```

Run it **after** `fetch_verdicts.py` and before regenerating the report, because the whole
point of a revision is that the curators re-read the corrected version.

What to expect:

- **The submitted workbook is never edited.** It is what the submitter sent, and the
  appendix in every report is built from it. A correction is recorded *over* it as a new
  revision, so both the original and the corrected reading survive.
- **Applying a correction clears every standing approval**, including from curators who
  were perfectly happy. They approved a different version. The submission needs re-screening
  and a fresh decision.
- **One revision per correction**, because a revision carries a single authority and a
  single list of who was consulted, and two corrections can disagree on both.
- It reads the ledger, not the verdict sheet, so re-running it is safe: an applied
  correction carries `applied_at` and is never picked up again.
- A correction whose flag has since been withdrawn is **refused**, not applied — at that
  point the submission is back in ordinary review and somebody may already have accepted it.

> ⚠️ **This program changes no data, and exits 3 to say so.** The correction is free text a
> curator typed; nothing parses it, deliberately — a program that guessed which cell "the
> host is *Turdus merula*, not *T. migratorius*" meant would be inferring data from prose.
> So the value itself changes in one of two places, both of which need you:
>
> 1. **The submitter sends a corrected workbook.** Re-screen and re-ingest; the fix arrives
>    with their rows, because ingest reads the workbook.
> 2. **You correct the store** with `correct_store.py` (§8a) *after* this submission has
>    been ingested — the path for a fix the curators settled between themselves, where
>    there is nothing to ask the submitter for. Before ingest there is nothing to edit but
>    the workbook, which stays untouched by design.
>
> The run prints the exact follow-up for each correction, and exits **3** rather than 0
> until one of the two has been done. Until 2026-08-14 it exited 0 and said nothing: the
> revision bumped, the report regenerated from the unchanged workbook and said exactly what
> it had said before, curators re-approved, ingest wrote the uncorrected value, and every
> gate reported success.

---

## 3db. Close a submission — decline, withdraw, or wait ✅ (built 2026-08-13)

The three states nothing else could produce. Before 2026-08-13 a rejected submission stayed
in `held` forever: its reserved names were never given back, the 60-day clock never started,
and the submitter was never told anything.

```bash
# we will not include it — needs a reason (see below)
.venv/bin/python curation/close_submission.py --submission MALAVI-SUB-2026-000004 \
    --decline --reason unresolved_objection --actor vaellis@udel.edu --apply

# the submitter emailed and took it back
.venv/bin/python curation/close_submission.py --submission MALAVI-SUB-2026-000004 \
    --withdraw --apply

# we asked them something and are waiting; starts the 60-day clock
.venv/bin/python curation/close_submission.py --submission MALAVI-SUB-2026-000004 \
    --ask --apply

# they came back with resequenced data; revive the ORIGINAL submission
.venv/bin/python curation/close_submission.py --submission MALAVI-SUB-2026-000004 \
    --reopen --actor vaellis@udel.edu --apply
```

Leave off `--apply` to see what it would do. Exit **1** means the ledger refused the move
and says why; **2** means the command itself was wrong (unknown submission, bad reason).

**A lead can now close a submission from the verdict form** — "Close a submission for good
(lead curators only)", added 2026-08-13, which goes through `ledger.decline` and checks that
the responder really is a lead. That is the right route for a curator's own judgment, and
this program is no longer the only way to reach `declined`.

What still belongs here: `--withdraw` (the submitter took it back — it arrives as an email,
so there is nobody on the form to record it), `--ask`, and `--decline` **on a lead's
instruction** when the lead is not at a browser. The ledger tells the two decline routes
apart: the form attributes it to the lead's own curator id, this one to whatever `--actor`
says.

**Reject on the form is still not this.** It lands the submission on `held` on purpose, so a
rejection gets a second look rather than being terminal on one person's say-so.

`--reason` is a closed vocabulary because it reaches `data/decisions.json`, the one
committed file that must contain no unpublished science. For `--decline`:

| code | means |
|---|---|
| `duplicate` | already in MalAvi |
| `out_of_scope` | not avian haemosporidian data |
| `unresolved_objection` | a flag was never answered |
| `data_not_verifiable` | the records could not be checked against the source |
| `superseded` | replaced by another submission |

A withdrawal is always `withdrawn_by_submitter` and takes no `--reason`; `--ask` takes none
either, because nothing is being disposed of.

What to expect:

- **`--decline` and `--withdraw` release the reserved lineage names. `--ask` does not, and
  neither does the dormancy that follows it** (changed 2026-08-20 — see "Rejection and
  resubmission" below). All of them drop the submission out of the public queue, which
  lists live submissions only; `--reopen` puts it back on.
- **An approved submission cannot be declined.** Somebody has to flag it first, and that
  flag is attributed to them — a decline follows an objection, it does not replace one.
- **A withdrawal is terminal with no way back.** A decline can be reopened, and that is
  logged as a deliberate act.
- `--ask` **sends nothing**. Asking the question is an email a human writes; this records
  that we are waiting, which is what makes the clock run.
- After a decline, `notify_submitters.py` sends the decline notice once the same three-day
  wait an approval gets has elapsed — here it gives anyone a window to notice a mistake
  before a person is told their work was refused. Run `notify_submitters.py` after this.
- **`--reopen` refuses a live submission**, and that refusal is load-bearing rather than
  fussy. `held -> in_review` is a legitimate transition — it is what clearing a hold does —
  and `transition()` guards only `approved` and `released`, because the lead-only rule and
  the consultation record live in the override path. Without the check, `--reopen` would
  walk a held submission back to `in_review` with the objection still standing and nobody
  recorded as having cleared it. It also refuses `withdrawn` and `released`, which are
  terminal.

### Rejection and resubmission — reopen the original ✅ (built 2026-08-20)

**The commonest curator action is not a verdict. It is "resequence this and send it back."**
Staffan Bensch, 2026-08-20: indels and grouped SNPs are things he catches often, and his
routine response is to write to the submitter and ask for the sequence to be re-read.

When that data arrives, **reopen the original submission — do not treat it as a new one.**

```bash
# 1. the corrected workbook goes into the ORIGINAL submission directory
#    (reopening moves the ledger, not files)
# 2. revive it
.venv/bin/python curation/close_submission.py --submission <id> --reopen \
    --actor vaellis@udel.edu --apply
# 3. re-screen, which rebuilds the report from the new workbook
.venv/bin/python curation/check_template.py curation/intake/submissions/<dir>/
# 4. send the curators the corrected report; the endpoint updates it in place, so
#    the link already in their inbox starts showing the new version
.venv/bin/python curation/publish_report.py <id>
```

Why not just have them file the form again: a fresh submission mints a **new identifier**
and a **new date**, and name reservation priority is earliest-timestamp-wins. A submitter
who spent three months at the bench because we asked them to would come back behind anyone
who claimed their lineage name in the meantime. Reopening keeps the identifier, the original
date and the claim.

**A dormant submission keeps its reserved names indefinitely.** The 60-day timeout used to
give them back; since 2026-08-20 it does not. Dormancy records that we have stopped
expecting a prompt answer — not that the submitter forfeited anything. If the name went back
and somebody else were issued it, MalAvi could end up holding two different sequences under
one name, which is the single failure the reservation system exists to prevent, and the
person penalized would be the one who did what we asked. **Declining is now the only thing
that returns a name**, which is why `dormant -> declined` was added to the transition
table.

---

## 3e. Ingest approved submissions into the record store ✅ (rehearsed 2026-08-10)

This is the step where an approval becomes data. Until it runs, a curator can approve a
submission and nothing carries its records into MalAvi — the store still holds only what
step 3b seeded.

```bash
# what would be ingested, writing nothing
.venv/bin/python curation/ingest_submissions.py --release 2026-08-14

# do it
.venv/bin/python curation/ingest_submissions.py --release 2026-08-14 --apply
```

`--release` is required and is written into `_added` ("since when has MalAvi held this
record?"). Give it the release these records will actually first appear in, which is not
necessarily today.

**It ingests exactly what a release would publish, because it asks the same function**
(`release_gate.admissibility`). An embargoed submission is the case that matters: it is
approved, so a looser ingest would write its rows, and `build_release` would then refuse to
build *any* release while they sat in the store.

By default it ingests every approved submission whose rows the store does not yet hold, and
leaves the rest alone. To re-ingest after a correction, name it:

```bash
.venv/bin/python curation/ingest_submissions.py --release 2026-08-14 --apply \
    --submission MALAVI-SUB-2026-000123
```

**A record id is never reissued.** New rows get ids above the highest ever issued, which
`data/records/id_high_water.json` remembers even after a row is retracted or retired
(`write_store` keeps it current; commit it with the store). Until 2026-09-15 the first
gap was reused, so the lineage ingested after STRALU01's retirement would have taken
STRALU01's id.

**The name a submission was approved under is the name that gets written.** A proposed
lineage name MalAvi already owns is a warning at screen time, not a block: the report
offers a free alternative and approving the submission adopts it, which the ledger records
in `name_corrections`. The ingest applies that rename across every table carrying a
`LINEAGE_NAME` and reports it:

```
note: TUMIG10 -> TUMIG32 in 2 row(s): the submitter's proposed name was already a
      MalAvi lineage, and this is the name agreed when the submission was approved
```

If the name is still taken after that — because no rename was agreed — the submission is
**refused and nothing of it is written**:

```
REFUSED MALAVI-SUB-2026-000004: TUMIG10 is already a lineage in MalAvi (KF314763) and no
rename was applied: the sequences differ, so this would put two lineages under one name.
```

Fix it by recording the agreed name in the ledger and ingesting again. Two rows under one
lineage name break every join that treats the name as a key, duplicate a tip label in the
alignment, and cannot be untangled afterwards, so this refuses rather than warning.

**A sequence that is not in the barcode reading frame is refused the same way**
(added 2026-08-20):

```
REFUSED MALAVI-SUB-2026-000006: NECMON01: 479 bp beginning at frame position 3 is not a
shape a correctly processed barcode arrives in (479 bp at position 1 = Sanger (full
window); 476 bp at position 2 = leuc; 478 bp at position 2 = haem). ...
```

The length check inside `lineage_rows` was not enough: NECMON01 was **exactly 479 bp** and
still two bases out of the window, which put it at 39% identity to its own clade instead of
92%, and it passed in silence. The screen had already worked this out and the curator report
had drawn the registered copy — ingest read the raw workbook cell and threw that away. This
is the re-check at the moment of the write.

**Short sequences are normal and are not refused.** 3,340 of MalAvi's 5,368 lineages hold
fewer than 479 unambiguous bases — `ABSUP01` has 326 real bases behind 153 leading gaps.
Sequencing with the forward primer only, or a read that failed at one end, gives a perfectly
good partial barcode. It is padded into the window with `-` (the character the store uses:
79,500 of them against 800 `N`) and stored, so it is indistinguishable from the partial
lineages MalAvi already holds. Padding is arithmetic, not judgment, and the note says what
was done.

**What is refused is a placement that would discard real bases.** A partial barcode fits
inside the window and loses nothing. NECMON01 was 479 bp *beginning at position 3*, so two
real bases fell past the end — that is a mis-trimmed read, not a partial one. The same test
catches an untrimmed 5′ primer and an over-length amplicon. Unlike a length, or a fixed set
of shapes, this holds for any read: it is the difference between "shorter than the window"
and "doesn't fit in the window".

It refuses rather than quietly trimming, because the same 2-base shift is produced both by a
mis-windowed read (harmless — re-place it) and by an indel near the 5′ end (a sequencing
error — re-read it), and a program that silently fixed the frame would settle that question
by itself and hide the second case. **The workbook is never edited either way.**

Two things to read in the output:

- **The notes.** Values MalAvi cannot source are left blank and reported, not guessed — the
  order and family of a host it has never seen, the continent of a country it has never
  recorded, `SEQ_LENGTH` (which is a curator's `Full`/`Partial` judgment, not a
  measurement). Fill these in **in the store**, then commit. Order and family then look
  after themselves: the row is one of MalAvi's own records afterwards, so the next ingest
  of that host reads them back out.
- **"N value(s) the store holds would be emptied."** A re-ingest maps the workbook over the
  submission's rows, so anything a curator typed into a column the template has no source
  for (`ALT_NAME`, `STUDY_TYPE`) would be lost. The run **refuses** and writes nothing.
  Either put the value in the workbook, or accept the loss with `--allow-blanking`.

The store is git-tracked: read the diff before committing it. Nothing here is marked
released — step 8 does that, once a ZIP exists.

Exit codes follow the usual convention, with one addition: **3** means the store was
written but at least one submission was refused and needs a person.

### Taking a submission back out — `--retract` ✅ (built 2026-08-14)

```bash
.venv/bin/python curation/ingest_submissions.py --release 2026-08-14 \
    --retract MALAVI-SUB-2026-000004            # report what would be removed
.venv/bin/python curation/ingest_submissions.py --release 2026-08-14 \
    --retract MALAVI-SUB-2026-000004 --apply    # remove it
```

**When you need this.** Rows enter the store at *approval*, before any release exists. The
release gate then refuses any source whose ledger entry is not approved or released. So a
submitter emailing "please withdraw it" the day after ingest — or a curator putting a late
hold on it, which the publish-hold design actively encourages — used to stop
`build_release.py` **completely**, on every subject, until somebody edited five CSVs by
hand. The only escape was `--i-am-overriding-the-approval-gate`, which publishes the
withdrawn submitter's records: the opposite of what they asked for.

The guard is deliberately inverted. This refuses a submission the gate is *happy* with —
those rows belong in the store, and taking them out would lose them silently. Close or
hold the submission first, then retract. It also refuses anything already `released`:
those rows are published, and removing them is a correction to a release (§8a,
`correct_store.py`), which leaves its own record in `data/corrections.csv`.

Nothing else moves. Rows from `seed` or from any other submission are untouched, the same
way they are on the way in.

```bash
.venv/bin/python -m pytest curation/tests/test_ingest_submissions.py \
    curation/tests/test_store_ingest.py curation/tests/test_release_gate.py -q
```

---

## 3f. Release records that were held back ✅ (built 2026-08-13)

A submitter with unpublished data can ask us to hold their **records** until their study is
out. Review carries on around it — the submission is screened, curators decide, and their
lineage names are reserved and confirmed. Only publication waits.

Normally you never run this: `publish_reference.py` (below) lifts the embargo itself, since
publication is the event the submitter was waiting for. Use this when the study is not ready
to be renamed yet, or to see what is being held.

```bash
# what is held, and which study each one is for
.venv/bin/python curation/lift_embargo.py

# the paper is out
.venv/bin/python curation/lift_embargo.py --reference "Barrow et al unpubl" --apply

# or by id, if the study cannot be read off the workbook
.venv/bin/python curation/lift_embargo.py --submission MALAVI-SUB-2026-000004 --apply

# a submitter who filed expecting to publish, then asked us to wait
.venv/bin/python curation/lift_embargo.py --submission MALAVI-SUB-2026-000004 \
    --set --note "author asked on 2026-08-20" --apply
```

**Lifting is not publishing.** The records still have to be ingested and released: §3e, then
§8. Until 2026-08-13 the embargo could be set and never lifted — `publish_reference` was the
only thing that lifted one and it looked for its submissions in the record store, which by
construction never holds an embargoed submission's rows.

**For a study whose records are entirely embargoed, the order matters:**

```
1. lift_embargo.py --reference "<name> unpubl" --apply
2. ingest_submissions.py --release <date> --apply     # rows arrive, still "unpubl"
3. publish_reference.py "<name> unpubl" "<name> 2027" ... --apply
```

`publish_reference.py` refuses step 3 before step 2 and prints this sequence, because adding
the reference row first would make the real rename refuse later ("curated twice").

Reference names are compared **exactly**, as `REFERENCE_NAME` is everywhere else. A
near-miss spelling finds nothing and shows you the listing — guessing here would publish
somebody's unpublished data on a resemblance.

```bash
.venv/bin/python -m pytest curation/tests/test_embargo.py -q
```

---

## 4. Community submissions ✅ (the two feed builders) / ⏳ (the fetch)

**Run this on BIOMIX, in this order.** It used to be a GitHub Actions workflow
(`.github/workflows/submissions.yml`, removed 2026-08-13) — see the box below for why that
could not work.

```bash
# 1. bring in what the Form has received
.venv/bin/python curation/fetch_submissions.py

# 2. screen each one. THIS is where a submission gets its permanent identifier.
for dir in curation/intake/submissions/*/; do
    .venv/bin/python curation/check_template.py "$dir"
done

# 3. rebuild the three public feeds
.venv/bin/python curation/build_site_feeds.py --dry-run          # queue + contributors
.venv/bin/python curation/build_site_feeds.py
.venv/bin/python curation/build_name_reservations.py --dry-run   # names that would be claimed
.venv/bin/python curation/build_name_reservations.py

# 4. commit the feeds
git add docs/assets/data/queue.json docs/assets/data/contributors.json \
        docs/assets/data/reserved_names.json
git commit -m "Refresh submission feeds"

# 5. publish them to the live site (feeds only -- no pages, no styles, no scripts)
publish/push_feeds.sh --dry-run
publish/push_feeds.sh
```

> **Steps 3 and 5 usually run themselves.** `enroll.py`, `fetch_verdicts.py`,
> `promote.py` and `close_submission.py` each call
> `malavi_curation.public_feeds.refresh()` when they have actually changed something, so
> the public queue follows a curator's decision without anyone remembering to rebuild it.
> The block above is the manual path, for when you have changed something they did not —
> a new fetch, an edited exclusion list — or when an automatic publish reported a failure.
>
> Three things make that automatic publish safe to leave on:
>
> * it runs `push_feeds.sh`, **not** `push_site.sh`. That script can only ever move the
>   three generated JSON files. A verdict recorded while `docs/` holds a half-edited page
>   cannot put that page on the public web;
> * it **never fails the run that called it**. The ledger write has already happened and
>   is the record. A dead network prints a note telling you to run `push_feeds.sh`, and
>   the verdict stays recorded;
> * it does nothing on a `--dry-run`. Pass `--no-publish` to any of the four to skip it
>   on a real run as well.
>
> **An approval publishes no visible change on the day it is made.** The queue applies the
> same rule as `notify_submitters.py`: an approval is not public until its three-day publish
> hold has elapsed, because a queue that said "Accepted" immediately would have to walk it
> back in front of the submitter when a late hold landed. So the normal output of an
> automatic publish right after an approval is *"the published queue was already current"*.
> It flips when the hold has elapsed, on whichever run happens next — which in practice is the
> `notify_submitters.py` run you make after the hold. See
> `build_site_feeds.public_review_state`.

Exit codes from the screen: **2** means findings to review, **3** means a paper with no
template. Neither is a failure. Anything else means the screen itself did not run.

`--dry-run` prints and writes nothing. Run it before the real thing; the reservation feed
is public and carries a name and a date only.

Fetched submissions are gitignored because they hold unpublished data and submitter email
addresses. They are re-fetchable at any time.

> ### Why this cannot run in CI
>
> The same reason the review loop cannot (§3d), and it took a second form. A submission's
> public identifier — `MALAVI-SUB-2026-000123` — is minted once and never changes, and the
> mapping from the private intake directory to that identifier lives in
> `curation/intake/submissions/submission_ids.json`, which is **gitignored**, because it is
> the one file where the submitter's name and their public id appear together.
>
> A runner never has that file. It re-fetched everything into a fresh filesystem, minted its
> own sequence from 1 in whatever order the directories sorted, committed `queue.json`
> carrying those numbers, and discarded the mapping when the job ended. The next run did it
> again.
>
> It happened to agree with this machine's numbering, because intake directories are named
> by timestamp so sorted order is append-only. That is luck. One superseded submission, one
> directory that exists only here (`20260806T210800_DEMO_Testsubmission` already does), or
> one partial fetch shifts every number after the gap — and a public identifier a submitter
> was given starts pointing at somebody else's submission.
>
> `build_site_feeds.py` now **never mints**, on any path. A submission with no identifier is
> not published, and the program says so loudly. So the failure is no longer possible; what
> replaced it is this manual sequence.
>
> **BIOMIX cannot schedule it** — scrontab is disabled cluster-wide and users have no
> crontab. Run it by hand when a submission arrives (the arrival email tells you), or from a
> scheduler on your own machine. That is already true of §3d, and the two belong together.

---

## 4a. Updates to a submission without a full resubmission ⏳ (built 2026-10-02; the form is not created yet)

A submitter answering a flag writes a sentence, or attaches the corrected template to an
email, and does not want to fill in the submission form again. They should not: a fresh
submission mints a new id and a new date, and the names they reserved would be lost to
anyone who claimed them in between (§3db). This is the route that keeps all three.

**The form:** `curation/apps_script/create_update_form.gs` builds "MalAvi — update a
submission" (submission id, what changed, the corrected template as an upload) and holds
its notifier, which emails every curator when an update arrives. **Apps Script cannot add
a file-upload question**, so the script leaves the form unfinished and `finishByHand()`
in it lists the steps; its log prints the responses sheet id and the public URL, which go
in `config/project.yml` as `submissions.update_responses_sheet` and
`submissions.update_form_url`. Share the sheet Viewer with the fetcher's service account,
and the upload folder Viewer with each curator (§3ca).

**The rule, written down:** any curator may fill this form in on a submitter's behalf,
from an emailed answer or an attached spreadsheet. It does not default to the maintainer.

**The program:**

```bash
.venv/bin/python curation/fetch_updates.py --dry-run
.venv/bin/python curation/fetch_updates.py
```

For each response not filed yet: the submission is found by its id; its current
workbook(s) and the screen's outputs (`report.html`, `report.pdf`, `screen.json`,
`checks.json`) are moved to `_superseded/<timestamp>/` inside its directory, kept; the
uploaded file(s) are downloaded into the directory with the submitter's note beside them
(`update_<timestamp>.json`); and a new revision is recorded in the ledger (authority
`submitter`), which clears the earlier approvals. The directory then has no `report.html`,
so the intake cycle (§4b, step 1) screens it again, and `publish_report.py --all-pending`
sends the new report because the revision is newer than the last report sent. A response
with words and no file is kept as the note and reported as needing a person (exit 2): a
curator reads it and files it as a correction, or asks for the template. An update to a
dormant or declined submission is filed and reported for a lead to reopen (§3db). Every
filed response is recorded in `fetched_updates.json`, so nothing is filed twice. With no
sheet configured the program prints one line and exits 0.

```bash
.venv/bin/python -m pytest curation/tests/test_fetch_updates.py -q
```

---

## 4b. The intake cycle: table A as one command ✅ (built 2026-10-02; preview verified, not yet run live)

`curation/intake_cycle.py` runs steps 1 to 11 of table A, in that order, each as its own
program with its own command line. It decides nothing: every rule stays in the program
that enforces it. It exists because every one of those steps was a command typed by hand,
none needs a person on the normal path, and nothing logged what was run.

```bash
.venv/bin/python curation/intake_cycle.py            # PREVIEW: every step is a dry run
.venv/bin/python curation/intake_cycle.py --live     # really run it
.venv/bin/python curation/intake_cycle.py --live --no-email
```

**A preview is the default and writes nothing.** Each program runs with its own
`--dry-run`. The two kinds of step that have no dry run (the screen, the two commits) are
not run; the preview names what they would have been run on. A `--live` run fetches,
screens, publishes the feeds to the public site, emails the curators their reports,
applies verdicts and emails submitters whose hold has elapsed, so it has to be asked for.

| Step | What runs | Notes |
|---|---|---|
| 1 | `fetch_submissions.py`, then `fetch_updates.py` (§4a) | the fetch exits 2 when a file could not be downloaded; the directory is marked `.download_incomplete` and is not screened until the file arrives |
| 2 | `check_template.py <dir> --redact` | only for a directory that is not excluded, is not marked incomplete, and has no `report.html` (the last file the screen writes; `screen.json` is written too early to mean "screened") |
| 3 | `build_site_feeds.py`, `build_name_reservations.py` | |
| 4 | `git commit -- <the three feed files>` | only when one changed; commits those paths and nothing else in the tree |
| 5 | `publish/push_feeds.sh` | skipped, with step 4, if a feed did not rebuild |
| 6 | `enroll.py` | |
| 7 | `publish_report.py --all-pending` | see §1b for what it will and will not send |
| 8 | `fetch_verdicts.py` | |
| 9 | `apply_corrections.py` (never `--apply`) | reports a correction that is waiting as needing a person; recording it and changing the data stays §3da |
| 10 | `promote.py`, then `git commit -- data/decisions.json` if it changed | |
| 11 | `notify_submitters.py` | the hold guard is in that program; exits 2 when a confirmation is refused because two submissions claim one name |
| 12 | `git commit -- <the three feed files>` again | steps 6 to 11 rebuild the feeds when they change a state |

**What stops the cycle and what does not.** A step that cannot run (any exit code not
listed for it) stops the steps after it, because they read what it was meant to leave
behind. Five do not: a screen, the two feed builders, the feeds commit and the feeds
push. One unreadable workbook must not hold up everybody else's reports, and the feeds
are a publication of the ledger, not a source for it, so GitHub being unreachable must
not stop verdicts and notices. Either still ends the cycle with exit 1.

**Exit codes** are the convention of the programs it runs: **0** clean; **2** the cycle
ran and at least one step wants a person (a file that did not download, a response that
could not be parsed, a name collision, a correction waiting, a stale submission, a refused
confirmation); **1** a step could not run.

**A preview can under-report, and is not entirely free of side effects.** `promote.py` and
`fetch_verdicts.py` return 0 from a dry run before working out whether a person is
needed, so "the preview ran clean" does not promise that a live run will. Feeds are not
written in a preview, so step 4 cannot say whether a live run would commit.
`push_feeds.sh --dry-run` refreshes the local clone of the site repository and writes its
own log. Nothing is sent, committed or published, and no submission, ledger or feed file
is written.

**The log** is `logs/intake_cycle_<UTC timestamp>.log`: every command, its exit code and
everything it printed. `logs/` is gitignored, and the log names submitters, so it stays on
the machine.

**The email.** A live run that ends 2 or 1 sends one message to the lead curators through
the report endpoint (`attention_notice`; the recipients are the script's own
`ATTENTION_TO` list, never taken from the request), carrying each step that needs a person
and the last 40 lines of its output, with every submitter's name cut out of the directory
names. A clean run sends nothing. **The same email is not sent twice within seven days**: a
standing condition (a stale submission, a name clash) would otherwise mail the leads on
every run; what was last sent is remembered in `logs/intake_cycle_last_attention.json`, and
anything different is sent at once. This needs the redeployed endpoint (§1c): until then
the email step fails with a printed note and the exit code and the log are the record.

**What it does not do:** record a correction, close a submission, lift an embargo, ingest,
build a release, or `git push` the private repository (its two commits stay local until
you push).

**One at a time.** It holds `logs/intake_cycle.lock`; a second cycle started while one is
running exits 1 and does nothing.

**How to bring it into use** (plan item 3.2): run the preview once a day for a week and
read the log; then run `--live` by hand once a day in place of the table A commands. A
scheduler comes later and elsewhere: `scrontab` is disabled on BIOMIX, and the plan is to
run this same program from a scheduled workflow once the ledger's state can travel
(`results/PLAN_2026-10-02.md`, items 4.3 and 4.4).

```bash
.venv/bin/python -m pytest curation/tests/test_intake_cycle.py -q
```

---

### Running it from GitHub Actions ⏳ (written 2026-10-14; workflow in place, NOT enabled)

`.github/workflows/intake_cycle.yml` runs the cycle four times a day on GitHub's runner,
so that fetching, screening, reporting and the clocks stop waiting for a login to BIOMIX.
It is a **preview** until the repository variable `MALAVI_INTAKE_MODE` is set to `live`,
and a preview on the runner stops at the fetch unless the Google key is present, so the
workflow can be merged and watched for weeks before it does anything.

**What makes it possible: the review state in the repository.** Until 2026-10-14 the
ledger, the id map, the applied-verdicts record, the fetch records and every screen's
outputs existed only on the machine that ran the cycle. With `intake.state_in_repo` on
(config/project.yml; the workflow sets `MALAVI_STATE_IN_REPO=1` for itself), a live cycle:

- **step 0** refuses to start if anything under the inbox is modified and uncommitted,
  then `git pull --ff-only`, and refuses if the pull cannot fast-forward. So two writers
  cannot both write the ledger: whoever pulled last writes, the other is refused and the
  log says so. Nothing is ever merged.
- **step 13** force-adds the named state files (the inbox stays gitignored; the list is
  `STATE_TOP_FILES` and `STATE_DIR_FILES` in `intake_cycle.py`, never a glob, so a
  submitter's workbook or paper can never be swept in), commits them if anything is
  staged, and pushes when `intake.push_state` is on (`MALAVI_PUSH_STATE=1` in the
  workflow; a runner that did not push loses its state with the job).

A hand run on BIOMIX with the setting on behaves the same way, which is how the two
machines share one ledger: pull, run, commit, push. Since 2026-10-16 the programs the
cycle runs also refuse, on their own, to write from a checkout that is behind its remote
(section 3h), and a live run needs `MALAVI_MAIL_MODE` set (the workflow sets `live` for a
live run and `dry` for a preview); `MALAVI_ALLOW_BULK_UNTIL` (a repository variable)
authorizes one unattended bulk apply.

**The decision this waits on (plan of 2026-10-14, open decision 2).** Committing the
state puts the ledger's reason text, which quotes what was wrong with unpublished data,
and submitters' names and addresses (`metadata.json`) into the private repository. The
reports already quote the same data and every curator already receives them by email,
and the repository is private and is what a successor inherits; still, it is the
maintainer's call, which is why both settings are off in the config and the workflow
is a preview. To switch on: (1) decide; (2) on BIOMIX, with `state_in_repo: true`, run
one live cycle by hand so the first state commit comes from the machine that holds the
state, and push; (3) add the three secrets and the two variables the workflow's header
lists; (4) run the workflow by hand once from the Actions tab with `live` unticked and
read the artifact log; (5) set `MALAVI_INTAKE_MODE=live`. Then the BIOMIX runbook
command is `git pull` before anything, and the cycle refuses to run otherwise.

**Known gaps.** R is not installed on the runner, so `validate_record.R` is reported as
skipped in every screen the runner makes (it is skipped on any machine without R, and
says so in the report). The watcher workflow has been failing on the same runner since
2026-08-17 and passes locally; that is the runner environment and should be fixed first,
as the free test. The log artifact names submitters, like the log on BIOMIX; it is visible
to the repository's collaborators only and expires after 30 days.

```bash
.venv/bin/python -m pytest curation/tests/test_intake_cycle.py -q     # the state steps are tested here
```

---

## 4c. The curators' board ✅ (built 2026-10-02; file written, NOT yet on the live site)

Where every submission stands, for the curators: its state, which curator recorded which
verdict, what it is waiting on and for how long, and links to its report and to the
verdict form. Page: `docs/board.html` (on the site: `/board.html`, linked from nowhere).
Data: `docs/assets/data/board.json`, written by `curation/build_status_board.py` from the
review ledger.

**It is on the public site, encrypted.** The site is static and its repository is public,
so nothing there can be access-controlled; an unlisted page is a public page. The data
file is therefore AES-256-GCM ciphertext, with the key derived from one shared password
(PBKDF2-HMAC-SHA256, 600,000 rounds), and the page asks for the password and decrypts in
the browser. Vincenzo chose this over a Google Sheet on 2026-10-02 because it needs no
second system. What that costs, and was accepted:

- one password for all curators: access cannot be withdrawn from one person without
  changing it for everybody;
- every published version stays in the public repository's history, readable by anyone
  who ever learns the password it was written under. Changing the password protects later
  versions only.

**So the board carries only what would be tolerable if the password leaked:** the opaque
id, dates, the state, curator names beside the verdict *word*, what it is waiting on, the
count of reserved names, and two links that keep their own Google protection (the report
in Drive, the verdict form). Never a submitter's name, a reason, a correction's text or a
lineage name. `malavi_curation/status_board.py` builds exactly that and
`curation/tests/test_status_board.py` holds it there.

```bash
.venv/bin/python curation/build_status_board.py --init-password   # once: makes the password
.venv/bin/python curation/build_status_board.py --dry-run         # what the board would say
.venv/bin/python curation/build_status_board.py                   # write the encrypted file
```

**The password** is generated (16 characters, 96 bits), not chosen: the ciphertext is public
for good, so only the password's randomness stands between it and a reader. It lives at
`~/.config/malavi/board_password.txt`, mode 600, outside the repository (the program
refuses a path inside it), and the lifeboat backs that directory up. `--init-password`
prints the path and never the password. **Read it there and give it to each curator
yourself**, in person or in a message that is not the one carrying the link.

**It stays current by itself.** `public_feeds.refresh()` runs the builder with the two
public feeds whenever `enroll`, `fetch_verdicts`, `promote` or `close_submission` changes
a state, and `publish/push_feeds.sh` publishes `board.json` when it exists; the intake
cycle (§4b) builds it at step 3. The file is rewritten only when the board changed
(encryption is randomized, so the builder decrypts what is there and compares), which is
what keeps an unchanged board from becoming a public commit on every run. A board that
fails to build never holds back the public feeds.

**It lags a verdict until the next run.** A verdict recorded on the form appears on the
board after the next `fetch_verdicts.py`, not at once; the verdict notifier (§3d) is the
immediate notice. The page says so.

**First publication** needs `publish/push_site.sh`, because the page itself is new;
afterwards `push_feeds.sh` keeps the data current.

**To change the password** (somebody leaves, or it leaked): delete the password file, run
`--init-password`, run the builder (the board is rewritten under the new password), publish,
and give every curator the new one. A browser that remembered the old one is asked again.

**With no password file** the builder writes nothing and exits 0, on purpose: it runs
inside the automatic feed refresh, and an optional feature that is not set up must not
stop the public queue.

```bash
.venv/bin/python -m pytest curation/tests/test_status_board.py -q
node docs/assets/js/tests/test_board.mjs
```

---

## 5. Tests ✅ (all three, verified 2026-08-08)

Run all three suites.

```bash
.venv/bin/python -m pytest curation -q                  # ✅ Python: the curation package
node docs/assets/js/tests/test_sequence_check.mjs       # ✅ JS: the browser checker
node docs/assets/js/tests/test_sequence_match.mjs       # ✅ JS: the sequence match page (~2 min)
node docs/assets/js/tests/test_transmission.mjs         # ✅ JS: the map's transmission classes vs the exporter's counts (added 2026-09-25)
node docs/assets/js/tests/test_board.mjs                # ✅ JS: the curators' board: decryption and wording (added 2026-10-02)
node docs/assets/js/tests/test_projection.mjs           # ✅ JS: the map's Equal Earth projection: equal area, inverse, antimeridian clipping (added 2026-10-02)
node docs/assets/js/tests/test_lineage_history.mjs      # ✅ JS: "formerly X" from the Lineage History table (added 2026-10-02)
cd /mnt/ellisbiostore/malaviR && Rscript -e 'devtools::test()'   # ✅ R: malaviR
```

The Python suite takes about 80 seconds and passed **1053 tests on 2026-08-14**. (This file
carried two different counts for it — 686 here and 1053 in §0, five days apart. It is one
figure, it lives here, and §0 now points at this line instead of repeating it.)

**The Python suite publishes nothing, since 2026-10-14.** Until then it did: three test
files ran their program's `main()` against a temporary inbox, the program called
`public_feeds.refresh()`, and that rebuilt the real feeds in `docs/` and ran
`push_feeds.sh`, so every run of the suite committed and pushed to the public site
repository (398 feed publishes between 2026-09-23 and 2026-10-14, most from tests; the
content was always the real feeds, so the site never showed anything false). Two guards
now stand: `curation/tests/conftest.py` replaces `refresh` with a stub for every test,
and `refresh` itself refuses to run while `PYTEST_CURRENT_TEST` is set. After running the
suite, `git status -- docs/` must be clean and `ls logs/publish_feeds_*.log | wc -l` must
not have grown; if either changed, a test is reaching the real tree again.

The JS suite runs **every lineage in the release** through the browser checker, so allow
minutes rather than seconds; it reported **135 passed / 0 failed on 2026-08-14**, about
28 ms per query. It is a required pre-publish step: the error it exists to prevent is
telling someone a sequence is new when MalAvi already names it.

The R suite reported 321 passed / 0 failed on 2026-08-08, in about two minutes, with **3
skipped**: the BLAST tests need DECIPHER >= 3.0 and Biostrings, which are not installed
here. Skipped is not passed — if the BLAST path ever matters, install those two and re-run
rather than assuming the suite covered it.

---

## 6. Rebuild the site's data files ✅ (verified 2026-08-08)

Run after the pinned release changes. **All seven**, and `build_downloads.R` is not optional:

```bash
Rscript export/build_bird_names.R      # ✅ the eBird/Clements checklist the name checker uses
Rscript export/build_site_stats.R      # ✅ every figure on the site
Rscript export/build_sequence_index.R  # ✅ the sequence checker's index
Rscript export/build_range_lookup.R    # biostore only, ~8 min on a compute node: the map's host-range flags (added 2026-09-25; before build_site_points.R)
Rscript export/build_site_points.R     # ✅ the map page's sampling sites (added 2026-09-15)
Rscript export/build_tables_json.R     # ✅ the browsable tables
Rscript export/build_reports.R         # ✅ the QC report CSVs
Rscript export/build_downloads.R       # the per-table CSV/XLSX, the FASTA alignment, the ZIP
```

> **`build_downloads.R` was missing from this list until 2026-08-14, and its absence is
> silent and total.** Every download URL on the site is built in the browser as
> `assets/downloads/tables/<id>_<RELEASE>.<ext>`, with `RELEASE` read from
> `site_stats.json` (`docs/assets/js/malavi.js`). So bumping the release and running only
> the other five renames every expected file without creating it: **every download link on
> the site 404s**, including the "Everything" archive, while the pages themselves look
> perfectly healthy. Nothing warns, because nothing on the server checks that a link
> resolves.
>
> After running it, confirm the files exist for the release you just built:
> `ls docs/assets/downloads/tables/ | tail`.

> **`build_land_layer.py` (2026-10-02)** is not one of the seven either, and does not
> depend on the release: `python3 export/build_land_layer.py` writes
> `docs/assets/data/world_land.json`, the interactive map's land (country outlines from the
> Natural Earth 1:10m layer in `reference/natural_earth/`, small islands included). Run it
> only if that source layer is replaced. The front-page maps keep `world_map.json`.
>
> **`build_range_lookup.R` (2026-09-25)** is not one of the seven. It reads the BirdLife
> range polygons at `/mnt/ellisbiostore/bird_ranges/BOTW_2024_2.gpkg` (licensed; never
> copied) and writes `reference/host_range_lookup.csv`, which `build_site_points.R` joins
> onto the map's records. If the lookup is missing or built for another release the site
> still builds; hatch-year birds of migratory species then read as undetermined on the map's
> transmission view and `build_site_points.R` says so on the console. It lists any host the
> crosswalk cannot name: add those to the sympatry project's crosswalk inputs, never guess.

Two other lists of this same step exist and disagreed with each other until 2026-08-14 (all three gained `build_site_points.R` on 2026-09-15):
`export/README.md` (which had `build_downloads.R` but not `build_bird_names.R`) and
`publish/README.md` (a deliberately shorter one, for republishing without a new release).
If you change the set, change all three.

**These were run on 2026-08-08 and every output was byte-identical to what the site was
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
node docs/assets/js/tests/test_transmission.mjs        # must pass
node docs/assets/js/tests/test_sequence_match.mjs      # must pass (~2 min)
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
the record store seeded in step 3b and added to in step 3e. Always dry-run first and read
the diff:

```bash
# what would change, writing nothing
.venv/bin/python curation/build_release.py --dry-run \
    --diff-against docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv

# build it
.venv/bin/python curation/build_release.py --release 2026-08-07 \
    --diff-against docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv
```

Writes `data/releases/MalAvi_<release>.zip` (six `.xlsx` tables since 2026-09-25, the
sixth being `LineageHistory_<release>.xlsx`, + the `.fas` alignment)
and `release_report_<release>.json`. Both are gitignored; the archived ZIP for a release
that ships belongs in `malaviR/data-raw/`.

### 8a. Correct records MalAvi already holds ✅ (built 2026-08-11)

For faults in data MalAvi has **already published** — a longitude with the wrong sign, a
misspelled vector method.

**It is also how a curator's correction to a submission finally reaches the data.** This
line used to say otherwise ("not for a curator fixing a submitter's rows before ingest;
that is `apply_corrections.py`"), which was wrong in a way that lost corrections silently:
`apply_corrections.py` records the authority for a change and edits nothing. Either the
submitter sends a corrected workbook, or the fix is made here after the submission is
ingested. See §3da.

**A published edition is never edited.** This changes the store, which is the *next*
edition, and the change then appears in that edition's report under **Records corrected**
— which is what tells somebody working from the previous edition that a value they
downloaded has changed.

```bash
# the 54 host records at one site whose longitude sign puts them in Somalia
.venv/bin/python curation/correct_store.py \
    --table host_records --site "Mata Seca State Park" \
    --set "SITE_COORDINATES=-14°50.91100', -043°59.29800'" \
    --reason "longitude sign; the site is in Minas Gerais, Brazil"

# the misspelled vector method, 90 rows
.venv/bin/python curation/correct_store.py \
    --table vector_records --where VECTOR_METHOD=Unkown \
    --set VECTOR_METHOD=Unknown --reason "spelling"
```

Nothing is written without `--apply`. The default run lists every row that would change
with the value it holds now, so the selection can be checked first.

| Selector | Selects |
|---|---|
| `--record HST-000123` | exactly one row, by RECORD_ID |
| `--site "<name>"` | every row at that site — **one decision, however many rows** |
| `--where COLUMN=VALUE` | every row whose column holds exactly that value |

**Selectors match exactly** — no case folding, no substrings. That is not fussiness:
`Mata Seca State Park` (54 rows) and `Manga, Mata Seca State Park` (53 rows) are different
sites with different coordinates, and a substring match would have corrupted the second
while fixing the first.

Every applied correction appends one line to **`data/corrections.csv`** (git-tracked, one
line per decision regardless of row count) recording the selector, both values, the row
count and the reason. Read the diff of that and of `data/records/`, then commit them
together. Re-running an applied correction reports nothing to do and exits 2.

---

### 8c. Record a lineage-history event ✅ (built 2026-09-25, seeded, not yet shipped)

The curators' rules of 2026-09-24: a complete sequence that matches a partial lineage
keeps the OLD name and updates its sequence; a synonym that adds nothing is merged into
the more complete lineage or removed; and nothing changes without a public row in the
**Lineage History** table (`data/records/lineage_history.csv`, shipped as the sixth
release table, `malaviR::extract_table("Lineage History")`). One command per decision
writes the row AND makes the change in every table that cites the name:

```bash
# dry run first: prints the history row and every store row the event touches
.venv/bin/python curation/record_lineage_event.py sequence_updated H_CCF2 \
    --sequence-file ccf2_full.fasta --accession PQ123456 \
    --source MALAVI-SUB-2026-000011 --reason "complete 479 bp barcode, identical over the 470 shared"
.venv/bin/python curation/record_lineage_event.py merged_into L_ACCBRE01 --into L_ACCBRE02 \
    --source "curators 2026-09-24" --reason "one host record; identical to ACCBRE02 over the overlap"
.venv/bin/python curation/record_lineage_event.py removed TURGUL01 \
    --source "curators 2026-09-24" --reason "..."
.venv/bin/python curation/record_lineage_event.py renamed OLD01 --into NEW01 --source ... --reason ...
# then the same command with --apply
```

`renamed` refuses a name that exists (use `merged_into`); `merged_into` refuses a target
that does not (use `renamed`); `removed` drops the lineage and every host, vector, alt-name
and morphospecies row citing it. Both rename and merge write the old name to `alt_names`
so papers that used it still resolve. The REASON and SOURCE are PUBLIC: neutral wording,
no colleagues' names, no working shorthand (see the seed notes below for the standard).
Apply only what the curators voted on; the candidate lists are
`malaviR::synonymy_report()` and `ambiguous_pairs()`.

The table was **seeded on 2026-09-25** (`curation/seed_lineage_history.py`, runs once)
with 74 rows from the master file: 70 "sequence updated from <accession>" notes, two
"same as" rows marked deleted (merged), one retired row, and STRALU01 -> STAL3 from the
2026-09-15 import. Every accession named was checked against GenBank
(`python3 curation/audit_lineage_history_genbank.py`, system python3): 59 of 60 exact;
MILVUS01's note was on the wrong row and is excluded. Staffan's verbatim notes and the
public wording chosen for each are in the PRIVATE
`results/LINEAGE_HISTORY_seed_notes_2026-09-25.md`. Re-run the GenBank audit before any
release that adds accession-sourced rows.

`malaviR::lineage_history_map(names)` follows old names forward for users of older
analyses; `malavi_issues()` reports history rows whose names do not resolve.

**Rule 1 runs by itself at ingest** (`store_ingest.resolve_known_lineages`, 2026-09-25).
A declared new lineage whose sequence is identical to a lineage MalAvi holds over every
position both cover (>= 300 of them) is filed under the existing name; if it determines
more bases than the stored sequence, the stored sequence becomes the submitted one and a
`sequence_updated` history row is written with the submission as its source. The screen
says so before approval: `sequence_completes_known_lineage` (warning) for the completing
case, `sequence_is_known_lineage` (blocking) when it adds nothing. Nothing to do by hand;
the ingest notes and the edition report show what was completed.

### 8d. Seed the Genomic Resources table ✅ (built and applied 2026-10-14)

The `genomic_resources` release table (format since 2026-09-25, `schemas/data_dictionary.md`)
was empty until 2026-10-14. `curation/seed_genomic_resources.py` filled it from the
malaviTree mitochondrial backbone: every avian haemosporidian mitochondrial genome in
`/mnt/ellisbiostore/malaviTree/data/raw/backbone_mtdna_2026-06-15/` (233 records,
fetched from GenBank on 2026-06-15) whose cytochrome b barcode, found anywhere in the
record on either strand by the submission screen's own code, is identical to one MalAvi
lineage over at least 470 of the 479 positions.

```bash
.venv/bin/python curation/seed_genomic_resources.py            # dry run: the side table only
.venv/bin/python curation/seed_genomic_resources.py --apply    # once; refuses over a filled table
```

**What it wrote (2026-10-14):** 217 rows covering 141 lineages, `RESOURCE_TYPE` =
`mitogenome` (5,500 bp or longer) or `mitogenome_partial`, the GenBank definition line as
the description, the host and study where the curated lists state them (Pacheco et al. 2018
table S1 for the host; the post-2018 list for the paper), `SOURCE` naming the backbone
fetch, `_source = seed`, and `_added` left blank for the release build to stamp.

**What it did not write, on purpose:** 16 records, listed with their calls in
`results/GENOMIC_RESOURCES_seed_2026-10-14.tsv` (every candidate is in that file,
column `seeded`). They are the *Babesia* outgroup; *Leucocytozoon caulleryi*, 35 bases
from the nearest MalAvi lineage; nine *Haemoproteus* records 2 to 9 bases from theirs
(candidate new lineages, a curator's call); and three records identical to a lineage over
467 positions, just under the floor, because the MalAvi lineage is itself partial. A
curator who wants any of those in the table files them through the template's
GenomicResources sheet, which is the route for everything after the seed.

**Keeping it current:** new rows come through submissions (template 2026-10, the
GenomicResources sheet, ingested by §3e) and, later, from re-running malaviTree's
`scripts/05_ncbi_scan.sh` and a sync program that does not exist yet; the seed itself
is not re-runnable over a filled table. The table reaches malaviR and the site with the
next release build (§8): malaviR 1.3.4 reads it (`extract_table("Genomic Resources")`,
empty with its eleven columns on an older bundle) and `export/build_site_stats.R` lists
it on the tables page when the release carries it.

### 8e. The synonymy cleanup pass ✅ (built 2026-09-27; first sheet written, nothing applied)

Meeting point 5 of 2026-09-24: simplify the synonymies and ambiguities, pair by pair, by
vote, with a public history row for every change. Two commands, and a sheet between them.

**1. Write the sheet** (reads the store, changes nothing; ~15 s):

```bash
.venv/bin/python curation/review_synonymies.py --date 2026-09-27
```

It writes `results/SYNONYMY_REVIEW_<date>.csv`, `.xlsx` and `.md`. One row per pair of
stored lineages whose sequences never disagree at a position both determine (gaps, N and
every other ambiguity code count as undetermined, as in malaviR's `synonymy_report()` and
`ambiguous_pairs()`). KIND is `duplicate` (same positions, same bases), `contained` (LINEAGE
determines a subset of OTHER's positions; `CONTAINERS` lists every complete lineage the
partial is compatible with) or `ambiguous` (each determines positions the other lacks).
Per side: genus, accession, determined bases, host and vector records, distinct hosts,
references and their earliest year, countries, morphospecies links, alternative names.

PROPOSAL is the rule, mechanically, never a decision: a duplicate merges into the group's
best-established name (most records, then earliest first year, then the earlier name); a
contained lineage with exactly one container and at most `SMALL_LINEAGE_MAX_HOST_RECORDS`
(1) record merges into it; everything else says `decide:` with the reason (several
containers, many records would move, the two genus labels differ) or `review by hand`
(ambiguous). Below `MIN_COMPARABLE` (300, the ingest's known-lineage floor) nothing is
proposed. The 2026-09-27 sheet: 503 pairs (9 duplicate, 330 contained over 202 partial
lineages, 164 ambiguous), 93 merges proposed, 233 below the floor, 16 cross-genus.

**2. The curators fill in the yellow columns** of the `.xlsx` (share it through Drive):
`DECISION` = merge / absorb / remove / keep / rename, `INTO` (pre-filled with the proposed
target), `CURATOR_NOTE` (required for remove and rename: it becomes the PUBLIC reason).
`absorb` is merge the other way round: OTHER folds into LINEAGE, which keeps its name, and
when OTHER's sequence determines more bases LINEAGE's stored sequence is completed from it
first (Rule 1 applied inside the database: GRW02, 60 records, contained in FICZAN23 with
one, keeps its name and takes the complete sequence). The sheet's second tab says all this.

**3. Apply what came back**, dry run first:

```bash
.venv/bin/python curation/apply_lineage_decisions.py results/SYNONYMY_REVIEW_2026-09-27.xlsx \
    --date 2026-10-05 --source "curator decision 2026-10-05"
# read every event and its public reason, then the same command with --apply
```

The dry run prints each event, the public REASON it will carry and every store row it
touches, and refuses the whole sheet if one row breaks a rule: a lineage decided two
different ways, a merge into a name the sheet removes, a remove without a note, a target
that is not a stored lineage. Merge chains (A into B, B into C) resolve to the survivor.
`--apply` applies every event through `lineage_history.apply` and writes the store once.
Keep the filled-in sheet in `results/` as the record of the vote; a later
`review_synonymies.py --decisions <that sheet>` shows those decisions in `PRIOR_DECISION`
instead of asking again. `keep` decisions live only on the sheet.

Not for this pass: TUPHI01 is stored twice under one name (a store fault, listed in the
`.md`, not a pair); the 16 cross-genus pairs want the genus label settled first (memory
note `malavi-genus-label-outliers`).

### 8f. Sync the Genomic Resources table from NCBI ✅ (built 2026-10-16; dry run only, nothing applied)

TODO 12b: fill the table in from the databases ourselves rather than waiting on
submissions. `curation/sync_genomic_resources.py` queries NCBI and ENA for every avian
haemosporidian nuclear genome, apicoplast genome, transcriptome and mitochondrial genome,
calls the cytochrome b barcode inside each one against the store's lineages with the
submission screen's own code (the seed's rule, §8d: identical over at least 470 of the
479 positions, no mismatch), and reads each record's REFERENCE blocks and `/host`
qualifier for the citation, the DOI and the host. Dry run by default; `--apply` is the
only thing that writes the store.

**What it reads.** The record store (`lineages.csv` as the barcode reference,
`genomic_resources.csv` as the rows to fill) and, over the network:

- GenBank nucleotide, with malaviTree's monitor queries copied verbatim
  (`scripts/05_ncbi_scan.sh`: `<genus>[Organism] AND 5000:7000[SLEN] AND
  (mitochondrion[Title] OR mitochondrial[Title] OR (cytochrome b[Title] AND cytochrome
  c oxidase[Title]))` for Haemoproteus, Leucocytozoon, Parahaemoproteus and Fallisia,
  and Plasmodium restricted to the avian species list) for mitogenomes, and the same
  taxa with `apicoplast[Title] AND 20000:60000[SLEN]` for apicoplast genomes;
- NCBI Assembly for `Haemosporida[Organism]`, reduced to avian taxa by name (an avian
  genus, or Plasmodium with an avian epithet; everything dropped is counted on the
  console), cross-checked against ENA's assembly listing for the order; each genome is
  downloaded through NCBI Datasets and scanned for the barcode (a k-mer prefilter says
  where to look, the registration decides);
- ENA `read_run` for transcriptomic runs of the order (NCBI SRA for the named taxa as a
  second listing), one candidate per BioProject, with the BioSamples' `host` and
  `lineage` attributes and the BioProject's publication; TSA assemblies through their
  master record, with the contig set downloaded from ENA and scanned;
- PubMed esummary for every PUBMED id a record or BioProject cites: DOI, year, authors.

Every response is cached under `results/genomic_sync_cache/` (ignored by git), so a
second run makes no request. The contact email comes from `config/project.yml`
(`project.curator_email`); an NCBI API key is read from the environment variable
`NCBI_API_KEY` if set (10 requests per second instead of 3). The client retries a 429
with a growing pause; the first run of 2026-10-16 met one inside NCBI's stated rate.

```bash
.venv/bin/python curation/sync_genomic_resources.py --date 2026-10-16        # dry run, ~10 min cold, seconds from the cache
.venv/bin/python curation/sync_genomic_resources.py --sources mitogenome     # one source family only
.venv/bin/python curation/sync_genomic_resources.py --date 2026-10-16 --apply   # after reading both side tables
```

**What it writes.** `results/GENOMIC_RESOURCES_sync_<date>.tsv`: every candidate record
with its barcode call (nearest lineage, distance, comparable positions, and the caveat
when an identical barcode is still not a clean call), what the record itself states
about its lineage, the reference, the DOI, the host, and `proposed_action`: `add`,
`curator call`, `paper needed` or `already in table`.
`results/GENOMIC_RESOURCES_fill_<date>.tsv`: for each existing row and each of
REFERENCE_NAME, DOI, HOST_SPECIES, the old value, the value the record gives, where it
came from, and `fill` (cell empty), `same`, `conflict (kept)` or `none`. With `--apply`:
the `add` rows (`_source = sync`, `_added` blank for the release build to stamp, ids
above the high-water mark) and the `fill` cells, nothing else.

**What it refuses to write, and why.** A barcode one base from a lineage (a new-lineage
candidate or a sequence correction: `curator call`). A barcode identical to two stored
lineages, or identical to a partial lineage while a complete one is a single base away
(the P. relictum SGS1 reference genome, GCA_900005765.1: identical to DONANA05 over 478,
one base from SGS1 over 479; by rank order it would have been filed under DONANA05). A
record whose own `/strain` or BioSample `lineage` names a different lineage than its
barcode. A record with no barcode at all: an apicoplast genome, an RNA-seq project, a
nuclear assembly deposited without its mitochondrion (`paper needed`, with the record's
own statement shown as `record states: NAME` when it makes one). A citation key is never
built from a resemblance: a "Direct Submission" and an "Unpublished" JOURNAL give no
reference, and the DOI of a record is written next to an existing REFERENCE_NAME only
when the record's own citation key is that same name. A filled cell is never overwritten;
a disagreement is listed as `conflict (kept)`.

**The dry run of 2026-10-16.** 244 candidates: 214 mitogenomes already in the table
(no mitogenome deposited since the June scan), 15 mitogenomes for the curators (the
seed's 16 minus the Babesia outgroup, now with their references and DOIs where GenBank
has them; PV839546 and PV839582 state their lineage in `/strain`, ATNO1 and BUBT3, and
match it exactly over 432 and 449 positions), 4 apicoplast genomes (H. columbae
MW250349, L. caulleryi AP013071 and its RefSeq copy, P. gallinaceum AB649424; none has
a barcode), 4 nuclear assemblies (H. tartakovskyi GCA_001625125.1 = SISKIN1 and P.
gallinaceum GCA_900005855.1 = GALLUS01 proposed `add`; P. relictum GCA_900005765.1 a
curator call as above; H. majoris GCA_965637345.1 has no mitochondrion in the assembly,
its BioProject title says "WW2 lineage": paper needed), 6 RNA-seq BioProjects (paper
needed; PRJEB42477 carries the BioSample lineages SGS1 and SGS2, a mixed project) and
the H. columbae transcriptome assembly GGWD00000000, whose contigs carry a cyt b
identical to COQUI05 over 479: proposed `add`. Fills: 66 reference names, 105 DOIs,
155 hosts; after them 177 of 217 rows have a reference, 142 a DOI, 213 a host. Still
blank: 51 rows whose GenBank record cites no publication, 17 Harl et al 2026 and 5
Pacheco et al 2025 rows whose records say "Unpublished" (no PUBMED id to resolve), and
the two rows spelled "Viera et al" whose records read "Vieira" (reported as conflicts,
DOI withheld until the name is settled). Seven host conflicts are the curated scientific
name against GenBank's common name or note; all kept.

Three seeded rows sit on ties the seed resolved by rank order (KY653775 and KY653776:
FICZAN23 over 479 and GRW02 over 478; OR327002: NEWBR01 and NEWBR04, both 479): not
changed here, for the synonymy pass (§8e).

### 8b. The edition report ✅ (built and rehearsed 2026-08-11)

Every edition ships with a report of how it differs from the last one, so that a release
can be checked afterwards and so there is a printed record of what changed. The build
writes it automatically whenever `--diff-against` is given — four files, in
`data/releases/`:

| File | Who it is for |
|---|---|
| `release_notes_<release>.html` / `.pdf` | **Internal.** Everything: the approval block, the submissions this release published, every data fault the build found, the detail of removed records, and the lines to sign. |
| `release_notes_<release>_public.html` / `.pdf` | **Public.** What changed in the database. No faults, no submission ids, no approval mechanics. |

The split is enforced in `release_notes.INTERNAL_ONLY_SECTIONS` — `render()` filters the
section list on it and a test iterates it — not left to whoever is copying files on
release day: a data fault names a study, and through it the people who contributed the
records. Adding a name to that tuple is the whole of what it takes to keep a new section
out of the public document.

`--no-notes` suppresses both. To write the report **without** building a release — to see
what the next edition would say, or to re-render an edition's report after a wording fix:

```bash
.venv/bin/python curation/edition_report.py --release 2026-08-14 \
    --previous docs/assets/downloads/tables/grand_lineage_summary_2026-03-23.csv
```

That path also writes `release_diff_<release>.json`, the machine-readable comparison. Its
"Faults to look at" section is empty by construction — the faults are found by the build,
so build the release to get them.

**Read these before shipping, in this order:** the totals, then *Lineages added* and
*Studies added* (what the edition is), then *Changes to existing lineages* (what it
overwrites), then *Corrections to the Grand Lineage Summary* (what the rebuild fixed). A
lineage name listed as "could not be compared row by row" means two rows carry that name
— MalAvi's own `TUPHI01` is always there, and anything else on that list wants a decision
before the release ships.

**Publishing the public one is a separate, deliberate act.** Copy it into `docs/` when the
edition goes out; nothing does that automatically, and `data/releases/` is gitignored so
the internal document cannot reach a tracked directory by accident.

```bash
.venv/bin/python -m pytest curation/tests/test_release_diff.py \
    curation/tests/test_release_notes.py -q
```

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

## 9. The three programs outside the curation loop ✅ (written 2026-08-13)

They do not touch submissions or releases, which is why they had no entry here. Each is
occasional and each is destructive or outward-facing in some way, so read the `@why` header
at the top of the file before the first run.

### 9a. Rebuild the orientation guide — `ops/build_docs.py`

```bash
.venv/bin/python ops/build_docs.py
```

Builds **two** documents, from two sources, and each becomes a public page:

| Source | Private copy | **Public page** |
|---|---|---|
| `ops/malavi-user-guide.src.html` | `ops/malavi-user-guide.html` | `docs/how-it-works.html` |
| `ops/curator-instructions.src.html` | `ops/curator-instructions.html` | `docs/curating.html` |

**Edit the `.src.html`, never the generated file** — the font is 17,000 characters of base64
on one line, and the generated copy is overwritten without asking. Run it after any edit to
a source, and commit all of them.

The curator guide is easy to miss here, and it is the one that matters most: it is the
document curators are sent to, it is live at `malavi-db.github.io/curating.html`, and
because it is generated, a build that fails part-way leaves the published page silently
stale — showing instructions that no longer match what the form does.

### 9b. Back up what cannot be recreated — `backup/lifeboat.sh`

```bash
bash backup/lifeboat.sh
```

Copies the full git history of the MalAvi repositories, the credentials, and the working
state that exists nowhere else into `$HOME`. The project lives on `/mnt/ellisbiostore`, which
is a temporary resource; `/home/vaellis` is permanent but small, so this copies only the
irreplaceable parts and records how to recreate the rest.

Run it before anything risky and after any session that changed credentials or the ledger.
Note the hardcoded `/mnt/ellisbiostore/malavi_rebuild` — the two mount points are the same
inode tree here (`61:5041985339`), so it works, but it would not on a machine with only one
of them.

### 9d. The privacy scan and the public audit ✅ (built 2026-10-16)

Four public destinations (the site, the feeds, the pipeline repository, malaviR), one
scanner: `curation/privacy_scan.py`. It builds its forbidden list at run time from the
private files (every Google id in `config/project.yml` and `CUSTODY_PRIVATE.md`; every
submitter's name and intake directory name; the ledger's reason texts and messages to
submitters; Drive file ids; the sequences of live submissions that are not in the
release; the board password; credential shapes; and, for the pipeline repository only,
the curators' names other than the project's authors) and refuses a tree that carries
any of it. It prints the class, the file, the line and a four-character hash, never the
value (`--reveal` on your own machine shows it). Exceptions: `privacy.public_addresses`
and `privacy.public_ids` in the config, and `publish/privacy_allow.txt` (destination,
class, path glob, reason; a line without a reason is refused).

It runs inside `push_site.sh` (on the exact staged tree), `push_feeds.sh` (the four
files; a hit stops the publish loudly), `push_pipeline.sh` (the staged tree, after its own
scan; the config example is now generated WITHOUT comments), and, for malaviR, as a
`pre-push` hook: `publish/install_hooks.sh` once per clone. CI runs
`test_privacy_scan.py`, which scans the real `docs/` tree and feeds.

`ops/public_audit.py` fetches what is actually live (the site's pages and feeds, the
three public repositories with their whole history) and scans it; `.github/workflows/
public_audit.yml` runs it every Monday. Problems at HEAD or on the site mean "act
today"; problems only in a repository's history are a decision to take once (rewrite the
history, or accept with a reasoned allow line for `<repo>.history.txt`). On 2026-10-16 the
first audit found: nothing private on the live site or feeds; at HEAD of the pipeline
repository one comment naming a volunteer (fixed, republish owed) and the R package's
author addresses (allowed); in the three histories, old intake directory names, Google
ids and placeholder addresses from before the 2026-08 scanners (Vincenzo's decision).

```bash
.venv/bin/python curation/privacy_scan.py --classes                      # how many values per class
.venv/bin/python curation/privacy_scan.py --destination site docs        # the site tree
.venv/bin/python curation/privacy_scan.py --destination pipeline --reveal <staging>   # local only
.venv/bin/python ops/public_audit.py                                     # everything live, with histories
.venv/bin/python ops/public_audit.py --no-history
bash publish/install_hooks.sh /mnt/biostore-all/Vellis/malaviR           # once per clone
```

### 9c. Publish the pipeline code — `publish/push_pipeline.sh`

```bash
bash publish/push_pipeline.sh
```

Copies the files named in `publish/public_manifest.txt` into a clone of
`malavi-db/malavi-pipeline`, scans the result, and refuses if anything sensitive appears.
**This is outward-facing and irreversible** — a push is public immediately and git history
keeps what you pushed even if you delete it afterwards. The allowlist is the manifest, not an
exclude list, so a new file is private until somebody adds it deliberately. Re-read the diff
it prints before confirming.

---

## Not yet written up

- The publication watcher (`watcher/`), which runs weekly in dry-run and has never
  announced a paper. **Testing it is deliberately deferred (decided 2026-08-08):** it is
  secondary to getting the rest of the loop running with real curators. The watcher's job is
  to find *new* work, and there is no point generating a queue while ~15 papers from the
  existing forward set are still unprocessed. Turn it on when curators are keeping up.
- The PDF/table extraction path (`python -m malavi_curation.intake`).
