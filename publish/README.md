# Publishing the MalAvi site

The site is served by **GitHub Pages from a separate public repository**.
`malavi_rebuild` (this repo) stays private, because it holds the curation
tooling, the watcher, submission material and working notes. Only the rendered
contents of `docs/` are ever copied out.

```
malavi_rebuild  (PRIVATE)                malavi-db.github.io  (PUBLIC)
├── curation/      never published
├── watcher/       never published   ──▶  everything in docs/, and nothing else
├── export/        never published        served by GitHub Pages
└── docs/          ──── published ───▶
```

`publish/push_site.sh` is the only thing that crosses that boundary. It refuses
to run if anything credential-shaped has found its way into `docs/`.

## One-time setup

1. **Create the public repo.** On GitHub, create a new **public** repository
   named `<owner>.github.io`. Do not add a README, license or `.gitignore` —
   the first publish should land in an empty repo.

   The repository name matters. Named `<owner>.github.io` it is a *user or
   organization site* and serves from the domain root; named anything else it
   is a *project site* and serves from a subdirectory. The live site uses the
   first form, which is why the repo is `malavi-db/malavi-db.github.io` and the
   address is `https://malavi-db.github.io/` with no path.

2. **Point the script at it.** The default in `publish/push_site.sh` is
   `https://github.com/malavi-db/malavi-db.github.io.git`. If you used a
   different account or name, either edit `SITE_REPO_URL` in the script or
   export it:

   ```bash
   export SITE_REPO_URL=git@github.com:<user>/<repo>.git
   ```

3. **Check SSH access to GitHub** from the machine you publish from:

   ```bash
   ssh -T git@github.com     # expect "Hi <user>! You've successfully authenticated"
   ```

   If that fails, use the HTTPS URL instead and let git prompt for a personal
   access token.

4. **Dry run first.** Nothing is written or pushed:

   ```bash
   publish/push_site.sh --dry-run
   ```

5. **Publish.**

   ```bash
   publish/push_site.sh
   ```

6. **Turn Pages on.** In the public repo: **Settings → Pages → Source: Deploy
   from a branch → Branch `main` / folder `/ (root)`**. Save. The site appears
   at `https://<owner>.github.io/` within a minute or two.

   The folder is `/ (root)`, not `/docs`: the script copies the *contents* of
   `docs/` to the top of the public repo, so `index.html` sits at the root.

## Publishing an update

This is the short path, for republishing the **same** release — a copy edit, a fixed link, a
regenerated figure:

```bash
Rscript export/build_site_stats.R        # refresh the numbers the site shows
Rscript export/build_sequence_index.R    # refresh the sequence checker's index
node docs/assets/js/tests/test_sequence_check.mjs   # must pass before publishing
publish/push_site.sh
```

**If the pinned release changed, this is not enough — use RUNBOOK §6, which runs all six
export scripts.** In particular `build_downloads.R` writes the per-release download files,
and every download link on the site is built from the release string, so publishing a new
release without it leaves every one of those links pointing at a file that was never
written.

## A custom domain, later

Pages will serve `malavi.org` (or similar) if you add a `CNAME` file containing
the bare domain to `docs/`, then set the DNS records GitHub asks for. Because
the script syncs the whole of `docs/`, the `CNAME` file carries across on its
own — put it in `docs/`, not in the public repo, or the next publish will
delete it.

## What must never be published

`push_site.sh` scans `docs/` and aborts on anything matching a credential or
working-file pattern. That check is a backstop, not a substitute for care:
`docs/` should only ever contain HTML, CSS, JS, images and the generated data
payloads under `docs/assets/data/`.

Copyrighted source material — submitted PDFs, supplementary files, the
`Papers.zip` corpus — is excluded by `.gitignore` and must not be moved into
`docs/` to make it downloadable.
