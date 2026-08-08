# @title Build the three data reports the website publishes
# @purpose Generate the synonymy report, the ambiguous-pairs report and a
#          release-wide lineage QC sweep, each as a CSV pinned to one release.
# @why The site offers these as downloads that "stay fixed to a release". They
#      must therefore be derived only from the pinned release, with no manual
#      steps, so that regenerating them from the same release reproduces the
#      same files exactly.
# @input /mnt/ellisbiostore/malaviR (bundled release, via malaviR)
# @input config/project.yml
# @output docs/assets/reports/synonymy_<release>.csv
# @output docs/assets/reports/ambiguous_pairs_<release>.csv
# @output docs/assets/reports/lineage_qc_<release>.csv
# @output docs/assets/data/reports.json
# @program Rscript
# @program malaviR
# @critical-var release
# @critical-var SYNONYMY_METHOD
# @critical-var EXPECTED_LENGTH_BP
# @critical-var GENETIC_CODE
# =============================================================================
# Determinism
# -----------
# Every report is a pure function of the pinned release: same release in, same
# CSV out. Nothing samples, nothing consults the clock, and every table is sorted
# on a key that is unique within it, so row order cannot depend on hash order or
# on the order malaviR happens to return rows in. The generation timestamp lives
# only in reports.json (metadata the site displays), never inside a CSV.
# =============================================================================

suppressPackageStartupMessages({
  library(malaviR)
  if (!requireNamespace("jsonlite", quietly = TRUE))
    stop("The 'jsonlite' package is required. install.packages('jsonlite').")
  if (!requireNamespace("yaml", quietly = TRUE))
    stop("The 'yaml' package is required. install.packages('yaml').")
})

# ---- locate repo root + load central config ---------------------------------
args       <- commandArgs(trailingOnly = FALSE)
file_arg   <- sub("^--file=", "", args[grep("^--file=", args)])
script_dir <- if (length(file_arg)) dirname(normalizePath(file_arg)) else getwd()
repo_root  <- normalizePath(file.path(script_dir, ".."))

cfg     <- yaml::read_yaml(file.path(repo_root, "config", "project.yml"))
release <- cfg$malaviR$release

# ---- parameters -------------------------------------------------------------
# "overlap" compares sequences only over positions where both have a base, so a
# short sequence that matches a long one across its whole length counts as the
# same haplotype. "strict" would instead treat the missing tail as a difference.
# Overlap is the definition the MalAvi name-inflation literature uses, and the
# one the site's synonymy card describes ("resolve to the same sequence").
SYNONYMY_METHOD <- "overlap"

# The MalAvi cytochrome b barcode window. Sequences shorter than this are usable
# but cannot be compared over the full frame.
EXPECTED_LENGTH_BP <- cfg$alignment$length_bp

# NCBI genetic code 4 (mold/protozoan mitochondrial) is the correct code for
# avian haemosporidians, and is what malaviR::lineage_qc() uses. Reusing
# malaviR's own translation keeps this release-wide sweep consistent with the
# per-sequence checker rather than reimplementing translation beside it.
GENETIC_CODE <- 4

out_dir  <- file.path(repo_root, "docs", "assets", "reports")
data_dir <- file.path(repo_root, cfg$paths$docs_data_dir)
dir.create(out_dir, recursive = TRUE, showWarnings = FALSE)

cat("== malavi_rebuild :: build_reports ==\n")
cat("release :", release, "\n\n")

write_report <- function(df, stem) {
  path <- file.path(out_dir, sprintf("%s_%s.csv", stem, release))
  utils::write.csv(df, path, row.names = FALSE, na = "", fileEncoding = "UTF-8")
  cat(sprintf("  %-18s %6d rows  ->  %s\n", stem, nrow(df), basename(path)))
  list(file = basename(path), rows = nrow(df))
}

# =============================================================================
# 1. Synonymy: lineage names that resolve to the same sequence
# =============================================================================
syn <- synonymy_report(version = release, method = SYNONYMY_METHOD)

synonymy_csv <- syn$synonymies
# Sort by group, then by name within the group, so the file is stable and a
# reader can see each haplotype's members together.
synonymy_csv <- synonymy_csv[order(synonymy_csv$haplotype, synonymy_csv$lineage), ]
meta_syn <- write_report(synonymy_csv, "synonymy")

cat(sprintf("     %d names across %d haplotype groups (%.1f%% name inflation)\n",
            syn$summary$n_lineages_in_synonymies,
            syn$summary$n_synonymous_haplotypes,
            syn$summary$pct_diversity_inflation))

