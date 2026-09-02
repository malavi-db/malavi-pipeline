#!/usr/bin/env bash
# @title Publish the clean pipeline code to the public GitHub repository
# @purpose Copy the files named in publish/public_manifest.txt into a clone of
#          malavi-db/malavi-pipeline, refuse if anything sensitive appears in the
#          result, commit and push.
# @why The community should be able to read and run the code that builds MalAvi. The
#      private repository cannot simply be made public: its history holds working notes,
#      and its tree holds unpublished submissions, curator identities and credentials.
# @input publish/public_manifest.txt
# @input config/project.yml (read ONLY to learn which values must never be published)
# @output <PUBLIC_REPO_DIR> (a clone of the public repository)
# @program git
# @program rsync
# @critical-var PUBLIC_REPO_URL
# @critical-var PUBLIC_REPO_DIR
# @critical-var MANIFEST
# @critical-flag push_pipeline.sh "" --dry-run
# =============================================================================
# Usage:
#   publish/push_pipeline.sh --dry-run   # build and scan, push nothing
#   publish/push_pipeline.sh             # build, scan, commit and push
# =============================================================================
#
# TWO INDEPENDENT DEFENSES, BECAUSE ONE IS NOT ENOUGH
# ---------------------------------------------------
# 1. An ALLOWLIST of paths. Nothing reaches the staging tree unless the manifest names
#    it. A file added to the private repository tomorrow is absent from the public one
#    until somebody adds it on purpose.
#
# 2. A CONTENT SCAN of the finished tree for the ACTUAL sensitive values, read out of
#    the private config at run time. This is the one that catches what the first misses:
#    a Drive folder id pasted into a docstring, a sheet id in a test fixture, an endpoint
#    URL in a comment. The private config is the source of truth for "what must never
#    appear", so a value added there is automatically forbidden here, with no second list
#    to keep in step.
#
# The history of the public repository is its own. It is never a fork, a filtered copy or
# a graft of the private history -- it is a series of sync commits into a tree that began
# empty. Nothing written in a private commit message, and nothing committed and later
# deleted, can reach it.
# =============================================================================
set -eEuo pipefail

# ---- configuration ----------------------------------------------------------
PUBLIC_REPO_URL="${PUBLIC_REPO_URL:-https://github.com/malavi-db/malavi-pipeline.git}"
PUBLIC_REPO_DIR="${PUBLIC_REPO_DIR:-${HOME}/malavi-pipeline}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${ROOT}/publish/public_manifest.txt"
PRIVATE_CONFIG="${ROOT}/config/project.yml"

# The same token that publishes the site. malaviadmin owns the organization, so it can
# push here too. Read from a file, never embedded in a URL -- see push_site.sh.
TOKEN_FILE="${TOKEN_FILE:-${ROOT}/malavi_site_github_token.txt}"

# Noise that is never wanted in any published directory copy.
ALWAYS_SKIP=(
  --exclude "__pycache__/" --exclude "*.pyc" --exclude "*.pyo"
  --exclude ".DS_Store" --exclude "*.swp" --exclude ".pytest_cache/"
  --exclude ".ipynb_checkpoints/" --exclude "*.egg-info/"
)

# Long identifiers that ARE already public, and so must not trip the content scan.
# Every entry needs a reason. Anything not listed here that looks like an id is refused,
# which is the safe direction: a new sensitive value is caught without anybody
# remembering to add it, and making something public takes a deliberate edit.
PUBLIC_OK=(
  # The submission form. Linked from every page of the website and printed on the
  # submit tab, so it is public by design.
  "1FAIpQLSerNc5iuTRXGEpdT-qjtw5u-7AMtCyMXeRGaf9-jdC894bZ1g"

  # Not an identifier at all: the name of the demo intake directory, which appears in
  # config/project.yml only as a `submissions.exclude` entry. It trips the bare-token
  # scan because the pattern asks for 25+ characters with an uppercase letter and a
  # digit, and a timestamped directory name has all three. It names a test submission
  # created here, refers to nothing in Google, and is quoted as an example in both
  # RUNBOOK.md and submission_id.py's docstring -- all three of which are published.
  # Added 2026-08-14, when it was blocking the publish outright.
  "20260806T210800_DEMO_Testsubmission"
)

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT

LOG_DIR="${ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/publish_pipeline_$(date +%Y%m%d_%H%M%S).log"

