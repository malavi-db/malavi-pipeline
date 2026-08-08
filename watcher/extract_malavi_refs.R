#!/usr/bin/env Rscript
# @title Export MalAvi reference titles for the publication watcher
# @purpose Dump the MalAvi "Table of References" to a DataTables-style JSON so the
#   Python watcher can drop candidate papers already in MalAvi (matched on title).
# @why The watcher surfaces papers NOT yet in MalAvi; MalAvi has no DOI column, so
#   the reference TITLE list is the only "already in MalAvi" signal available now.
# @input config/project.yml (malaviR.source_path + malaviR.release)
# @input <source_path>/inst/extdata/malavi_db_<release>.rds
# @output data/malavi_references.json  (or the path given as the first argument)
# @program Rscript
# @program yaml
# @program jsonlite
# @critical-var MALAVIR_RELEASE
# @critical-var OUT_PATH

# --- Locate the repo root (this script lives in <repo>/watcher/) --------------
# Resolve the directory of this script so paths work regardless of the caller's cwd.
args_all <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", args_all[grep("^--file=", args_all)])
script_dir <- if (length(file_arg)) dirname(normalizePath(file_arg)) else getwd()
repo_root <- normalizePath(file.path(script_dir, ".."))

# --- Read the shared project config (single source of truth) ------------------
suppressPackageStartupMessages(library(yaml))
cfg <- yaml::read_yaml(file.path(repo_root, "config", "project.yml"))
malavir_source <- cfg$malaviR$source_path       # local malaviR checkout on BIOMIX
MALAVIR_RELEASE <- cfg$malaviR$release           # pinned bundled release date

# --- Command-line output path (defaults to data/malavi_references.json) -------
cli <- commandArgs(trailingOnly = TRUE)
OUT_PATH <- if (length(cli) >= 1) cli[[1]] else file.path(repo_root, "data", "malavi_references.json")

# --- Load the bundled MalAvi database and pull the references table -----------
# malaviR ships each release as inst/extdata/malavi_db_<date>.rds (a named list of
# the five tables + the alignment). We read the file directly to avoid needing the
# package installed on this machine.
db_file <- file.path(malavir_source, "inst", "extdata",
                     paste0("malavi_db_", MALAVIR_RELEASE, ".rds"))
if (!file.exists(db_file)) {
  stop("MalAvi DB file not found: ", db_file, call. = FALSE)
}
db <- readRDS(db_file)
refs <- db[["references"]]

# --- Write DataTables-style JSON: {"columns": [...], "data": [[...]]} ---------
# This matches the shape the Python side already reads (load_malavi_titles), and the
# same shape export/build_datatables_json.R produces for the website tables.
suppressPackageStartupMessages(library(jsonlite))
payload <- list(
  columns = as.list(colnames(refs)),
  # unname() so each row serializes as a JSON array (not an object keyed by column).
  data = lapply(seq_len(nrow(refs)), function(i) unname(as.list(refs[i, ])))
)
dir.create(dirname(OUT_PATH), showWarnings = FALSE, recursive = TRUE)
write(jsonlite::toJSON(payload, auto_unbox = TRUE, null = "null", na = "null",
                       pretty = FALSE), file = OUT_PATH)

cat("Wrote", nrow(refs), "MalAvi references to", OUT_PATH, "\n")
