#!/usr/bin/env bash
# @title Copy the irreplaceable parts of MalAvi to permanent home storage
# @purpose Write a small, self-contained "lifeboat" into $HOME holding the full
#          git history of the MalAvi repositories, the credentials, and the
#          working state that exists nowhere else, so the project survives the
#          loss of /mnt/ellisbiostore.
# @why The project lives on /mnt/ellisbiostore, which is a temporary resource
#      that may be withdrawn. /home/vaellis is permanent but small, so a full
#      copy is neither possible nor useful. This copies only what cannot be
#      recreated, and records how to recreate everything else.
# @input /mnt/ellisbiostore/malavi_rebuild
# @input /mnt/ellisbiostore/malaviR
# @output $HOME/malavi_lifeboat
# @program git
# @program rsync
# @critical-var LIFEBOAT_DIR
# @critical-var PROJECT_DIR
# @critical-var MALAVIR_DIR
# @critical-var MAX_STATE_FILE_SIZE
# @critical-flag git bundle --all
# =============================================================================
# Usage:
#   backup/lifeboat.sh              # refresh the lifeboat
#   backup/lifeboat.sh --with-papers  # also copy Papers.zip (359 MB)
#
# Safe to re-run: it overwrites the previous lifeboat in place. Run it after
# any session that changed the curation state, and after every git commit.
# =============================================================================
set -eEuo pipefail

# ---- configuration ----------------------------------------------------------
# Everything the lifeboat needs to know about where things live is here.

# The permanent destination. /home is backed by the university and is not tied
# to the biostore mount, which is the whole point of this script.
LIFEBOAT_DIR="${LIFEBOAT_DIR:-${HOME}/malavi_lifeboat}"

# The two repositories that make up the project. malaviR is separate because it
# is an R package with its own public GitHub remote and its own history.
PROJECT_DIR="${PROJECT_DIR:-/mnt/ellisbiostore/malavi_rebuild}"
MALAVIR_DIR="${MALAVIR_DIR:-/mnt/ellisbiostore/malaviR}"

# State files above this size are assumed to be PDFs, spreadsheets of raw
# downloads, or other re-fetchable bulk, and are skipped. The state worth
# keeping -- ledgers, extracted records, JSON queues -- is all small.
MAX_STATE_FILE_SIZE="${MAX_STATE_FILE_SIZE:-2m}"

# Papers.zip is 359 MB of source PDFs. It is skipped by default because most of
# it can be downloaded again, but it is offered as a flag because some of it
# came from Staffan rather than from a publisher.
WITH_PAPERS=0
[[ "${1:-}" == "--with-papers" ]] && WITH_PAPERS=1

LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/lifeboat_$(date +%Y%m%d_%H%M%S).log"

log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "${LOG_FILE}"; }

log "== MalAvi lifeboat =="
log "source  : ${PROJECT_DIR}"
log "destination: ${LIFEBOAT_DIR}"

# ---- validate the sources ---------------------------------------------------
# Fail early and loudly rather than writing a lifeboat that is quietly partial.
for dir in "${PROJECT_DIR}" "${MALAVIR_DIR}"; do
  [[ -d "${dir}/.git" ]] || { log "ERROR: ${dir} is not a git repository."; exit 1; }
done

# ---- warn about work that a bundle cannot capture ---------------------------
# `git bundle` writes committed history only. Uncommitted edits in the working
# tree would NOT be in the lifeboat, and a restore would silently lose them.
# This is a warning rather than an error: a partial lifeboat still beats none.
UNCOMMITTED=0
for dir in "${PROJECT_DIR}" "${MALAVIR_DIR}"; do
  if [[ -n "$(git -C "${dir}" status --porcelain)" ]]; then
    log "WARNING: ${dir} has uncommitted changes -- they will NOT be in the bundle."
    log "         Commit them first if they matter."
    UNCOMMITTED=1
  fi
done

# ---- lay out the lifeboat ---------------------------------------------------
mkdir -p "${LIFEBOAT_DIR}"/{repos,secrets,state}
chmod 700 "${LIFEBOAT_DIR}/secrets"

# ---- 1. the repositories, as bundles ----------------------------------------
# A bundle is a single file holding the complete history. `git clone` treats it
# like a remote, so a restore needs no network, no GitHub account and no
# credentials -- which matters because losing the GitHub account is one of the
# scenarios this protects against.
log ""
log "-- bundling repositories --"
git -C "${PROJECT_DIR}" bundle create "${LIFEBOAT_DIR}/repos/malavi_rebuild.bundle" --all 2>&1 \
  | sed 's/^/    /' | tee -a "${LOG_FILE}"
git -C "${MALAVIR_DIR}" bundle create "${LIFEBOAT_DIR}/repos/malaviR.bundle" --all 2>&1 \
  | sed 's/^/    /' | tee -a "${LOG_FILE}"

