#!/usr/bin/env Rscript
# =============================================================================
# export_tables.R
# -----------------------------------------------------------------------------
# Pull the canonical MalAvi tables + cytochrome b alignment from the malaviR
# package (the data source of truth) and write website-ready download files into
# data/<release>/. This is a THIN SHIM over malaviR -- it does not transform or
# re-derive the data, only serializes it for distribution.
#
# Usage:
#   Rscript export/export_tables.R            # full export
#   Rscript export/export_tables.R --dry-run  # load malaviR, report plan, write nothing
#
# Phase 1 status: the --dry-run path is fully implemented (loads malaviR, prints
# the pinned release and the tables it WOULD write). The actual file-writing
# block is stubbed and wired out in a later phase so we don't generate large
# artifacts before the website is ready to consume them.
# =============================================================================

suppressMessages({
  library(malaviR)
})

# ---- locate repo root + load central config ---------------------------------
# This script lives in export/; the repo root is its parent directory.
args <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", args[grep("^--file=", args)])
script_dir <- if (length(file_arg)) dirname(normalizePath(file_arg)) else getwd()
repo_root <- normalizePath(file.path(script_dir, ".."))

config_path <- file.path(repo_root, "config", "project.yml")
if (!requireNamespace("yaml", quietly = TRUE)) {
  stop("The 'yaml' package is required to read config/project.yml. Install with install.packages('yaml').")
}
cfg <- yaml::read_yaml(config_path)

# ---- parse flags ------------------------------------------------------------
trailing <- commandArgs(trailingOnly = TRUE)
dry_run  <- "--dry-run" %in% trailing

# ---- resolve the pinned release --------------------------------------------
# config pins a release; "latest" defers to whatever malaviR bundles newest.
release <- cfg$malaviR$release
bundled <- malavi_version()  # the version malaviR currently ships
if (identical(release, "latest")) release <- bundled

cat("== malavi_rebuild :: export_tables ==\n")
cat("repo root        :", repo_root, "\n")
cat("malaviR bundled  :", bundled, "\n")
cat("config-pinned    :", cfg$malaviR$release, "->", release, "\n")
if (!identical(release, bundled)) {
  cat("NOTE: pinned release differs from the bundled malaviR release.\n")
  cat("      Bump config/project.yml or install the matching malaviR.\n")
}

# The five tables to export, keyed by the snake_case slug used in filenames.
tables <- cfg$tables
out_dir <- file.path(repo_root, cfg$paths$data_dir, release)

cat("\nTables to export ->", out_dir, "\n")
for (slug in names(tables)) {
  cat(sprintf("  %-24s  (%s)\n", paste0(slug, ".csv"), tables[[slug]]$extract_key))
}
cat("  alignment.fasta           (", cfg$alignment$length_bp, "bp cytochrome b )\n")

if (dry_run) {
  cat("\n[--dry-run] No files written. Exiting.\n")
  quit(status = 0)
}

# ---- TODO (later phase): real export ----------------------------------------
# When wiring this up:
#   dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)
#   for (slug in names(tables)) {
#     df <- extract_table(tables[[slug]]$extract_key, version = release)
#     write.csv(df, file.path(out_dir, paste0(slug, ".csv")), row.names = FALSE, na = "")
#   }
#   aln <- extract_alignment(version = release)
#   ape::write.FASTA(aln, file.path(out_dir, "alignment.fasta"))
#   # then write MANIFEST.json (release, per-file row counts, checksums) and
#   # repoint data/latest -> out_dir.
stop("Full export is not implemented yet (Phase 1 scaffold). Run with --dry-run.")
