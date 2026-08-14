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
| 11 | `notify_submitters.py` — **only after the 24-hour hold has elapsed, and nothing schedules it** | §1c |
| 12 | `close_submission.py --apply` if it ended; `lift_embargo.py --apply` if it was held | §3db, §3f |
| 12b | if it was already ingested when it ended: `ingest_submissions.py --retract <id> --apply`, or no release can be built at all | §3e |

### B. Building and publishing a release

| # | Do this | § |
|---|---|---|
| 13 | `ingest_submissions.py --release <date> --apply` | §3e |
| 14 | fill the blank order/family/continent/`SEQ_LENGTH` columns in `data/records/`, and commit | §3e |
| 15 | `build_release.py --dry-run --diff-against …`, **read the diff**, then build for real | §8 |
| 16 | read the edition report; copy the *public* one into `docs/` by hand | §8b |
| 17 | archive the ZIP into `malaviR/data-raw/` | §8 |
| 18 | in malaviR: `Rscript data-raw/process_release.R`, then rebuild and reinstall the package | *no section — see the header of that script* |
| 19 | bump `malaviR.release` in `config/project.yml` to the new date | *no section* |
| 20 | `Rscript curation/r/gate_reference.R > curation/src/malavi_curation/data/db_snapshot.json` | §2 |
| 21 | `Rscript curation/r/gazetteer.R > curation/src/malavi_curation/data/gazetteer.json` | *only in `curation/README.md`* |
| 22 | **all six** export scripts | §6 |
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
- **Nothing logs what you ran.** Only the two bash scripts write to `logs/`. If you stop
  half way, the record of where you stopped is `data/decisions.json`, `data/corrections.csv`
  and your own shell history.
- **Four things must agree at the end**: the release the site advertises
  (`site_stats.json`), the download files that exist, `malaviR.release` in
  `config/project.yml`, and the release `db_snapshot.json` was built from. Each is a
  separate manual step above (22, 22, 19, 20) and nothing checks that they match.
- **Budget two sittings.** The 24-hour hold between approval and notification means a
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

> ⚠️ **Pasting that file in wipes the live configuration. Put three values back before you
> deploy.** The repository copy carries placeholders, because it is published:
>
> | Line | In the repo | What belongs there |
> |---|---|---|
> | `REPORTS_FOLDER_ID` | `PASTE_THE_REPORTS_FOLDER_ID_HERE` | the reports Drive folder id |
> | `SHARED_SECRET` | `PASTE_THE_SHARED_SECRET_HERE` | the contents of `~/.config/malavi/report_secret.txt` |
> | `CURATORS` | `['vaellis@udel.edu']` | every curator who should receive reports |
>
> The live values are in `CUSTODY_PRIVATE.md`. **Then run `testSetup` in the Apps Script
> editor before deploying** — it checks all three and is the only thing that catches this.
> Deploying with the placeholders in place does not error: reports simply stop arriving.
>
> The same applies to `notify_on_submission.gs`, whose `FORM_ID` is
> `PASTE_THE_SUBMISSION_FORM_ID_HERE`, and to `inspect_verdict_form.gs`, whose
> `VERDICT_FORM_ID` is `PASTE_THE_VERDICT_FORM_ID_HERE` (the id is in `config/project.yml`
> as `review.verdict_form_edit`).

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

To check the rules themselves without touching real data:

```bash
.venv/bin/python -m pytest curation/tests/test_ledger.py curation/tests/test_curators.py \
    curation/tests/test_verdicts.py curation/tests/test_fetch_verdicts.py \
    curation/tests/test_enrollment.py curation/tests/test_promote.py \
    curation/tests/test_apply_corrections.py curation/tests/test_close_submission.py -q
```

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

- All three **release the reserved lineage names** and drop the submission out of the public
  queue, which lists live submissions only.
- **An approved submission cannot be declined.** Somebody has to flag it first, and that
  flag is attributed to them — a decline follows an objection, it does not replace one.
- **A withdrawal is terminal with no way back.** A decline can be reopened, and that is
  logged as a deliberate act.
- `--ask` **sends nothing**. Asking the question is an email a human writes; this records
  that we are waiting, which is what makes the clock run.
- After a decline, `notify_submitters.py` sends the decline notice once the same 24-hour
  wait an approval gets has elapsed — here it gives anyone a window to notice a mistake
  before a person is told their work was refused. Run `notify_submitters.py` after this.

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
```

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

## 5. Tests ✅ (all three, verified 2026-08-08)

Run all three suites.

```bash
.venv/bin/python -m pytest curation -q                  # ✅ Python: the curation package
node docs/assets/js/tests/test_sequence_check.mjs       # ✅ JS: the browser checker
cd /mnt/ellisbiostore/malaviR && Rscript -e 'devtools::test()'   # ✅ R: malaviR
```

The Python suite takes about 80 seconds and passed **1053 tests on 2026-08-14**. (This file
carried two different counts for it — 686 here and 1053 in §0, five days apart. It is one
figure, it lives here, and §0 now points at this line instead of repeating it.)

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

Run after the pinned release changes. **All six**, and `build_downloads.R` is not optional:

```bash
Rscript export/build_bird_names.R      # ✅ the eBird/Clements checklist the name checker uses
Rscript export/build_site_stats.R      # ✅ every figure on the site
Rscript export/build_sequence_index.R  # ✅ the sequence checker's index
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

Two other lists of this same step exist and disagreed with each other until 2026-08-14:
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

Writes `data/releases/MalAvi_<release>.zip` (five `.xlsx` tables + the `.fas` alignment)
and `release_report_<release>.json`. Both are gitignored; the archived ZIP for a release
that ships belongs in `malaviR/data-raw/`.

### 8a. Correct records MalAvi already holds ✅ (built 2026-08-11)

For faults in data MalAvi has **already published** — a longitude with the wrong sign, a
misspelled vector method. Not for a curator fixing a submitter's rows before ingest; that
is `apply_corrections.py`.

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
