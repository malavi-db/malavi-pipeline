#!/usr/bin/env bash
#SBATCH --job-name=malavi_fwd_watch
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=01:00:00
#SBATCH --output=watcher/slurm_logs/forward_watcher_%j.out
#SBATCH --error=watcher/slurm_logs/forward_watcher_%j.err
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=vaellis@udel.edu
#
# @title Forward literature watcher (recurring) — papers since last MalAvi release
# @purpose Query the three literature databases (Europe PMC + OpenAlex + Crossref)
#   for avian-haemosporidian papers published SINCE the pinned MalAvi release, merge
#   and de-duplicate them, drop anything already seen, and emit a curator worklist.
# @why This is the "forward look": catch new papers MalAvi has not yet ingested. Run
#   periodically by hand (until it is coded into GitHub Actions); watcher/seen.json
#   accumulates so each run only surfaces genuinely new hits.
# @input config/project.yml (watcher.sources, queries, malaviR.release)
# @input watcher/seen.json (de-dup ledger, updated in place)
# @output watcher/runs/forward_<UTC>/batch.json (+ provenance.txt, worklist)
# @program python3
# @program watcher/scan_publications.py
# @critical-var RELEASE_DATE
# @critical-var MAX_ITEMS
# @critical-flag scan_publications.py "" --lookback-days
# @critical-flag scan_publications.py "" --max-items
set -eEuo pipefail

# --- Centralized paths and knobs (edit here, not in the body) -----------------
# Resolve the repository root robustly. Slurm runs a *copy* of this script from its
# spool directory, so BASH_SOURCE/$0 are unreliable under sbatch. Prefer
# SLURM_SUBMIT_DIR (the directory sbatch was launched from) and ask git for the
# repository top level; fall back to this script's own location for a direct
# `bash forward_watcher.sbatch` run. Submit from the repository root.
if [[ -n "${SLURM_SUBMIT_DIR:-}" ]]; then
  REPO_ROOT="$(git -C "${SLURM_SUBMIT_DIR}" rev-parse --show-toplevel)"
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # watcher/slurm
  REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"               # -> repository root
fi
WATCHER_DIR="${REPO_ROOT}/watcher"
CONFIG="${REPO_ROOT}/config/project.yml"
PYTHON="/usr/bin/python3"                      # system python3: has the scan's deps
SCAN="${WATCHER_DIR}/scan_publications.py"

# A large cap so the deterministic title-descending sort never silently drops
# early-alphabet papers on a wide catch-up window. Override by exporting MAX_ITEMS.
MAX_ITEMS="${MAX_ITEMS:-1000}"

# Working directories: logs for Slurm stdout/stderr, runs/ for dated records.
LOG_DIR="${WATCHER_DIR}/slurm_logs"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${WATCHER_DIR}/runs/forward_${RUN_STAMP}"
mkdir -p "${LOG_DIR}" "${RUN_DIR}"

# --- Derive the lookback window from the pinned MalAvi release -----------------
# The forward look always spans "release date -> today" so nothing between the
# release and now can slip through; seen.json handles the overlap across runs.
# RELEASE_DATE is read from config so bumping malaviR.release moves the window too.
RELEASE_DATE="$(grep -E '^[[:space:]]*release:' "${CONFIG}" | head -1 \
  | sed -E 's/.*release:[[:space:]]*"?([0-9]{4}-[0-9]{2}-[0-9]{2})"?.*/\1/')"
if [[ ! "${RELEASE_DATE}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "ERROR: could not parse malaviR.release from ${CONFIG} (got '${RELEASE_DATE}')" >&2
  exit 1
fi
# Whole days between the release date and now (UTC). Allow LOOKBACK_DAYS override.
LOOKBACK_DAYS="${LOOKBACK_DAYS:-$(( ( $(date -u +%s) - $(date -u -d "${RELEASE_DATE}" +%s) ) / 86400 ))}"

# --- Provenance record: exactly what ran, with versions -----------------------
PROV="${RUN_DIR}/provenance.txt"
{
  echo "run_utc         : ${RUN_STAMP}"
  echo "host            : $(hostname)"
  echo "slurm_job_id    : ${SLURM_JOB_ID:-<interactive>}"
  echo "repo_root       : ${REPO_ROOT}"
  echo "git_commit      : $(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo n/a)"
  echo "git_dirty       : $(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null | wc -l) changed file(s)"
  echo "python          : $(${PYTHON} --version 2>&1)"
  echo "release_date    : ${RELEASE_DATE}"
  echo "lookback_days   : ${LOOKBACK_DAYS}"
  echo "max_items       : ${MAX_ITEMS}"
  echo "scan_argv       : ${SCAN} --lookback-days ${LOOKBACK_DAYS} --max-items ${MAX_ITEMS} --out ${RUN_DIR}/batch.json"
} | tee "${PROV}"

# --- Run the scan -------------------------------------------------------------
# No --seen / --no-save-seen: the canonical watcher/seen.json is read and updated
# in place so future runs de-dup against this one (the recurring-job contract).
echo "[forward_watcher] scanning ${RELEASE_DATE} -> today (${LOOKBACK_DAYS} days) ..."
"${PYTHON}" "${SCAN}" \
  --lookback-days "${LOOKBACK_DAYS}" \
  --max-items "${MAX_ITEMS}" \
  --out "${RUN_DIR}/batch.json"

echo "[forward_watcher] done. Batch + provenance in: ${RUN_DIR}"