# =============================================================================
# 2. Ambiguous pairs: lineages separated only by ambiguous positions
# =============================================================================
amb <- ambiguous_pairs(version = release)

ambiguous_csv <- amb$pairs
if (nrow(ambiguous_csv)) {
  ambiguous_csv <- ambiguous_csv[order(ambiguous_csv[[1]], ambiguous_csv[[2]]), ]
}
meta_amb <- write_report(ambiguous_csv, "ambiguous_pairs")

# =============================================================================
# 3. Release-wide lineage QC: length, frame, duplicates
# =============================================================================
# One row per lineage in the alignment. This is a sweep of the release itself,
# not a check of a submitted sequence, so it reports the properties a curator
# would want to see flagged rather than a novelty call.
aln_mat <- toupper(as.character(as.matrix(extract_alignment(version = release))))
lineage_ids <- rownames(aln_mat)

# malaviR's own code-4 translation, so the frame check here means exactly what
# it means in lineage_qc(). Fail loudly rather than silently diverging if a
# future malaviR renames these.
ns <- asNamespace("malaviR")
if (!all(c(".qc_translate", ".qc_genetic_code_4") %in% ls(ns, all.names = TRUE))) {
  stop("malaviR no longer provides .qc_translate/.qc_genetic_code_4; the frame ",
       "check must be updated to match whatever replaced them.", call. = FALSE)
}
qc_translate <- get(".qc_translate", envir = ns)
code4        <- get(".qc_genetic_code_4", envir = ns)()

GAP_CHARS  <- c("-", ".", "~")
BASE_CHARS <- c("A", "C", "G", "T")

qc_rows <- lapply(seq_len(nrow(aln_mat)), function(i) {
  chars <- aln_mat[i, ]
  is_gap  <- chars %in% GAP_CHARS
  is_base <- chars %in% BASE_CHARS
  # Anything that is neither a gap nor an unambiguous base is an IUPAC
  # ambiguity code (N, R, Y, ...).
  is_ambig <- !is_gap & !is_base

  # Translate the sequence as it sits in the alignment frame. Gaps are left in
  # place so codon boundaries still line up with the reference frame.
  aa <- qc_translate(chars, code4)
  n_stops <- sum(aa == "*", na.rm = TRUE)

  data.frame(
    lineage            = lineage_ids[i],
    aligned_length_bp  = sum(!is_gap),
    n_ambiguous_bases  = sum(is_ambig),
    n_gaps             = sum(is_gap),
    n_stop_codons      = n_stops,
    has_stop_codon     = n_stops > 0L,
    shorter_than_frame = sum(!is_gap) < EXPECTED_LENGTH_BP,
    stringsAsFactors   = FALSE
  )
})
qc_csv <- do.call(rbind, qc_rows)

# Flag membership in a synonymy group, reusing report 1 rather than recomputing
# duplicates with a second, possibly different, definition.
qc_csv$in_synonymy_group <- qc_csv$lineage %in% syn$synonymies$lineage

qc_csv <- qc_csv[order(qc_csv$lineage), ]
meta_qc <- write_report(qc_csv, "lineage_qc")

cat(sprintf("     %d with a stop codon, %d shorter than %d bp, %d carrying ambiguities\n",
            sum(qc_csv$has_stop_codon), sum(qc_csv$shorter_than_frame),
            EXPECTED_LENGTH_BP, sum(qc_csv$n_ambiguous_bases > 0)))

# =============================================================================
# Metadata for the website
# =============================================================================
# The site renders its report cards from this, so a new release updates the
# links and row counts without anyone editing the HTML.
reports <- list(
  release   = release,
  generated = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  reports   = list(
    list(id = "synonymy", title = "Synonymy report",
         description = "Lineage names that resolve to the same sequence.",
         file = meta_syn$file, rows = meta_syn$rows),
    list(id = "ambiguous_pairs", title = "Ambiguous pairs",
         description = "Lineage pairs separated by ambiguous positions alone.",
         file = meta_amb$file, rows = meta_amb$rows),
    list(id = "lineage_qc", title = "Lineage QC",
         description = "Sequence checks across the whole release: length, frame, duplicates.",
         file = meta_qc$file, rows = meta_qc$rows)
  )
)
json_path <- file.path(data_dir, "reports.json")
jsonlite::write_json(reports, json_path, auto_unbox = TRUE, pretty = TRUE)
cat("\nwrote:", json_path, "\n")
