#!/usr/bin/env bash
# @title Publish the rendered MalAvi site to the public GitHub Pages repository
# @purpose Copy docs/ from this private repo into a clone of the public site
#          repo, commit, and push, so GitHub Pages serves the current build.
# @why malavi_rebuild is private and holds curation and watcher tooling that
#      must not be public. GitHub Pages cannot serve from a private repo on a
#      free plan, so the rendered site lives in a separate public repo. Copying
#      only docs/ makes it structurally impossible to publish anything else.
# @input docs/
# @output <SITE_REPO_DIR> (a clone of the public site repo)
# @program git
# @program rsync
# @critical-var SITE_REPO_URL
# @critical-var SITE_REPO_DIR
# @critical-var SOURCE_DIR
# =============================================================================
# Usage:
#   publish/push_site.sh --dry-run   # show exactly what would change, push nothing
#   publish/push_site.sh             # sync, commit and push
#
# First run: set SITE_REPO_URL below (or export it), then run with --dry-run.
# =============================================================================

set -eEuo pipefail

# ---- configuration ----------------------------------------------------------
# The PUBLIC repository that GitHub Pages serves. Nothing but the contents of
# docs/ is ever written into it.
SITE_REPO_URL="${SITE_REPO_URL:-https://github.com/malavi-db/malavi-db.github.io.git}"

# A GitHub personal access token with `repo` scope, used to authenticate the
# push over HTTPS. Kept in a file that .gitignore excludes, never inside a URL:
# a token embedded in a remote URL gets written to .git/config in plaintext and
# leaks into any `git remote -v` output or shell history.
TOKEN_FILE="${TOKEN_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/malavi_site_github_token.txt}"

# Where the clone of that public repo is kept locally. Deliberately outside this
# repo so it can never be committed here by accident.
SITE_REPO_DIR="${SITE_REPO_DIR:-${HOME}/malavi-db.github.io}"

# The directory in THIS repo that is published. Only this is copied.
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docs"

LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/publish_$(date +%Y%m%d_%H%M%S).log"

# An unrecognized argument is a hard error, not a live publish. This is the program that
# stands between the private tree and the public web, and the old test -- "is argument 1
# exactly --dry-run?" -- meant that --dryrun, --dry_run or -n all synced, committed and
# pushed to the public site while the person typing believed they were previewing.
DRY_RUN=0
case "${1:-}" in
  "")         ;;
  --dry-run)  DRY_RUN=1 ;;
  *)          printf 'unknown argument: %s\nusage: push_site.sh [--dry-run]\n' "$1" >&2
              exit 2 ;;
esac

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "${LOG_FILE}"; }

# ---- authentication ---------------------------------------------------------
# Feed the token to git through a credential helper on the command line, so it
# lives only in this process's memory for the duration of one command. Nothing
# is written to .git/config, ~/.git-credentials, or the log.
#
# `git_auth <args...>` runs git with credentials attached; use it only for the
# commands that actually talk to the remote.
# The token is read with `tr -d '[:space:]'` rather than plain `cat`. A token
# file saved on Windows, or pasted with a stray newline, carries a trailing
# carriage return that command substitution does NOT strip -- git then sends
# "ghp_...\r" and GitHub rejects it with a misleading "Invalid username or
# token". Stripping all whitespace makes the helper indifferent to how the file
# was written.
#
# For a classic PAT the username field is ignored by GitHub, but it must be
# non-empty; the token itself is what authenticates.
git_auth() {
  if [[ -f "${TOKEN_FILE}" ]]; then
    git -c "credential.helper=!f() { printf '%s\n' username=token password=\"\$(tr -d '[:space:]' < '${TOKEN_FILE}')\"; }; f" "$@"
  else
    # No token file: fall back to whatever git is already configured with
    # (an SSH remote, a system credential manager, or an interactive prompt).
    git "$@"
  fi
}

log "== malavi_rebuild :: publish site =="
log "source     : ${SOURCE_DIR}"
log "site repo  : ${SITE_REPO_URL}"
log "local clone: ${SITE_REPO_DIR}"
log "dry run    : ${DRY_RUN}"

# ---- validate the source ----------------------------------------------------
[[ -d "${SOURCE_DIR}" ]] || { log "ERROR: ${SOURCE_DIR} does not exist."; exit 1; }
[[ -f "${SOURCE_DIR}/index.html" ]] || { log "ERROR: no index.html in ${SOURCE_DIR}."; exit 1; }

