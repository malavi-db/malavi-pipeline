#!/usr/bin/env bash
# @title Publish only the site's data feeds to the public GitHub Pages repository
# @purpose Copy the three generated JSON feeds -- the submission queue, the contributor
#          board, and the name reservations -- into the public site repo and push, so a
#          curator's decision reaches the public queue without republishing the site.
# @why push_site.sh syncs the whole of docs/. Running it automatically on every review
#      state change would publish whatever else happened to be in docs/ at that moment --
#      a half-finished page, a rebuilt curating.html from an aborted build. This script
#      can only ever move three generated files, so an automatic publish is safe.
# @input docs/assets/data/queue.json
# @input docs/assets/data/contributors.json
# @input docs/assets/data/reserved_names.json
# @input docs/assets/data/board.json (optional: the curators' board, encrypted)
# @output <SITE_REPO_DIR> (a clone of the public site repo)
# @program git
# @critical-var SITE_REPO_URL
# @critical-var SITE_REPO_DIR
# @critical-var FEEDS
# @critical-var OPTIONAL_FEEDS
# =============================================================================
# Usage:
#   publish/push_feeds.sh --dry-run   # show what would change, push nothing
#   publish/push_feeds.sh             # copy, commit and push
#
# This is the narrow companion to push_site.sh, not a replacement for it. It
# publishes DATA only. A change to any page, stylesheet or script still needs
# push_site.sh, and so does the very first publish -- this script refuses to run
# until the local clone exists, because it has no business creating one.
#
# THE ALLOWLIST IS THE WHOLE POINT. FEEDS below is an explicit list of three
# filenames, not a glob over assets/data/. A glob would quietly start publishing
# anything a future step dropped into that directory; the list cannot. If a
# fourth feed is ever added to the site, adding it here is a deliberate act.
# =============================================================================

set -eEuo pipefail

# ---- configuration ----------------------------------------------------------
# Kept identical to push_site.sh, deliberately. Two publishers disagreeing about
# which repository is the public one is the kind of divergence that ends with a
# push to the wrong place.
SITE_REPO_URL="${SITE_REPO_URL:-https://github.com/malavi-db/malavi-db.github.io.git}"
TOKEN_FILE="${TOKEN_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/malavi_site_github_token.txt}"
SITE_REPO_DIR="${SITE_REPO_DIR:-${HOME}/malavi-db.github.io}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docs"

# The privacy scanner (guard rails of 2026-10-16). The checkout's own interpreter when
# there is one (BIOMIX), else whatever python3 the runner installed the package into.
SCAN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "${SCAN_ROOT}/.venv/bin/python" ]]; then
  SCAN_PYTHON="${SCAN_ROOT}/.venv/bin/python"
else
  SCAN_PYTHON="${PYTHON:-python3}"
fi

# The only files this program may ever publish, relative to docs/.
FEEDS=(
  "assets/data/queue.json"
  "assets/data/contributors.json"
  "assets/data/reserved_names.json"
)

# Published when present, and not required. The curators' board
# (curation/build_status_board.py) exists only once a board password has been set
# up, and an optional feature that is not set up must not stop the public queue
# from being published. The file is ENCRYPTED (AES-256-GCM); what the public
# repository receives is ciphertext. It is still on this list by name, like the
# three above: the allowlist is the whole point.
OPTIONAL_FEEDS=(
  "assets/data/board.json"
)

# Filled in below: the required feeds plus whichever optional ones exist.
PUBLISH=()

LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/publish_feeds_$(date +%Y%m%d_%H%M%S).log"

# An unrecognized argument is a hard error rather than a live publish, for the
# same reason it is in push_site.sh: --dryrun, --dry_run and -n must not push.
DRY_RUN=0
case "${1:-}" in
  "")         ;;
  --dry-run)  DRY_RUN=1 ;;
  *)          printf 'unknown argument: %s\nusage: push_feeds.sh [--dry-run]\n' "$1" >&2
              exit 2 ;;
esac

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "${LOG_FILE}"; }

# ---- authentication ---------------------------------------------------------
# Identical to push_site.sh: the token is fed to git through a credential helper
# on the command line, so it lives only in this process's memory and is never
# written to .git/config, ~/.git-credentials, or the log. `tr -d '[:space:]'`
# strips the trailing carriage return a token file saved on Windows carries,
# which git would otherwise send verbatim for GitHub to reject with a
# misleading "Invalid username or token".
git_auth() {
  if [[ -f "${TOKEN_FILE}" ]]; then
    git -c "credential.helper=!f() { printf '%s\n' username=token password=\"\$(tr -d '[:space:]' < '${TOKEN_FILE}')\"; }; f" "$@"
  else
    git "$@"
  fi
}

log "== malavi_rebuild :: publish data feeds =="
log "source     : ${SOURCE_DIR}"
log "local clone: ${SITE_REPO_DIR}"
log "dry run    : ${DRY_RUN}"

