#!/usr/bin/env Rscript
# =============================================================================
# build_datatables_json.R
# -----------------------------------------------------------------------------
# Convert the exported CSV tables (from export_tables.R) into the JSON the
# website's DataTables UI loads. For each table we emit:
#   docs/assets/data/<slug>.json  -> { "columns": [...], "data": [[...], ...] }
# DataTables consumes this array-of-arrays form efficiently (deferRender) even
# for the ~18k-row hosts table.
#
# Usage:
#   Rscript export/build_datatables_json.R [--dry-run]
#
# Phase 1 status: dry-run reports the plan; the conversion body is stubbed and
# wired out once export_tables.R produces real CSVs.
# =============================================================================

suppressMessages({
  if (!requireNamespace("yaml", quietly = TRUE))
    stop("The 'yaml' package is required. install.packages('yaml').")
})

args <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", args[grep("^--file=", args)])
script_dir <- if (length(file_arg)) dirname(normalizePath(file_arg)) else getwd()
repo_root <- normalizePath(file.path(script_dir, ".."))
cfg <- yaml::read_yaml(file.path(repo_root, "config", "project.yml"))

trailing <- commandArgs(trailingOnly = TRUE)
dry_run  <- "--dry-run" %in% trailing

release <- cfg$malaviR$release
data_dir <- file.path(repo_root, cfg$paths$data_dir, release)
json_dir <- file.path(repo_root, cfg$paths$docs_data_dir)

cat("== malavi_rebuild :: build_datatables_json ==\n")
cat("reads CSVs from :", data_dir, "\n")
cat("writes JSON to  :", json_dir, "\n\n")
for (slug in names(cfg$tables)) {
  cat(sprintf("  %s.csv  ->  %s.json\n", slug, slug))
}

if (dry_run) {
  cat("\n[--dry-run] No files written. Exiting.\n")
  quit(status = 0)
}

# ---- TODO (later phase): real conversion ------------------------------------
# requireNamespace("jsonlite")
# dir.create(json_dir, recursive = TRUE, showWarnings = FALSE)
# for (slug in names(cfg$tables)) {
#   df <- read.csv(file.path(data_dir, paste0(slug, ".csv")),
#                  check.names = FALSE, colClasses = "character", na.strings = "")
#   payload <- list(columns = colnames(df),
#                   data = unname(as.matrix(df)))
#   jsonlite::write_json(payload, file.path(json_dir, paste0(slug, ".json")),
#                        na = "null", auto_unbox = TRUE)
# }
stop("JSON build is not implemented yet (Phase 1 scaffold). Run with --dry-run.")