# .nojekyll must be present or GitHub will run Jekyll, which drops files and
# directories whose names begin with an underscore.
if [[ ! -f "${SOURCE_DIR}/.nojekyll" ]]; then
  log "NOTE: adding missing .nojekyll to ${SOURCE_DIR}"
  [[ "${DRY_RUN}" -eq 1 ]] || touch "${SOURCE_DIR}/.nojekyll"
fi

# ---- guard: never publish anything that looks private -----------------------
# A last line of defense. docs/ is supposed to contain only rendered site files;
# if any of these appear there, something has gone wrong upstream and the push
# must stop rather than leak.
log ""
log "-- scanning docs/ for files that must never be published --"
FORBIDDEN_PATTERNS=(
  "*.env" ".env" "*.key" "*.pem" "id_rsa*" "*credentials*" "*secret*"
  "*.xlsx.tmp" "seen.json" "*.sqlite" "*.Rhistory"
)
FOUND_FORBIDDEN=0
for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
  while IFS= read -r hit; do
    log "  REFUSING TO PUBLISH: ${hit}"
    FOUND_FORBIDDEN=1
  done < <(find "${SOURCE_DIR}" -name "${pattern}" 2>/dev/null)
done
if [[ "${FOUND_FORBIDDEN}" -eq 1 ]]; then
  log "ERROR: docs/ contains files that must not be public. Nothing was pushed."
  exit 1
fi
log "  clean."

# ---- clone or refresh the site repo ----------------------------------------
if [[ ! -d "${SITE_REPO_DIR}/.git" ]]; then
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log ""
    log "[dry-run] would clone ${SITE_REPO_URL} into ${SITE_REPO_DIR}"
    log "[dry-run] stopping here: nothing to diff against until the clone exists."
    exit 0
  fi
  log ""
  log "cloning ${SITE_REPO_URL} -> ${SITE_REPO_DIR}"
  git_auth clone "${SITE_REPO_URL}" "${SITE_REPO_DIR}"
else
  log ""
  log "refreshing existing clone"
  git_auth -C "${SITE_REPO_DIR}" fetch --quiet origin
  git -C "${SITE_REPO_DIR}" checkout --quiet main 2>/dev/null || \
    git -C "${SITE_REPO_DIR}" checkout --quiet -b main
  git -C "${SITE_REPO_DIR}" reset --hard --quiet origin/main 2>/dev/null || true
fi

# ---- sync ------------------------------------------------------------------
# --delete so files removed from docs/ also disappear from the live site.
# .git is excluded so the destination repo's own history is never touched.
#
# The other exclusions are files that live in docs/ for development but have no
# business on a public site:
#   _config_notes.md   internal deployment notes (superseded by publish/README.md)
#   assets/js/tests/   the Node test suite for the sequence checker
#   assets/js/sanger/  the .ab1 pipeline modules and their tests. Unfinished,
#                      not imported by any page, and developed against the
#                      malavi_sanger project rather than the site. They live
#                      here so they can share the checker's code; publishing
#                      them would put work-in-progress on the public repo.
#   *.html stubs       the pre-redesign scaffold pages, superseded by index.html
#                      and no longer linked from anywhere. Publishing them would
#                      leave stale pages contradicting the live site.
#   style.css, site.js, datatables-init.js, references.json, site.json
#                      the scaffold's assets, likewise superseded.
#
# THE SCAFFOLD FILES WERE DELETED ON 2026-08-13 and their exclusions kept, the
# same way sanger.html's was kept after that tool moved out. An exclusion for a
# file that does not exist costs nothing and is the only thing that would stop a
# name being republished if it ever came back -- which is how the scaffold got
# onto the live site in the first place. They were never live: the live tree held
# only index.html, curating.html and how-it-works.html when this was written.
#
# submit.html is gone from here too, but for the opposite reason. It was NOT
# empty scaffold: it held Staffan's submission instructions, several paragraphs
# of which existed in no other file. It moved to ops/submit.src.html, out of the
# published tree but intact. On 2026-08-14 the paragraphs that existed nowhere
# else were folded into the Submit tab of index.html and published; the header of
# ops/submit.src.html lists exactly which ones and where they landed. Its
# exclusion stays here regardless, for the reason given above.
#
# Note on what is NOT excluded: assets/img/hemignathus_virens.png is the
# 'amakihi illustration, which index.html embeds inline as a base64 data URI, so
# no browser ever fetches the standalone file. It is published anyway, and
# deliberately. Excluding it would not remove the copy already on the live site:
# rsync's --delete protects excluded files that exist on the receiver, so the
# exclusion would only freeze a stale copy there. The flag that would delete it,
# --delete-excluded, is unusable here because ".git/" is also excluded and that
# flag would erase the destination repo's history. At 11 KB, publishing the
# source image alongside the page it is embedded in is the safe trade.
RSYNC_FLAGS=(
  -a --delete
  --exclude ".git/"
  --exclude "_config_notes.md"
  --exclude "assets/js/tests/"
  # The Sanger .ab1 tool is a separate, unfinished project that lives in this tree. Its
  # modules were excluded in d3e7b5e; the PAGE that loads them was not, and nothing else
  # here would have stopped it shipping. sanger.html imports assets/js/sanger/ui.mjs and
  # assets/css/style.css and assets/js/site.js -- all excluded -- so publishing it alone
  # puts a page on the public site whose script and stylesheet both 404.
  --exclude "assets/js/sanger/"
  --exclude "sanger.html"
  --exclude "assets/css/sanger.css"
  --exclude "about.html" --exclude "blast.html"
  --exclude "submit.html" --exclude "tables.html"
  --exclude "assets/css/style.css"
  --exclude "assets/js/site.js" --exclude "assets/js/datatables-init.js"
  --exclude "assets/data/references.json" --exclude "assets/data/site.json"
  --exclude "assets/data/README.md"
)
[[ "${DRY_RUN}" -eq 1 ]] && RSYNC_FLAGS+=(--dry-run)

