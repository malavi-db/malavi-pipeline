# Apps Script: the Google-side pieces, kept as code

Google Forms and the mail they trigger are part of MalAvi's review interface, so they live
here as scripts rather than as a sequence of clicks somebody has to remember. The reasons
are the same ones that apply to the rest of the pipeline:

- a change is a diff, and a mistake is undone by re-running rather than by recalling which
  of forty settings was toggled;
- the decisions the form encodes — that a hold requires written reasoning, that an override
  requires naming who was consulted — are visible in review instead of buried in a UI;
- rebuilding after a bad edit takes a minute, so the form is not something to be afraid of
  touching.

## One project (since 2026-10-16)

Everything that RUNS lives in one Apps Script project, **"MalAvi curation"**, owned by
malaviadmin. It is the old "MalAvi report publisher" project with more files, so the
report endpoint keeps its `/exec` URL. One project means one set of Script Properties, one
authorization, one place to install triggers, and one status sheet every trigger can write.
`build_bundle.py` concatenates the runtime files into `dist/malavi_curation.gs`, so an
update is ONE paste over the project's `Code.gs`; the files are split here for reading, and
Google treats one file and ten files identically (one global scope). The bundle builder
refuses to build when two files declare the same function, because in one project the
last definition silently wins.

No file reads a Script Property at its top level: files load in an order nobody controls,
so every read is a function call at the moment of use (`curators_()`, `attentionTo_()`,
`statusSheetEnabled_()`, ... in `settings.gs`), and each file declares what it needs in a
`SETTINGS_NEEDED_<NAME>` global that `showSettings()` collects after everything has loaded.

The browser steps to switch it on are `results/CHECKLIST_browser_2026-10-16.md`
(private) and RUNBOOK section 3g.

## Files

Runtime (bundled into `dist/malavi_curation.gs`):