# ---- 2. the credentials -----------------------------------------------------
# Copied with -p so the 600 permission survives. These are the files that make
# the difference between "we have the code" and "we can publish the site".
log ""
log "-- credentials --"
for secret in "${PROJECT_DIR}/malavi_site_github_token.txt"; do
  if [[ -f "${secret}" ]]; then
    cp -p "${secret}" "${LIFEBOAT_DIR}/secrets/"
    log "    $(basename "${secret}")"
  else
    log "    MISSING: ${secret}"
  fi
done
chmod -R go-rwx "${LIFEBOAT_DIR}/secrets"

# ---- 3. the state that is not in git ----------------------------------------
# Everything here is gitignored, so none of it is on GitHub. PDFs are excluded
# because they can be downloaded again; the metadata around them cannot.
log ""
log "-- state not held in git --"

# The built release: the ZIP a curator would ship. Regenerable from
# data/records/ (which IS in git), but small enough to keep ready-made.
if [[ -d "${PROJECT_DIR}/data/releases" ]]; then
  rsync -a --delete "${PROJECT_DIR}/data/releases/" "${LIFEBOAT_DIR}/state/data_releases/"
  log "    data/releases        $(du -sh "${LIFEBOAT_DIR}/state/data_releases" | cut -f1)"
fi

# The curation intake tree: submissions, verdicts, the processed/processing
# ledger. The PDFs inside it are re-fetchable; the record of what was done to
# them is not.
if [[ -d "${PROJECT_DIR}/curation/intake" ]]; then
  rsync -a --delete \
    --exclude '*.pdf' --exclude '__pycache__/' --exclude '*.pyc' \
    --max-size="${MAX_STATE_FILE_SIZE}" \
    "${PROJECT_DIR}/curation/intake/" "${LIFEBOAT_DIR}/state/curation_intake/"
  log "    curation/intake      $(du -sh "${LIFEBOAT_DIR}/state/curation_intake" | cut -f1) (PDFs excluded)"
fi

# The watcher's memory of what it has already reported. Losing this makes the
# watcher re-report every paper it has ever seen.
if [[ -d "${PROJECT_DIR}/watcher/cache" ]]; then
  rsync -a --delete --max-size="${MAX_STATE_FILE_SIZE}" \
    "${PROJECT_DIR}/watcher/cache/" "${LIFEBOAT_DIR}/state/watcher_cache/"
  log "    watcher/cache        $(du -sh "${LIFEBOAT_DIR}/state/watcher_cache" | cut -f1)"
fi

# malaviR's DATA_ISSUES.md is deliberately gitignored there, because malaviR has
# a PUBLIC remote and the file is internal. That makes this the only copy.
if [[ -f "${MALAVIR_DIR}/DATA_ISSUES.md" ]]; then
  cp -p "${MALAVIR_DIR}/DATA_ISSUES.md" "${LIFEBOAT_DIR}/state/malaviR_DATA_ISSUES.md"
  log "    malaviR DATA_ISSUES.md"
fi

# malaviR design notes and prototypes that are untracked there, and so exist in
# exactly one place. They are untracked rather than gitignored, which means they
# are one `git add -A` away from being published: malaviR's remote is public and
# these are unfinished proposals. Copying them here keeps them safe without
# forcing that decision. If they are ever committed to malaviR, or moved into
# this repository, these two lines can go.
mkdir -p "${LIFEBOAT_DIR}/state/malaviR_untracked"
for item in "malavi_lin_qc_notes.txt" "docs/proposals"; do
  if [[ -e "${MALAVIR_DIR}/${item}" ]]; then
    rsync -a "${MALAVIR_DIR}/${item}" "${LIFEBOAT_DIR}/state/malaviR_untracked/"
    log "    malaviR untracked: ${item}"
  fi
done

# Papers.zip, only when asked for.
if [[ "${WITH_PAPERS}" -eq 1 && -f "${PROJECT_DIR}/Papers.zip" ]]; then
  cp -p "${PROJECT_DIR}/Papers.zip" "${LIFEBOAT_DIR}/state/"
  log "    Papers.zip           $(du -sh "${LIFEBOAT_DIR}/state/Papers.zip" | cut -f1)"
fi

# ---- 4. the script itself ---------------------------------------------------
# So the lifeboat can be refreshed from inside itself, before the repository it
# came from has been restored.
cp -p "${BASH_SOURCE[0]}" "${LIFEBOAT_DIR}/lifeboat.sh"

