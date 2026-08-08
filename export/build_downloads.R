# @title Build every downloadable file the website offers
# @purpose Write each MalAvi table as CSV and XLSX, write the cytochrome b
#          alignment as FASTA, and bundle all of them into one ZIP archive.
# @why The site advertises per-table CSV/Excel downloads, an alignment FASTA and
#      an "Everything" archive. These must come from the same pinned release as
#      every number on the page, and must be regenerable byte-for-byte so a file
#      served today can be reproduced from the release alone.
# @input /mnt/ellisbiostore/malaviR (bundled release, via malaviR)
# @input config/project.yml
# @output docs/assets/downloads/tables/<table_id>_<release>.csv
# @output docs/assets/downloads/tables/<table_id>_<release>.xlsx
# @output docs/assets/downloads/malavi_alignment_<release>.fasta
# @output docs/assets/downloads/malavi_<release>.zip
# @program Rscript
# @program malaviR
# @program writexl
# @program ape
# @program zip
# @critical-var release
# @critical-var ZIP_MTIME
# =============================================================================
# Determinism
# -----------
# Re-running this script on the same release must produce identical files.
# Two things would otherwise leak the wall clock into the output:
#
#   1. XLSX and ZIP both embed per-entry modification times. Every file written
#      here is therefore stamped with ZIP_MTIME, a fixed instant derived from
#      the release itself, before being archived.
#   2. Row order. Table order comes from malaviR and is stable for a given
#      release, so it is preserved rather than re-sorted; re-sorting would make
#      the download disagree with the on-site table view.
# =============================================================================

set.seed(1)  # nothing here samples, but pin it so that stays true if it changes

suppressPackageStartupMessages({
  library(malaviR)
  library(writexl)
  library(ape)
  library(zip)
})

# ---- locate repo root + load central config ---------------------------------
# Same resolution the other export scripts use: derive it from --file= so the
# script works from any working directory, falling back to the cwd when sourced.
args       <- commandArgs(trailingOnly = FALSE)
file_arg   <- sub("^--file=", "", args[grep("^--file=", args)])
script_dir <- if (length(file_arg)) dirname(normalizePath(file_arg)) else getwd()
repo_root  <- normalizePath(file.path(script_dir, ".."))

cfg     <- yaml::read_yaml(file.path(repo_root, "config", "project.yml"))
release <- cfg$malaviR$release

source(file.path(repo_root, "export", "lib", "tables.R"))

cat("== malavi_rebuild :: build_downloads ==\n")
cat("release :", release, "\n\n")

# A fixed timestamp for every generated file. Derived from the release date so
# it is a property of the data, not of when the script happened to run.
ZIP_MTIME <- as.POSIXct(paste0(release, " 00:00:00"), tz = "UTC")

out_dir    <- file.path(repo_root, "docs", "assets", "downloads")
tables_dir <- file.path(out_dir, "tables")
dir.create(tables_dir, recursive = TRUE, showWarnings = FALSE)

# ---- tables: one CSV and one XLSX each --------------------------------------
# Every table the site browses gets its own pair of files, because every one of
# them carries a CSV and an Excel button on the site. Only the release's own
# tables go into the archive further down -- see there.
tables <- site_tables(release)
stopifnot(setequal(names(tables), SITE_TABLE_IDS))

release_csv_paths <- character(0)
for (id in SITE_TABLE_IDS) {
  df <- tables[[id]]
  csv  <- file.path(tables_dir, sprintf("%s_%s.csv",  id, release))
  xlsx <- file.path(tables_dir, sprintf("%s_%s.xlsx", id, release))

  # quote-all + CRLF-free output, so the file opens cleanly in Excel and R alike
  # and does not change shape because a value happened to contain a comma.
  utils::write.csv(df, csv, row.names = FALSE, na = "", fileEncoding = "UTF-8")
  writexl::write_xlsx(df, xlsx)

  Sys.setFileTime(csv,  ZIP_MTIME)
  Sys.setFileTime(xlsx, ZIP_MTIME)
  if (id %in% MALAVI_TABLE_IDS) release_csv_paths <- c(release_csv_paths, csv)
  cat(sprintf("  %-24s %7d rows  ->  csv + xlsx%s\n", id, nrow(df),
              if (id %in% MALAVI_TABLE_IDS) "" else "  (not in the archive)"))
}

# ---- alignment --------------------------------------------------------------
# The same alignment the sequence checker and the reports use.
aln       <- extract_alignment(version = release)
aln_path  <- file.path(out_dir, sprintf("malavi_alignment_%s.fasta", release))
ape::write.FASTA(aln, aln_path)
Sys.setFileTime(aln_path, ZIP_MTIME)
# as.matrix() first: length() on a DNAbin *matrix* counts cells, not sequences.
aln_mat <- as.matrix(aln)
cat(sprintf("\n  alignment                %7d sequences x %d bp  ->  fasta\n",
            nrow(aln_mat), ncol(aln_mat)))

# ---- "Everything" archive ---------------------------------------------------
# Every table of the RELEASE as CSV, plus the alignment. Two deliberate
# omissions: XLSX, which holds the same data and would roughly double the
# download for no additional information; and the derived host taxonomy key,
# which is malaviR's rather than MalAvi's -- an archive named for the release
# should contain the release and nothing else. It is still downloadable on its
# own from the table index.
zip_path <- file.path(out_dir, sprintf("malavi_%s.zip", release))
if (file.exists(zip_path)) file.remove(zip_path)
# Store paths relative to out_dir so the archive unpacks into a tidy directory
# rather than recreating the whole repo path.
zip::zip(
  zipfile = zip_path,
  files   = c(file.path("tables", basename(release_csv_paths)), basename(aln_path)),
  root    = out_dir,
  mode    = "cherry-pick"
)
Sys.setFileTime(zip_path, ZIP_MTIME)

cat(sprintf("  archive                  %7.1f MB  ->  zip\n",
            file.size(zip_path) / 1024^2))
cat("\nwrote:", out_dir, "\n")