# ---- validate the source ----------------------------------------------------
# Every feed must exist and must be readable JSON. A truncated or half-written
# feed is worse than a stale one: the page fetches all three on load, and a
# parse error takes out the section that reads it. Checking here means a bad
# write from the generator stops at this boundary rather than on the live site.
log ""
log "-- checking the feeds --"
for feed in "${FEEDS[@]}"; do
  path="${SOURCE_DIR}/${feed}"
  if [[ ! -f "${path}" ]]; then
    log "ERROR: ${feed} does not exist. Run build_site_feeds.py first."
    exit 1
  fi
  if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${path}" 2>/dev/null; then
    log "ERROR: ${feed} is not readable JSON. Nothing was pushed."
    exit 1
  fi
  log "  ok: ${feed} ($(wc -c < "${path}") bytes)"
  PUBLISH+=("${feed}")
done

# The optional feeds: absent is fine, present-but-unreadable is not.
for feed in "${OPTIONAL_FEEDS[@]}"; do
  path="${SOURCE_DIR}/${feed}"
  if [[ ! -f "${path}" ]]; then
    log "  not present (optional): ${feed}"
    continue
  fi
  if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${path}" 2>/dev/null; then
    log "ERROR: ${feed} is not readable JSON. Nothing was pushed."
    exit 1
  fi
  log "  ok: ${feed} ($(wc -c < "${path}") bytes)"
  PUBLISH+=("${feed}")
done

# ---- the privacy scan (guard rails of 2026-10-16) ---------------------------
# The feeds are the one public destination the unattended cycle writes to. Every
# file about to be copied is scanned for submitters' names, addresses, Google ids,
# reason text and unpublished sequences; a hit stops the publish here, loudly.
SCAN_PATHS=()
for feed in "${PUBLISH[@]}"; do SCAN_PATHS+=("${SOURCE_DIR}/${feed}"); done
log ""
log "-- privacy scan of the feeds --"
if ! "${SCAN_PYTHON}" "${SCAN_ROOT}/curation/privacy_scan.py" --destination feeds \
     --base "${SOURCE_DIR}" "${SCAN_PATHS[@]}" 2>&1 | tee -a "${LOG_FILE}"; then
  log "ERROR: the privacy scan refused the feeds. Nothing was pushed."
  exit 1
fi

# ---- the clone must already exist -------------------------------------------
# This program publishes data into an existing site. It does not stand up a new
# one: a clone created here would be pushed with three JSON files and no pages.
if [[ ! -d "${SITE_REPO_DIR}/.git" ]]; then
  log ""
  log "ERROR: no clone of the site repo at ${SITE_REPO_DIR}."
  log "       Run publish/push_site.sh once first; this script only updates feeds."
  exit 1
fi

log ""
log "refreshing the clone"
git_auth -C "${SITE_REPO_DIR}" fetch --quiet origin
git -C "${SITE_REPO_DIR}" checkout --quiet main
# Discard any local drift before copying, so this run publishes exactly what is
# in docs/ plus what is already live -- never a leftover from a failed run.
git -C "${SITE_REPO_DIR}" reset --hard --quiet origin/main

# ---- copy ------------------------------------------------------------------
log ""
log "-- copying feeds --"
for feed in "${PUBLISH[@]}"; do
  src="${SOURCE_DIR}/${feed}"
  dest="${SITE_REPO_DIR}/${feed}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    if [[ -f "${dest}" ]] && cmp -s "${src}" "${dest}"; then
      log "  unchanged: ${feed}"
    else
      log "  would update: ${feed}"
      # The board is ciphertext: a diff of it would be forty lines of base64.
      if [[ " ${OPTIONAL_FEEDS[*]} " != *" ${feed} "* ]]; then
        diff <(python3 -m json.tool "${dest}" 2>/dev/null || echo "(not on the live site yet)") \
             <(python3 -m json.tool "${src}") | head -40 | sed 's/^/      /' | tee -a "${LOG_FILE}" || true
      fi
    fi
  else
    mkdir -p "$(dirname "${dest}")"
    cp "${src}" "${dest}"
    log "  copied: ${feed}"
  fi
done

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log ""
  log "[dry-run] nothing was written or pushed."
  exit 0
fi

# ---- commit and push --------------------------------------------------------
cd "${SITE_REPO_DIR}"

# Belt and braces: refuse to commit if the copy somehow touched anything outside
# the allowlist. `git status` here is the only thing that would catch a mistake
# in the loop above before it became a public commit.
UNEXPECTED="$(git status --porcelain | awk '{print $2}' | grep -vFx -f <(printf '%s\n' "${PUBLISH[@]}") || true)"
if [[ -n "${UNEXPECTED}" ]]; then
  log ""
  log "ERROR: the working tree changed outside the feed allowlist:"
  printf '%s\n' "${UNEXPECTED}" | sed 's/^/  /' | tee -a "${LOG_FILE}"
  log "Nothing was committed. Investigate before publishing."
  exit 1
fi

if [[ -z "$(git status --porcelain)" ]]; then
  log ""
  log "the published feeds already match docs/; nothing to push."
  exit 0
fi

git add -- "${PUBLISH[@]}"
# The public repo's history is public. Keep the message plain: it must not name
# the private repo, a submission id, or a curator.
git commit -q -m "Update data feeds"
log ""
log "committed the feed changes."

log "pushing to origin/main"
git_auth push -q -u origin main

log ""
log "published. The site fetches these feeds with cache: \"no-cache\", so the"
log "change is visible on the next page load rather than after a cache expiry."
