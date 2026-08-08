#!/usr/bin/env Rscript
# @title Export MalAvi lineage barcode sequences to FASTA
# @purpose Dump every MalAvi lineage's cytochrome-b sequence to a FASTA file so a
#   BLAST database of "what MalAvi already knows" can be built for the lineage
#   gap-finder (lineage_gap_finder.py).
# @why The gap-finder decides whether a GenBank haemosporidian cytb sequence is a
#   NEW lineage (absent from MalAvi) by BLASTing it against these reference
#   sequences; a <100% best identity means MalAvi has no matching lineage.
# @input config/project.yml (malaviR.source_path + malaviR.release)
# @input <source_path>/inst/extdata/malavi_db_<release>.rds
# @output data/malavi_lineages.fasta  (or the path given as the first argument)
# @program Rscript
# @program yaml
# @critical-var MALAVIR_RELEASE
# @critical-var OUT_PATH

# Resolve this script's directory so paths work regardless of the caller's cwd.
args_all <- commandArgs(trailingOnly = FALSE)
file_arg <- sub("^--file=", "", args_all[grep("^--file=", args_all)])
script_dir <- if (length(file_arg)) dirname(normalizePath(file_arg)) else getwd()
repo_root <- normalizePath(file.path(script_dir, ".."))

suppressPackageStartupMessages(library(yaml))
cfg <- yaml::read_yaml(file.path(repo_root, "config", "project.yml"))
malavir_source  <- cfg$malaviR$source_path
MALAVIR_RELEASE <- cfg$malaviR$release

cli <- commandArgs(trailingOnly = TRUE)
OUT_PATH <- if (length(cli) >= 1) cli[[1]] else file.path(repo_root, "data", "malavi_lineages.fasta")

db_file <- file.path(malavir_source, "inst", "extdata",
                    paste0("malavi_db_", MALAVIR_RELEASE, ".rds"))
if (!file.exists(db_file)) stop("MalAvi DB file not found: ", db_file, call. = FALSE)
db <- readRDS(db_file)
g  <- db[["grand_lineage_summary"]]

# Each lineage carries its representative cytb sequence in SEQUENCE. Uppercase and
# strip alignment gaps / whitespace so BLAST sees the raw nucleotides. Skip
# lineages with no usable sequence.
lineage <- trimws(as.character(g$LINEAGE_NAME))
genus   <- trimws(as.character(g$GENUS_NAME))
seqs    <- toupper(gsub("[^ACGTNacgtn]", "", as.character(g$SEQUENCE)))

keep <- !is.na(lineage) & lineage != "" & !is.na(seqs) & nchar(seqs) >= 100
lineage <- lineage[keep]; genus <- genus[keep]; seqs <- seqs[keep]

dir.create(dirname(OUT_PATH), showWarnings = FALSE, recursive = TRUE)
con <- file(OUT_PATH, "w")
for (i in seq_along(seqs)) {
  # FASTA id = lineage name; genus kept in the defline for downstream reporting.
  writeLines(sprintf(">%s %s", lineage[i], genus[i]), con)
  writeLines(seqs[i], con)
}
close(con)
cat("Wrote", length(seqs), "MalAvi lineage sequences to", OUT_PATH, "\n")
