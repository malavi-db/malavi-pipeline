#!/usr/bin/env bash
#SBATCH --job-name=malavi_bwd_gap
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --output=watcher/slurm_logs/backward_gapfinder_%j.out
#SBATCH --error=watcher/slurm_logs/backward_gapfinder_%j.err
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=vaellis@udel.edu
#
# @title Backward lineage gap-finder (one-time GenBank-vs-MalAvi novelty sweep)
# @purpose Build a BLAST database of every MalAvi lineage sequence, pull haemosporidian
#   cytochrome-b records from GenBank, BLAST them against MalAvi, and list the ones
#   with no 100%-identity hit (novel lineages MalAvi lacks) mapped to their papers.
# @why The "backward look": find sequences already public on GenBank that never made
#   it into MalAvi. Sequence novelty (not keywords) is what distinguishes a genuinely
#   missed lineage from a paper with nothing new. Run once to close historical gaps.
# @input config/project.yml (malaviR.source_path + malaviR.release)
# @input NCBI nucleotide (Entrez) haemosporidian cytb records
# @output watcher/runs/backward_<UTC>/novel_candidates.tsv (+ provenance.txt)
# @program Rscript
# @program makeblastdb
# @program blastn
# @program python3
# @program watcher/lineage_gap_finder.py
# @critical-var MINDATE
# @critical-var MAXDATE
# @critical-var MAX_RECORDS
# @critical-flag blastn "" -perc_identity
set -eEuo pipefail

# --- Centralized paths and knobs ---------------------------------------------
# Resolve the repository root robustly: Slurm runs a *copy* of this script from its
# spool dir, so BASH_SOURCE/$0 are unreliable under sbatch. Prefer SLURM_SUBMIT_DIR
# + git top level; fall back to this script's location for a direct `bash` run.
# Submit from the repository root.
if [[ -n "${SLURM_SUBMIT_DIR:-}" ]]; then
  REPO_ROOT="$(git -C "${SLURM_SUBMIT_DIR}" rev-parse --show-toplevel)"
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # watcher/slurm
  REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"               # -> repository root
fi
WATCHER_DIR="${REPO_ROOT}/watcher"
PYTHON="/usr/bin/python3"                                    # has biopython (Bio)
GAP_FINDER="${WATCHER_DIR}/lineage_gap_finder.py"
LINEAGE_EXTRACTOR="${WATCHER_DIR}/extract_malavi_lineages.R"

# GenBank pub-date window to sweep and per-genus record cap. Override by exporting.
MINDATE="${MINDATE:-2020/01/01}"
MAXDATE="${MAXDATE:-$(date -u +%Y/%m/%d)}"
MAX_RECORDS="${MAX_RECORDS:-400}"

# BLAST database of "what MalAvi already knows" (built once, reused on re-runs).
BLAST_DIR="${WATCHER_DIR}/blastdb"
LINEAGE_FASTA="${BLAST_DIR}/malavi_lineages.fasta"
BLAST_DB="${BLAST_DIR}/malavi_lineages"      # makeblastdb prefix

# Working directories.
LOG_DIR="${WATCHER_DIR}/slurm_logs"
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${WATCHER_DIR}/runs/backward_${RUN_STAMP}"
CACHE_DIR="${WATCHER_DIR}/cache/genbank"     # fetched FASTA/GenBank, cached across runs
mkdir -p "${LOG_DIR}" "${RUN_DIR}" "${BLAST_DIR}" "${CACHE_DIR}"

# --- Provenance record --------------------------------------------------------
PROV="${RUN_DIR}/provenance.txt"
{
  echo "run_utc         : ${RUN_STAMP}"
  echo "host            : $(hostname)"
  echo "slurm_job_id    : ${SLURM_JOB_ID:-<interactive>}"
  echo "repo_root       : ${REPO_ROOT}"
  echo "git_commit      : $(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo n/a)"
  echo "python          : $(${PYTHON} --version 2>&1)"
  echo "Rscript         : $(Rscript --version 2>&1 | head -1)"
  echo "blastn          : $(blastn -version 2>&1 | head -1)"
  echo "mindate         : ${MINDATE}"
  echo "maxdate         : ${MAXDATE}"
  echo "max_records     : ${MAX_RECORDS}"
  echo "blast_db        : ${BLAST_DB}"
} | tee "${PROV}"

# --- 1. Build the MalAvi lineage BLAST database (only if missing) --------------
# extract_malavi_lineages.R dumps every MalAvi lineage's cytb sequence to FASTA;
# makeblastdb turns that into a nucleotide BLAST db. Skip if the db already exists.
if [[ ! -f "${BLAST_DB}.nsq" ]]; then
  echo "[backward_gapfinder] building MalAvi lineage BLAST db ..."
  Rscript "${LINEAGE_EXTRACTOR}" "${LINEAGE_FASTA}"
  makeblastdb -in "${LINEAGE_FASTA}" -dbtype nucl -out "${BLAST_DB}"
else
  echo "[backward_gapfinder] reusing existing BLAST db at ${BLAST_DB}"
fi

# --- 2. Sweep GenBank, BLAST vs MalAvi, map novel accessions to papers ---------
echo "[backward_gapfinder] sweeping GenBank ${MINDATE} -> ${MAXDATE} ..."
"${PYTHON}" "${GAP_FINDER}" \
  --db "${BLAST_DB}" \
  --mindate "${MINDATE}" \
  --maxdate "${MAXDATE}" \
  --max-records "${MAX_RECORDS}" \
  --cache-dir "${CACHE_DIR}" \
  --out "${RUN_DIR}/novel_candidates.tsv"

echo "[backward_gapfinder] done. Novel-candidate TSV + provenance in: ${RUN_DIR}"
