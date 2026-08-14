# Giving the fetch jobs read-only access to MalAvi's Drive

**Why this is needed.** Until 2026-08-06 the submission responses sheet was shared with
"anyone with the link", and the fetcher read it over plain HTTPS with no credentials. That
had a real virtue — no token to leak, rotate or expire, reproducible on any machine — and
it was given up for a plainer reason: the same link let anyone who ever saw it read every
submitter's email address and their unpublished sequences, and that link lived in a config
file, in git history, and in every clone.

So the sheets and upload folders are now private, and a job that reads them has to prove
who it is. This is the one-time setup for that, done by the **maintainer**, not a curator.

Budget about twenty minutes. It is all browser work except the last step.

---

## What you are creating

A **service account** — a Google identity that belongs to a program rather than a person.
It gets its own email address, you share the sheets and folders with that address exactly
as you would with a colleague, and it holds a key file instead of a password.

A service account is used rather than signing in as `malaviadmin` because a user login
needs a browser and a human, which a scheduled job has neither of. The alternative — an
OAuth refresh token — expires after seven days while the OAuth app is in "Testing" status,
which is a well-known way to build something that works all week and breaks on Monday.

---

## 1. Create the project and the service account

Signed in as **`malaviadmin@gmail.com`** throughout.

1. Go to [console.cloud.google.com](https://console.cloud.google.com). Accept the terms if
   asked. **No billing account is needed** — nothing here costs anything.
2. Create a project. Name it `malavi`.
3. **APIs & Services → Library →** search **Google Drive API →** *Enable*.
   (Drive's API covers both downloading an uploaded file and exporting a Sheet as CSV, so
   the Sheets API is not needed.)
4. **APIs & Services → Credentials → Create credentials → Service account**.
   - Name: `malavi-fetcher`
   - Skip the two optional steps ("Grant this service account access to project" and
     "Grant users access"). Neither applies: its access comes from Drive sharing, not from
     project roles.
5. Open the new service account → **Keys → Add key → Create new key → JSON**. A file
   downloads. **This file is the credential** — anyone holding it can read everything the
   account can read, with no password and no second factor.

---

## 2. Put the key somewhere outside the repository

```bash
mkdir -p ~/.config/malavi
mv ~/Downloads/malavi-*.json ~/.config/malavi/service-account.json
chmod 600 ~/.config/malavi/service-account.json
```

Then point the tools at it, in `config/project.yml`:

```yaml
google:
  service_account_key: ~/.config/malavi/service-account.json
```

or per-run, which overrides the config:

```bash
export MALAVI_GOOGLE_KEY=~/.config/malavi/service-account.json
```

`google_auth.py` **refuses a key stored inside the repository** rather than trusting
`.gitignore` to keep it out of history. That is not fussiness: relying on the ignore file
means relying on nobody ever running `git add -f`, nobody copying it to a differently-named
file, and every future clone keeping the same rules.

---

## 3. Share the four things with it

Find the service account's address — it looks like
`malavi-fetcher@malavi-XXXXXX.iam.gserviceaccount.com`. It is in the Cloud console, and
also printed by:

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'curation/src')
from malavi_curation import google_auth; print(google_auth.describe())"
```

Share each of these with that address as **Viewer**, exactly as you would share with a
person:

| What | Where |
|---|---|
| Submission responses sheet | `submissions.responses_sheet` in config |
| Curator verdict responses sheet | `review.verdict_sheet` |
| Template upload folder | `submissions.template_folder` |
| Materials upload folder | `submissions.materials_folder` |

Both folders matter. Sharing only the sheet gets you a fetch that reads every response and
then fails on every file.

**Turn off "Notify people"** when sharing — a service account has no inbox and Google will
report a bounce.

---

## 4. Install the dependency and test

```bash
pip install -e 'curation[google]'
.venv/bin/python curation/fetch_submissions.py --dry-run
```

`--dry-run` lists what would be fetched and writes nothing.

### What the errors mean

| What you see | What to do |
|---|---|
| `Google access: none configured` | Step 2 — the key path is not set |
| `key configured at …, but the file is not there` | The path is wrong, or the file was moved |
| `The 'google' extra is not installed` | `pip install -e 'curation[google]'` |
| `Drive refused the responses sheet (403/404)` | Step 3 — share it with the address the error prints. 403 and 404 are the same mistake here; Drive returns 404 for things you are not allowed to know exist |
| Reads the sheet, fails on files | Step 3, but for the two **folders** |

---

## Living with it

**The key does not expire, which is the problem with it.** Nothing will remind you it
exists. It is a bearer credential valid until someone revokes it, so it belongs in
`BREAK_GLASS.md` alongside the publishing token, and it should be replaced if the machine
holding it is lost or shared.

**Scope is read-only** (`drive.readonly`) and should stay that way. A fetch job has no
business writing to Drive, and a credential that cannot write cannot destroy a submission
by accident.

**Revoking is instant**: delete the key in the Cloud console, or remove the service
account's access from the shared items. Either stops it.

**In CI**, put the JSON key in a repository secret and write it to a temporary file at the
start of the run. Note that the submission sheet holds unpublished data and submitter
addresses, so anything printed in a CI log is readable by every repository collaborator —
`check_template.py --redact` exists for that reason.
