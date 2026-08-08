#!/usr/bin/env Rscript
# @title Build the lineage sequence index the website's sequence checker uses
# @purpose Export every MalAvi lineage sequence, normalized to the 479 bp
#          alignment window, into a JSON index the browser-side checker loads.
# @why The submit page's checker has to answer "is this sequence already in
#      MalAvi?" against the WHOLE release. Checking against a sample would let
#      it tell a submitter their sequence is new when it is already named --
#      the one error the checker must never make. Sequences that several
#      lineage names share are grouped, so the checker reports all of them
#      rather than picking one arbitrarily.
# @input /mnt/ellisbiostore/malaviR (bundled release, via malaviR::extract_table)
# @input config/project.yml
# @output docs/assets/data/lineage_sequences.json
# @program Rscript
# @program malaviR
# @program jsonlite
# @critical-var release
# @critical-var WINDOW_LENGTH
# =============================================================================
# Usage:
#   Rscript export/build_sequence_index.R              # write the index
#   Rscript export/build_sequence_index.R --dry-run    # report only, write nothing
# =============================================================================

suppressMessages({
  library(malaviR)
  if (!requireNamespace("yaml", quietly = TRUE))
    stop("The 'yaml' package is required. install.packages('yaml').")
  if (!requireNamespace("jsonlite", quietly = TRUE))
    stop("The 'jsonlite' package is required. install.packages('jsonlite').")
})

# ---- locate repo root + load central config ---------------------------------
args <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", args[grep("^--file=", args)])
script_dir <- if (length(file_arg)) dirname(normalizePath(file_arg)) else getwd()
repo_root <- normalizePath(file.path(script_dir, ".."))
cfg <- yaml::read_yaml(file.path(repo_root, "config", "project.yml"))

trailing <- commandArgs(trailingOnly = TRUE)
dry_run <- "--dry-run" %in% trailing

release <- cfg$malaviR$release
if (identical(release, "latest")) release <- malavi_version()

# The MalAvi barcode window. Every sequence in the release is stored padded to
# exactly this length; the checker relies on that to compare without aligning.
WINDOW_LENGTH <- cfg$alignment$length_bp

cat("== malavi_rebuild :: build_sequence_index ==\n")
cat("release :", release, "\n")
cat("window  :", WINDOW_LENGTH, "bp\n\n")

lineages <- extract_table("Grand Lineage Summary", version = release)

# ---- normalize --------------------------------------------------------------
# The release mixes upper and lower case and carries IUPAC ambiguity codes and
# alignment gaps. Normalizing here -- once, at build time -- means the browser
# compares like with like and never has to guess about case.
normalize <- function(x) toupper(trimws(x))

lineages$SEQ_NORM <- normalize(lineages$SEQUENCE)

bad_length <- lineages$LINEAGE_NAME[nchar(lineages$SEQ_NORM) != WINDOW_LENGTH]
if (length(bad_length)) {
  cat("WARNING: ", length(bad_length), " sequence(s) are not ", WINDOW_LENGTH, " bp:\n", sep = "")
  cat(paste0("  - ", head(bad_length, 20), collapse = "\n"), "\n\n", sep = "")
}

alphabet <- sort(unique(strsplit(paste(lineages$SEQ_NORM, collapse = ""), "")[[1]]))
cat("alphabet present:", paste(alphabet, collapse = ""), "\n")

# ---- group lineage names by identical sequence ------------------------------
# Several lineage names can carry byte-identical sequences. The checker must
# report every name for a matched sequence, not the first one it happens to
# find, so the index is keyed by sequence with a vector of names attached.
by_seq <- split(seq_len(nrow(lineages)), lineages$SEQ_NORM)

entries <- lapply(names(by_seq), function(sq) {
  idx <- by_seq[[sq]]
  # A lineage can appear on several Grand Lineage Summary rows (one per
  # morphospecies link); unique() keeps each name once.
  names_here <- sort(unique(lineages$LINEAGE_NAME[idx]))
  list(
    seq     = sq,
    names   = I(names_here),
    acc     = I(sort(unique(lineages$GENBANK_ACC[idx][!is.na(lineages$GENBANK_ACC[idx])]))),
    genus   = I(sort(unique(lineages$GENUS_NAME[idx][!is.na(lineages$GENUS_NAME[idx])]))),
    species = I(sort(unique(lineages$SPECIES_NAME[idx][!is.na(lineages$SPECIES_NAME[idx])])))
  )
})

shared <- Filter(function(e) length(e$names) > 1, entries)
cat("\nrows in release        :", nrow(lineages), "\n")
cat("distinct lineage names :", length(unique(lineages$LINEAGE_NAME)), "\n")
cat("distinct sequences     :", length(entries), "\n")
cat("sequences shared by >1 lineage name:", length(shared), "\n")
for (e in shared) cat("  ", paste(e$names, collapse = " = "), "\n")

payload <- list(
  release       = release,
  generated     = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  window_length = WINDOW_LENGTH,
  min_length    = cfg$alignment$min_unambiguous_bp,
  n_lineages    = length(unique(lineages$LINEAGE_NAME)),
  n_sequences   = length(entries),
  entries       = entries
)

out_path <- file.path(repo_root, cfg$paths$docs_data_dir, "lineage_sequences.json")
if (dry_run) {
  cat("\n[dry-run] would write:", out_path, "\n")
} else {
  dir.create(dirname(out_path), recursive = TRUE, showWarnings = FALSE)
  jsonlite::write_json(payload, out_path, auto_unbox = TRUE, digits = NA)
  cat("\nwrote:", out_path, " (",
      format(file.info(out_path)$size / 1024^2, digits = 3), "MB )\n", sep = "")
}
