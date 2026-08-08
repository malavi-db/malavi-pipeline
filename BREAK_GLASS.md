# Break glass in case of emergency --> handing off MalAvi

**Read this if you have inherited MalAvi, or if you are taking over while Vincenzo (current
maintainer) is unavailable.** It is not a guide to running the database day to day. That is
`RUNBOOK.md`. This describes how MalAvi is passed on.

Last verified: **2026-08-08.** If that date is more than a year old, treat every account and
path below as needing confirmation before you rely on it.

> **This file is written to be publishable.** It says what exists, what breaks, and whom you should
> contact. It deliberately does not say how any account is recovered, which addresses or
> devices can reset a password, or what any credential is called in the cloud consoles that
> issue it. Those live in `CUSTODY_PRIVATE.md`, which stays in the private repository and
> must never be copied into a public one.

---

## Current contact info

- **Vincenzo Ellis** — `vaellis@udel.edu`
- **The project address** — `malaviadmin@gmail.com`. Curators and submitters use this; it
  is also the identity that owns the public site. Vincenzo holds it too, at present.

---

## The accounts, and what fails without each

"Who can change it" means who can alter or destroy the thing. Several rows are public and
can be *read* by anyone.

| What | Where it lives | Who can change it today | If it is lost |
|---|---|---|---|
| Submission Form + responses sheet, 2 upload folders | Google Drive, **`malaviadmin@gmail.com`** | Vincenzo only, **no second custodian yet** | New submissions stop arriving. Existing ones survive in the repo's intake tree if they were fetched (which is automatic). |
| Curator Response Form + responses sheet | Same account | Vincenzo only | Curators cannot record decisions and the review process stops. |
| `malaviadmin@gmail.com` itself | Google, consumer account, created 2026-08-06 | Vincenzo only | Everything in the two rows above. **Deleted by Google after 2 years with no sign-in.** |
| **`malavi-db` GitHub organization** | GitHub, created 2026-08-08 | Its **Owner accounts**: `malaviadmin` and `vincenzoaellis` — **both held by Vincenzo today**, so this is two ways in, not two people | Nothing immediately because an organization survives the loss of an Owner account, and more Owners can be added without anyone sharing a password. |
| The public site | `malavi-db/malavi-db.github.io`, online at <https://malavi-db.github.io/> | The org's Owner accounts (above). Anyone can read it because it's a public website | The website goes stale: it keeps serving the last published version, and being public it can be forked and re-hosted by anyone. |
| The public pipeline code | `malavi-db/malavi-pipeline` | The org's Owner accounts. Anyone can read it | It's built from the private repository by `publish/push_pipeline.sh` and can be rebuilt. |
| `malaviR` (public R package) | GitHub, `vincenzoaellis` | Vincenzo only can push | Recoverable: it is public and forkable. Parts of the site are generated with it. |
| MalAvi BLAST | shinyapps.io, **free tier** | Only Vincenzo can deploy | The BLAST app goes offline. Luckily, its code is in `malaviR`. |
| Publishing token | A file in the working copy, gitignored, and in the lifeboat | Whoever has the working copy | Publishing stops until a new token is issued. |
| Drive read key | A service-account key on Vincenzo's BIOMIX home directory | Vincenzo | The fetch jobs stop reading submissions and verdicts. Replaceable by issuing a new key. |
| Curator-report delivery | An Apps Script web app under `malaviadmin@gmail.com`, plus a shared secret on BIOMIX | Vincenzo | Curator reports stop reaching curators. They are still rendered and still on the cluster. Replaceable: redeploy the script, generate a new secret. |
| **The lifeboat** | `~/malavi_lifeboat` on BIOMIX, written by `backup/lifeboat.sh` | Vincenzo | Nothing, a copy of everything. |


---

## What breaks first if nobody touches anything

| When | What happens |
|---|---|
| Immediately | Nothing. The public site is static and will keep working. |
| Days | Submissions keep arriving in Drive and keep being fetched, but nobody reviews them. |
| ~60 days idle | **GitHub disables scheduled workflows on a repository with no activity.** It emails a warning first, to the account owner. The daily submission fetch stops after that. |
| Whenever a token expires | Publishing fails. The site will keep showing the last published version of the data. |
| Next shinyapps change | The BLAST app may stop. |
| 2 years with no sign-in | Google deletes `malaviadmin@gmail.com` and everything under it. |
| Indefinitely | The database itself is safe. The release data is in `malaviR`, on the public site, and in the lifeboat. |

---

## Getting control, step by step

1. **Contact Vincenzo.** Everything below is easier with credentials he can hand over
   directly. The private recovery details are in `CUSTODY_PRIVATE.md`, which is deliberately
   not in this repository. It is held in the maintainer's private repository and in the
   lifeboat (step 6), and is meant to move to a private repository owned by the `malavi-db`
   organization, so that any Owner can read it without logging in as anyone else. Until
   that exists, the lifeboat is the copy a successor can reach.

2. **GitHub — the good case.** Succession is an ownership list, not a password handover.
   An existing owner of the `malavi-db` organization adds the successor's own GitHub account
   as an **Owner** (Organization → People → Invite member → role Owner). Nobody needs to log
   in as anyone else. This should be done *before* it is needed.

3. **GitHub — the bad case.** If no owner is reachable, GitHub has an account-recovery
   process. Everything public can be forked by anyone
   in the meantime, so the site can be brought back up under a new owner without recovering
   anything. The maintainer's private working repository, which holds the full history,
   cannot be forked by anyone; the lifeboat (step 6) is built for that case.

4. **The operational Google account.** `malaviadmin@gmail.com` holds the Forms, Sheets and
   Drive folders. It has 2-Step Verification enabled. Recovery specifics are in the private
   file. Sign in at least once a year.

5. **The publishing token and the Drive read key.** Do not try to recover either. Issue new
   ones. The publishing token is covered in `publish/README.md`. The Drive key is
   documented in the private repository; if you cannot reach that, the Google Cloud console
   shows the service account and you can issue a fresh key from there without it.

6. **If the working copy is gone.** `~/malavi_lifeboat` on BIOMIX (the UD HPC that Vincenzo uses) holds the complete history
   plus the credentials. `RESTORE.md` inside it is the procedure.

7. **Re-run the pipeline** from `RUNBOOK.md` and confirm the test suites pass before changing anything.