# Same reasoning as push_site.sh: an unrecognized argument must not fall through to a real
# publish. This one copies source code rather than a rendered site, so the cost of a
# mistaken live run is higher, not lower.
DRY_RUN=0
case "${1:-}" in
  "")         ;;
  --dry-run)  DRY_RUN=1 ;;
  *)          printf 'unknown argument: %s\nusage: push_pipeline.sh [--dry-run]\n' "$1" >&2
              exit 2 ;;
esac

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "${LOG_FILE}"; }

git_auth() {
  if [[ -f "${TOKEN_FILE}" ]]; then
    git -c "credential.helper=!f() { printf '%s\n' username=token password=\"\$(tr -d '[:space:]' < '${TOKEN_FILE}')\"; }; f" "$@"
  else
    git "$@"
  fi
}

log "== malavi_rebuild :: publish pipeline =="
log "source   : ${ROOT}"
log "manifest : ${MANIFEST}"
log "public   : ${PUBLIC_REPO_URL}"
log "dry run  : ${DRY_RUN}"

[[ -f "${MANIFEST}" ]] || { log "ERROR: no manifest at ${MANIFEST}"; exit 1; }

# ---- 1. build the staging tree from the manifest ----------------------------
log ""
log "-- copying the files the manifest names --"
copied=0
while IFS= read -r line; do
  # Strip comments and surrounding whitespace; skip blanks.
  line="${line%%#*}"
  line="$(printf '%s' "${line}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [[ -z "${line}" ]] && continue

  # "src -> dest" renames; otherwise destination equals source.
  if [[ "${line}" == *"->"* ]]; then
    src="$(printf '%s' "${line%%->*}" | sed 's/[[:space:]]*$//')"
    dest="$(printf '%s' "${line##*->}" | sed 's/^[[:space:]]*//')"
  else
    src="${line}"; dest="${line}"
  fi

  if [[ "${src}" == */ ]]; then
    if [[ ! -d "${ROOT}/${src}" ]]; then
      log "  ERROR: manifest names a directory that does not exist: ${src}"; exit 1
    fi
    mkdir -p "${STAGING}/${dest}"
    rsync -a "${ALWAYS_SKIP[@]}" "${ROOT}/${src}" "${STAGING}/${dest}"
  else
    if [[ ! -f "${ROOT}/${src}" ]]; then
      log "  ERROR: manifest names a file that does not exist: ${src}"; exit 1
    fi
    mkdir -p "${STAGING}/$(dirname "${dest}")"
    cp -p "${ROOT}/${src}" "${STAGING}/${dest}"
  fi
  copied=$((copied + 1))
done < "${MANIFEST}"
log "  ${copied} manifest entries copied"

# ---- 1b. warn about maintainer programs the manifest does not name -----------
# An allowlist fails safe -- a new file is absent from the public repo rather than
# published by accident -- but "absent" has a cost of its own here. RUNBOOK.md and
# BREAK_GLASS.md are both published, and BREAK_GLASS names the public repo as the
# recovery route, so a program the runbook tells you to run and the manifest does not
# carry leaves a successor with instructions for something that is not there. That is
# exactly what happened: six programs written in August 2026 were missing until a
# review found them.
#
# A WARNING, not an error. Deciding a program should stay private is legitimate; not
# noticing it is missing is what this catches. If the omission is deliberate, name the
# program in a comment line in the manifest, with the reason, like so:
#
#   # curation/some_program.py   private: <reason>
#
# and this stays quiet about it. The whole line must be a comment: a trailing
# "# private:" after an uncommented path would still copy the file. The check is a
# fixed-string, whole-word match anywhere in the line (a path is not a regular
# expression, and the name may sit after a "#"); until 2026-09-02 it was anchored at
# the start of the line, so the convention documented here never actually matched.
log ""
log "-- checking for maintainer programs the manifest does not name --"
unlisted=0
while IFS= read -r program; do
  if ! grep -qwF -- "${program}" "${MANIFEST}"; then
    log "  NOT PUBLISHED: ${program}"
    unlisted=$((unlisted + 1))
  fi
done < <(cd "${ROOT}" && find curation -maxdepth 1 -name '*.py' -type f | sort)
if [[ "${unlisted}" -gt 0 ]]; then
  log "  ${unlisted} program(s) above are not in the manifest. If the RUNBOOK tells"
  log "  anyone to run one of them, add it to publish/public_manifest.txt."
else
  log "  every curation/*.py is accounted for."
fi

# ---- 2. work out what must never appear -------------------------------------
# Read the live config and pull out everything that looks like a Google identifier or a
# deployment URL. Whatever is not on the PUBLIC_OK list is forbidden in the output.
log ""
log "-- reading the private config for values that must not be published --"
FORBIDDEN=()
while IFS= read -r value; do
  [[ -z "${value}" ]] && continue
  allowed=0
  for ok in "${PUBLIC_OK[@]}"; do
    [[ "${value}" == *"${ok}"* ]] && allowed=1 && break
  done
  [[ "${allowed}" -eq 0 ]] && FORBIDDEN+=("${value}")
done < <(
  {
    # Whole URLs, for the sanitizer to blank in project.example.yml.
    grep -oE 'https://script\.google\.com/macros/s/[A-Za-z0-9_-]+' "${PRIVATE_CONFIG}" || true
    grep -oE 'https://docs\.google\.com/forms/d/e?/?[A-Za-z0-9_-]{20,}' "${PRIVATE_CONFIG}" || true

    # And EVERY long identifier anywhere in the file, whatever surrounds it.
    #
    # This is the important one, and it exists because the narrower version above let two
    # values through on 2026-08-08. Both failures were the same mistake in different
    # clothes: matching the SHAPE a value happens to have in the config rather than the
    # value itself.
    #
    #   * the verdict form is a /forms/d/e/... URL, and the pattern above required the id
    #     to follow "d/" directly, so it matched nothing;
    #   * the submission form id appears in project.yml only inside a URL, but in the
    #     Apps Script it appears bare -- so a literal search for the URL could never
    #     find it.
    #
    # Extracting bare tokens catches an id however it is written on either side. 25
    # characters of [A-Za-z0-9_-] does not occur in prose, so this does not fire on
    # comments.
    # The trailing filters separate a Google identifier from a long snake_case config key.
    # `awaiting_submitter_timeout_days` is 31 characters and would otherwise be treated as
    # a secret, which would make this scanner cry wolf on ledger.py and half the tests --
    # and a guard that always fails is one somebody switches off. Google ids always carry
    # both an uppercase letter and a digit; snake_case keys carry neither.
    grep -oE '[A-Za-z0-9_-]{25,}' "${PRIVATE_CONFIG}" | grep -E '[A-Z]' | grep -E '[0-9]' \
      || true
  } | sort -u
)
log "  ${#FORBIDDEN[@]} sensitive value(s) identified"

# ---- 3. generate the sanitized config example -------------------------------
# Derived from the real file rather than maintained by hand, so a key added to the config
# appears in the example -- blank -- instead of being silently missing.
log ""
log "-- generating config/project.example.yml --"
mkdir -p "${STAGING}/config"
cp -p "${PRIVATE_CONFIG}" "${STAGING}/config/project.example.yml"
for value in "${FORBIDDEN[@]}"; do
  # On any line carrying a forbidden value, blank the WHOLE quoted value rather than
  # cutting the value out of it. Deleting just the id leaves debris like
  # form_edit: "/edit" or report_endpoint: "/exec", which reads like a real setting and
  # is neither a usable value nor an obviously empty one. '|' is the delimiter because
  # ids and URLs never contain it.
  sed -i "\|${value}|s|\"[^\"]*\"|\"\"|g" "${STAGING}/config/project.example.yml"
done

# Second pass: anything that survived was not a quoted value. The submissions.exclude list
# holds bare YAML values -- and they are intake directory names, which are built from the
# submitter's own name. Publishing those would name people who sent unpublished data, which
# is the exact thing the opaque queue ids exist to prevent. Found on 2026-08-08, when the
# scanner refused a publish over it.
for value in "${FORBIDDEN[@]}"; do
  sed -i "s|${value}||g" "${STAGING}/config/project.example.yml"
done
{
  echo ""
  echo "# ---------------------------------------------------------------------------"
  echo "# This file was generated from the maintainer's config/project.yml by"
  echo "# publish/push_pipeline.sh. Every Drive folder id, sheet id, form edit URL and"
  echo "# deployment URL has been blanked. Copy it to config/project.yml and fill in"
  echo "# your own values; RUNBOOK.md says where each one comes from."
  echo "# ---------------------------------------------------------------------------"
} >> "${STAGING}/config/project.example.yml"

# ---- 4. scan the finished tree ----------------------------------------------
# Everything above could be wrong. This is the check that does not depend on the manifest
# being right, and it runs over exactly the bytes that are about to be published.
log ""
log "-- scanning the staged tree --"
FOUND=0

for value in "${FORBIDDEN[@]}"; do
  while IFS= read -r hit; do
    log "  REFUSING: a value from the private config appears in ${hit#${STAGING}/}"
    FOUND=1
  done < <(grep -rlF "${value}" "${STAGING}" 2>/dev/null || true)
done

# Generic credential shapes, for anything the config does not know about.
#
# The service-account-key pattern is assembled from two pieces rather than written out,
# so that this scanner does not match its own source and refuse every publish. The other
# patterns are regexes whose literal text cannot match themselves, so they need no such
# treatment. (Found on the first dry run, which refused to publish because of this line.)
SERVICE_KEY_PATTERN='"pri''vate_key"'

for pattern in 'ghp_[A-Za-z0-9]{20,}' 'github_pat_[A-Za-z0-9_]{20,}' \
               'BEGIN [A-Z ]*PRIVATE KEY' "${SERVICE_KEY_PATTERN}" \
               'AKfycb[A-Za-z0-9_-]{20,}'; do
  while IFS= read -r hit; do
    log "  REFUSING: credential-shaped text (${pattern}) in ${hit#${STAGING}/}"
    FOUND=1
  done < <(grep -rlE "${pattern}" "${STAGING}" 2>/dev/null || true)
done

# Files that must never be present, whatever the manifest says.
for name in "CUSTODY_PRIVATE.md" "DELIVERY_PLAN.md" "project.yml" "curators.yml" \
            "*token*" "*secret*" "service-account*"; do
  while IFS= read -r hit; do
    log "  REFUSING: ${hit#${STAGING}/} must never be published"
    FOUND=1
  done < <(find "${STAGING}" -name "${name}" 2>/dev/null || true)
done

if [[ "${FOUND}" -eq 1 ]]; then
  log ""
  log "ERROR: the staged tree contains material that must not be public. Nothing was pushed."
  exit 1
fi
log "  clean."

log ""
log "-- what would be published --"
(cd "${STAGING}" && find . -type f | sed 's|^\./||' | sort | sed 's/^/    /') | tee -a "${LOG_FILE}"
log "  $(find "${STAGING}" -type f | wc -l) files, $(du -sh "${STAGING}" | cut -f1)"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log ""
  log "[dry-run] nothing was cloned, written or pushed."
  exit 0
fi

# ---- 5. sync into the public repository -------------------------------------
if [[ ! -d "${PUBLIC_REPO_DIR}/.git" ]]; then
  log ""
  log "cloning ${PUBLIC_REPO_URL} -> ${PUBLIC_REPO_DIR}"
  git_auth clone "${PUBLIC_REPO_URL}" "${PUBLIC_REPO_DIR}" 2>&1 | tee -a "${LOG_FILE}" || true
  if [[ ! -d "${PUBLIC_REPO_DIR}/.git" ]]; then
    log "ERROR: clone failed. Does the repository exist and does the token have access?"
    exit 1
  fi
else
  log ""
  log "refreshing existing clone"
  git_auth -C "${PUBLIC_REPO_DIR}" fetch --quiet origin || true
  git -C "${PUBLIC_REPO_DIR}" checkout --quiet -B main
  git -C "${PUBLIC_REPO_DIR}" reset --hard --quiet origin/main 2>/dev/null || true
fi

log ""
log "-- syncing into the public repository --"
rsync -a --delete --exclude ".git/" "${STAGING}/" "${PUBLIC_REPO_DIR}/"

cd "${PUBLIC_REPO_DIR}"
git add -A
if git diff --cached --quiet; then
  log "the public repository already matches; nothing to commit."
  exit 0
fi

SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse --short HEAD)"
git commit -q -m "Sync the pipeline from malavi_rebuild ${SOURCE_COMMIT}

Built by publish/push_pipeline.sh from publish/public_manifest.txt.
This repository has its own history: it is not a fork or a filtered
copy of the private one, and no private commit is an ancestor of any
commit here."

log "committed."
git_auth push -q origin main
log ""
log "published. Source commit: ${SOURCE_COMMIT}"
log "  https://github.com/malavi-db/malavi-pipeline"
