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

## Files

| File | What it does |
|---|---|
| `create_verdict_form.gs` | Builds the curator verdict form (verdict / override / correction), wires it to a fresh responses spreadsheet, and prints the ids needed for prefilled links. |
| `create_submission_form.gs` | Rebuilds the public data submission form under the operational account. **Cannot finish on its own** — see below. |
| `notify_on_submission.gs` | Emails the curators when a submission arrives, and acknowledges the submitter. Installed as an on-form-submit trigger. |

## Running one

**Sign in as the MalAvi operational account first** — `malaviadmin@gmail.com`. Whatever
account runs the script *owns* what it creates, and running it as yourself would put
MalAvi's form back on a personal account, which is precisely what the operational account
exists to prevent.

1. [script.google.com](https://script.google.com) → **New project**
2. Paste the `.gs` file in, name the project after it
3. **Run** the entry-point function (`createVerdictForm`)
4. Authorize when prompted — it needs Forms and Sheets
5. **View → Logs** for the URLs and ids it printed
6. Do the manual check documented at the bottom of the script

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

## The one thing the script cannot be trusted to do

Apps Script's control over **verified** email collection has changed more than once and
differs between consumer and Workspace accounts. `create_verdict_form.gs` tries the modern
call, falls back, and logs loudly — but you must confirm by hand:

> Form → Settings → Responses → Collect email addresses → **Verified**

"Responder input" looks identical in the spreadsheet and is a typed string anyone can put
anything into. Verified is Google confirming the responder controls that mailbox. It is
still not proof they are a curator — `config/curators.yml` decides that — but without it,
anyone who receives a forwarded link could put a colleague's name on a decision.

## The notifier needs a trigger, not just a Run

`notify_on_submission.gs` does nothing until it is attached to the form:

> **Triggers** (clock icon) → **Add trigger** → function `onSubmissionReceived`,
> event source **From form**, event type **On form submit**

Run `testNotification` first. It emails you one sample and nothing else, so you can see
exactly what a curator receives before a real submitter ever triggers one.

**Its curator list is a copy.** The script cannot read `config/curators.yml` — that lives
in a private repository — so the addresses are a constant at the top of the file. The
registry remains the authority on *who may decide*; this list only controls *who gets
told*. Adding a curator means editing both, and a curator removed from the registry but
left in the script keeps receiving submissions they can no longer vote on.

## What these scripts are not

They are not authority. Everything a form collects is re-checked by
`curation/src/malavi_curation/ledger.py`, which decides whether the address belongs to an
active curator, whether a hold blocks, and whether the person who typed a revision is
allowed to approve it. A form link is unlisted, not private; assume anyone can submit
through it and let the registry make that harmless.