log ""
log "-- syncing docs/ -> site repo --"
rsync "${RSYNC_FLAGS[@]}" -i "${SOURCE_DIR}/" "${SITE_REPO_DIR}/" | tee -a "${LOG_FILE}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log ""
  log "[dry-run] nothing was written or pushed."
  exit 0
fi

# ---- cache-bust the versioned assets ---------------------------------------
# GitHub Pages serves everything with `Cache-Control: max-age=600` and offers no
# way to change that. A visitor who has the page cached therefore keeps running
# the OLD malavi.js for up to ten minutes after a publish -- and that stale
# script fetches the data feeds, so the whole page silently shows stale content.
# Observed in the wild: a submission removed from the queue kept appearing.
#
# Fix: append a content hash to the script and stylesheet URLs in the PUBLISHED
# index.html. When the HTML is fetched, the changed URL forces a fresh download
# of the asset instead of a cache hit. The hash is derived from the file's own
# bytes, so it changes exactly when the file does and never otherwise -- an
# unchanged asset keeps its URL and stays cached, which is the point.
#
# This rewrites only the copy in the site repo. docs/ keeps clean, un-suffixed
# URLs so the site still opens correctly from a local file path.
cache_bust() {
  local asset="$1" pattern="$2" hash
  [[ -f "${SITE_REPO_DIR}/${asset}" ]] || return 0
  hash="$(sha256sum "${SITE_REPO_DIR}/${asset}" | cut -c1-8)"
  # Strip any previous ?v=... first, so re-running never stacks suffixes.
  sed -i -E "s|${pattern}(\?v=[a-f0-9]+)?|${pattern}?v=${hash}|g" \
    "${SITE_REPO_DIR}/index.html"
  log "  ${asset} -> ?v=${hash}"
}

log ""
log "-- cache-busting versioned assets --"
cache_bust "assets/js/malavi.js"   "assets/js/malavi\.js"
cache_bust "assets/css/malavi.css" "assets/css/malavi\.css"

# ---- commit and push --------------------------------------------------------
cd "${SITE_REPO_DIR}"
SOURCE_COMMIT="$(git -C "$(dirname "${SOURCE_DIR}")" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# Commit whatever the sync changed, if anything.
if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  # The public repo's history is public. Keep the message plain: it should not
  # name the private repo or expose its commit hashes.
  git commit -q -m "Update site"
  log ""
  log "committed the synced changes."
else
  log ""
  log "working tree already matches docs/ — nothing new to commit."
fi

# A clean working tree does NOT mean there is nothing to publish: an earlier run
# may have committed successfully and then failed to push (a bad token, no
# network). Compare against the remote and push whenever we are ahead of it,
# rather than treating "no local changes" as "already live".
if git rev-parse --verify --quiet HEAD >/dev/null; then
  UPSTREAM_COUNT="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo "all")"
else
  log "nothing has ever been committed to the site repo; nothing to publish."
  exit 0
fi

if [[ "${UPSTREAM_COUNT}" == "0" ]]; then
  log "the published site already matches docs/; nothing to push."
  exit 0
fi

log "pushing ${UPSTREAM_COUNT} commit(s) to origin/main"
git_auth push -q -u origin main

log ""
log "published. Source commit: ${SOURCE_COMMIT}"
log "GitHub Pages will redeploy within a minute or two."