# ---- 5. the manifest --------------------------------------------------------
# What is here, how big, and which commit it came from. Checksums so a restore
# can prove the bundles are intact rather than truncated.
log ""
log "-- manifest --"
{
  echo "MalAvi lifeboat"
  echo "written: $(date --iso-8601=seconds)"
  echo "host   : $(hostname)"
  echo "source : ${PROJECT_DIR}"
  echo ""
  echo "repository HEADs at the time of writing:"
  echo "  malavi_rebuild  $(git -C "${PROJECT_DIR}" log -1 --format='%H %ad %s' --date=short)"
  echo "  malaviR         $(git -C "${MALAVIR_DIR}" log -1 --format='%H %ad %s' --date=short)"
  echo ""
  if [[ "${UNCOMMITTED}" -eq 1 ]]; then
    echo "WARNING: at least one repository had uncommitted changes when this was"
    echo "written, so the bundles are not a complete picture of the working tree."
    echo ""
  fi
  echo "checksums:"
  (cd "${LIFEBOAT_DIR}" && find repos state -type f -exec sha256sum {} \; | sort -k2)
  echo ""
  echo "sizes:"
  (cd "${LIFEBOAT_DIR}" && du -sh repos secrets state 2>/dev/null)
  echo "  total: $(du -sh "${LIFEBOAT_DIR}" | cut -f1)"
} > "${LIFEBOAT_DIR}/MANIFEST.txt"

# ---- 6. the restore instructions --------------------------------------------
# Written by the script rather than kept as a static file, so it cannot drift
# out of step with what the script actually copies.
cat > "${LIFEBOAT_DIR}/RESTORE.md" <<'RESTORE'
# Restoring MalAvi from the lifeboat

This directory is a minimal, permanent copy of the MalAvi rebuild project. It
exists because the working copy lives on `/mnt/ellisbiostore`, which is a
temporary resource, while `/home` is not.

It is deliberately **not** a full backup. It holds what cannot be recreated.
Everything else is listed at the bottom, with how to get it back.

## What is here

| Path | What it is |
|---|---|
| `repos/malavi_rebuild.bundle` | Complete git history of the private project repo |
| `repos/malaviR.bundle` | Complete git history of the malaviR R package |
| `secrets/` | The GitHub token that publishes the site. Mode 600. |
| `state/data_releases/` | The most recently built MalAvi release |
| `state/curation_intake/` | Submissions, verdicts and the processing ledger (PDFs excluded) |
| `state/watcher_cache/` | What the literature watcher has already reported |
| `state/malaviR_DATA_ISSUES.md` | Internal notes, gitignored in malaviR because that repo is public |
| `MANIFEST.txt` | Checksums, sizes, and which commit each bundle ends at |
| `lifeboat.sh` | The script that wrote all of this |

## Restoring

A bundle behaves like a git remote, so no network and no GitHub account is
needed:

```bash
mkdir -p ~/malavi_restored && cd ~/malavi_restored
git clone ~/malavi_lifeboat/repos/malavi_rebuild.bundle malavi_rebuild
git clone ~/malavi_lifeboat/repos/malaviR.bundle malaviR
```

Then put back the parts that git does not carry:

```bash
cd malavi_rebuild
cp ~/malavi_lifeboat/secrets/malavi_site_github_token.txt .
chmod 600 malavi_site_github_token.txt
mkdir -p data/releases curation/intake watcher/cache
cp -r ~/malavi_lifeboat/state/data_releases/*     data/releases/
cp -r ~/malavi_lifeboat/state/curation_intake/*   curation/intake/
cp -r ~/malavi_lifeboat/state/watcher_cache/*     watcher/cache/
```

Point the clone back at GitHub so it can push again:

```bash
git remote add origin https://github.com/vincenzoaellis/malavi_rebuild.git
```

Verify the bundles were not truncated:

```bash
cd ~/malavi_lifeboat && sha256sum -c <(grep -A999 '^checksums:' MANIFEST.txt | tail -n +2 | grep '^[0-9a-f]')
```

## What is NOT here, and how to get it back

| Not copied | Why | How to recover |
|---|---|---|
| `.venv/` (383 MB) | Rebuildable | Recreate the virtualenv and reinstall from the project's requirements |
| `Papers.zip` (359 MB) | Bulk source PDFs | Re-download, or re-run `backup/lifeboat.sh --with-papers` while the biostore copy still exists |
| Benchmark and intake PDFs (~220 MB) | Re-fetchable from publishers | The extraction results and annotations that matter are in git |
| `__pycache__/`, logs | Generated | Regenerated on next run |
| The published site | Lives in its own public repo | `git clone https://github.com/malavi-db/malavi-db.github.io.git` |

## Keeping it current

Re-run after any session that changes curation state, and after committing:

```bash
/mnt/ellisbiostore/malavi_rebuild/backup/lifeboat.sh
```

It overwrites in place and warns if either repository has uncommitted changes,
because `git bundle` captures committed history only.
RESTORE

log ""
log "-- done --"
log "total: $(du -sh "${LIFEBOAT_DIR}" | cut -f1) in ${LIFEBOAT_DIR}"
if [[ "${UNCOMMITTED}" -eq 1 ]]; then
  log "NOTE: uncommitted changes existed; the bundles do not include them."
fi