| File | What it does |
|---|---|
| `settings.gs` | Reads every live value (addresses, ids, secret, sheet id) from Script Properties through accessor functions; `showSettings()` says what is set. |
| `status_sheet.gs` | The private "MalAvi submissions" sheet: tabs Submissions, Verdicts, Mail, Arrivals, Curators, Log; header-named read/write helpers; **assignment** in rotation (`nextAssignee_`, least-loaded, honoring `away_until` / `max_open` on the Curators tab); `reassign`; `createStatusSheet` (run once). |
| `publish_report.gs` | The signed endpoint. Writes the report to Drive, puts the report and the submitter on the sheet, **assigns a first report** to one curator and emails the assignee ("needs your verdict") and the others ("you are in copy"). Actions: `publish_report`, `confirm_names`, `decline_notice`, `verdict_notice`, `attention_notice`, `describe`, **`status`** (the intake cycle's ledger sync, `curation/push_status.py`). |
| `notify_on_submission.gs` | Tells the leads (`ARRIVAL_TO`, default `ATTENTION_TO`) a submission arrived, acknowledges the submitter, writes an Arrivals row. The curators asked to receive only the report email. |
| `notify_on_verdict.gs` | Writes the Verdicts row and the verdict summary; emails every curator what was entered; on a **flag with a "Message to the submitter"**, emails the submitter that text verbatim, from MalAvi, curator and assignee in copy, and records the send. Never on Accept or Reject, never for an address off the curator list. |
| `create_update_form.gs` | Builds the update form (run once) and holds its trigger handler, which now also notes the update on the sheet (`last_from_submitter`). |
| `inbox_triage.gs` | Every 15 minutes: files each new inbox message under its submission (id in the text, else the sender's address against the Submissions tab), labels it, logs it on the Mail tab, **forwards it to the assignee and the leads** with reply-to set to the sender; unmatched mail is labelled and listed in the digest, never forwarded. Records hand-written mail from the account too. |
| `daily_clock.gs` | Daily at 07:00 UTC: assigns the unassigned backlog; reminds the assignee at 7 days and everyone at 14; **reassigns at 21**; reminds the flagging curator to write after 3 days; reminds a waiting submitter at 14 and 30 days; warns before the 60-day dormancy; one **digest** to the leads with the whole queue, alerts and unfiled mail. |
| `triggers.gs` | `installAllTriggers` (idempotent: removes ours, reinstalls what the settings allow), `showTriggers`, `removeAllTriggers`. |
| `add_submitter_message_question.gs` | Adds the "Message to the submitter" question to the LIVE verdict form, after "Why?", once. |
| `build_bundle.py` | Builds `dist/malavi_curation.gs`; `--check` says whether it is current (a test enforces it). |

One-off builders (pasted the day they are needed, then removed; NOT in the bundle):

| File | What it does |
|---|---|
| `create_verdict_form.gs` | Builds the curator verdict form (verdict / override / correction, and since 2026-10-16 the submitter-message field), wires it to a fresh responses spreadsheet, and prints the ids needed for prefilled links. |
| `create_submission_form.gs` | Rebuilds the public data submission form under the operational account. **Cannot finish on its own** — see below. |
| `inspect_verdict_form.gs` | Prints the verdict form's sections, questions and prefill entry ids, to check the form against what the Python side expects. |

## Running a one-off builder

**Sign in as the MalAvi operational account first** — `malaviadmin@gmail.com`. Whatever
account runs the script *owns* what it creates, and running it as yourself would put
MalAvi's form back on a personal account, which is precisely what the operational account
exists to prevent.

1. Open the **MalAvi curation** project (the bundle is already there) and add the builder
   as a new file (Files → "+" → Script); or use a throwaway project with `settings.gs`
   pasted beside it.
2. Set the Script Properties it lists (see above); run `showSettings`.
3. **Run** the entry-point function (`createVerdictForm`)
4. Authorize when prompted — it needs Forms and Sheets
5. **View → Logs** for the URLs and ids it printed
6. Do the manual check documented at the bottom of the script, then remove the builder file
   from the live project (it is not part of the bundle and `build_bundle.py` would refuse
   a clash with it).

## Apps Script cannot create file-upload questions

`FormApp` has no `addFileUploadItem`, and neither does the Forms REST API. This is a
long-standing platform gap, not something to work around.

It matters for `create_submission_form.gs`, because the submission form's two file-upload
questions are the whole point of it. The script therefore builds everything else and leaves
the form **not accepting responses**, with the two missing questions and their exact titles
logged. Finishing it is a documented manual step (`finishByHand()` in that file), and the
titles must match exactly — `fetch_submissions.py` reads uploaded file ids out of the
response row by column name.

A form left obviously unfinished is better than one that looks complete and silently
collects submissions with no files attached.

## The verdict sheet has to be shared with the fetcher

Settled 2026-08-14, after `fetch_verdicts.py` turned out never to have read it over the
network. Share the responses sheet **Viewer** with the service account —
`fetch_verdicts.py` prints the exact address when Drive refuses it — and leave General
access on **Restricted**.

Never "anyone with the link". This sheet is not the submissions sheet: verdict reason text
quotes what was wrong with somebody's unpublished data, and curator names sit next to it.

The failure mode to recognize: Drive answers **404**, not 403, because a file you have no
access to is invisible rather than forbidden. It reads like a wrong sheet id, and the id is
usually fine.

## The one thing the script cannot be trusted to do

Apps Script's control over **verified** email collection has changed more than once and
differs between consumer and Workspace accounts. `create_verdict_form.gs` tries the modern
call, falls back, and logs loudly — but you must confirm by hand:

> Form → Settings → Responses → Collect email addresses → **Verified**

"Responder input" looks identical in the spreadsheet and is a typed string anyone can put
anything into. Verified is Google confirming the responder controls that mailbox. It is
still not proof they are a curator — `config/curators.yml` decides that — but without it,
anyone who receives a forwarded link could put a colleague's name on a decision.

## The notifiers need triggers, not just a Run

Nothing in the project runs on its own until `installAllTriggers` (triggers.gs) has been
run; `showTriggers` lists what is attached. Do not look for the Triggers screen for the
form triggers: the project is standalone, not bound to a form, and that screen does not
offer "From form" for a standalone project; creating them in code works and is repeatable.

Run `testSubmissionNotification`, `testVerdictNotification` and `testSetup` first. Each
emails a clearly marked sample (the first two to the curators or leads, `testSetup` to the
leads only) and nothing else, so what a curator receives is seen before a real submitter
or curator ever triggers one.

**Its curator list is a copy.** The script cannot read `config/curators.yml` — that lives
in a private repository — so the addresses are the `CURATORS` Script Property. The
registry remains the authority on *who may decide*; this list only controls *who gets
told*. Adding a curator means editing both, and a curator removed from the registry but
left in the script keeps receiving submissions they can no longer vote on.

## What these scripts are not

They are not authority. Everything a form collects is re-checked by
`curation/src/malavi_curation/ledger.py`, which decides whether the address belongs to an
active curator, whether a hold blocks, and whether the person who typed a revision is
allowed to approve it. A form link is unlisted, not private; assume anyone can submit
through it and let the registry make that harmless.
