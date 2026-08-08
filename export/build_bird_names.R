#!/usr/bin/env Rscript
# @title Export the avian checklist the lineage name checker validates against
# @purpose Write the clootl/eBird species list bundled with malaviR -- scientific
#          names plus the IOC, BirdLife and Howard & Moore synonyms clootl carries
#          -- into a compact JSON the browser can check a host name against.
# @why The site's name checker builds a lineage acronym from any two words. Its own
#      test suite pins "Zzzzzus qqqqqus" -> ...01, so a typo or an invented name
#      silently yields a real-looking MalAvi lineage name. Checking against a real
#      checklist catches that. It must NOT check against MalAvi's own host list:
#      the interesting case for a new lineage is a host MalAvi has never recorded,
#      and rejecting those would reject exactly the submissions worth having.
# @input /mnt/ellisbiostore/malaviR (bundled clootl snapshot in R/sysdata.rda)
# @input config/project.yml
# @output docs/assets/data/bird_names.json
# @program Rscript
# @program malaviR
# @program jsonlite
# @critical-var clootl_year
# =============================================================================
# Usage:
#   Rscript export/build_bird_names.R              # write the checklist
#   Rscript export/build_bird_names.R --dry-run    # report only, write nothing
#
# Shape of the output. Names are grouped by genus rather than listed as flat
# binomials, because the genus is repeated across all of its species and a flat
# list stores it once per species:
#
#   { "generated": ..., "clootl_year": 2025, "n_species": 11167,
#     "accepted": { "Turdus": ["migratorius", "merula", ...], ... },
#     "synonyms": { "Abrornis": { "maculipennis": "Phylloscopus maculipennis" } },
#     "families": { "Turdus": "Turdidae (Thrushes and Allies)", ... } }
#
# `accepted` answers "is this a current eBird species?". `synonyms` answers "is
# this an older name, and what is it now?" -- which matters because MalAvi is full
# of older binomials and a submitter reading an older paper will use them.
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

cat("== malavi_rebuild :: build_bird_names ==\n")

# The clootl reference is malaviR package data, not an exported object. Reaching
# for it with ::: is deliberate and is the reason this exporter exists rather
# than the site depending on the package: the extraction happens once, here, and
# the browser gets a plain file.
clootl <- malaviR:::clootl_ref
clootl_year <- tryCatch(clootl_taxonomy_version(), error = function(e) NA)

cat("clootl taxonomy year :", as.character(clootl_year), "\n")
cat("species in checklist :", nrow(clootl), "\n")

# ---- split binomials into genus + epithet -----------------------------------
# Anything that is not a clean two-word binomial is dropped rather than guessed
# at: a checker that has to explain itself to a submitter cannot be built on
# names it did not understand.
split_binomial <- function(names) {
  parts <- strsplit(trimws(as.character(names)), "\\s+")
  keep <- vapply(parts, length, integer(1)) == 2
  list(
    genus   = vapply(parts[keep], `[`, character(1), 1),
    epithet = vapply(parts[keep], `[`, character(1), 2),
    keep    = keep
  )
}

accepted <- split_binomial(clootl$SCI_NAME)
n_dropped <- sum(!accepted$keep)
if (n_dropped) cat("names that are not plain binomials (dropped):", n_dropped, "\n")

# accepted: genus -> the epithets it holds
accepted_by_genus <- split(accepted$epithet, accepted$genus)
# I() marks each vector AsIs so jsonlite keeps it an ARRAY even when a genus holds
# exactly one species. Without it auto_unbox collapses those to a bare string, and
# the browser's substring semantics then accept 'Necrosyrtes mona' as a real bird.
accepted_by_genus <- lapply(accepted_by_genus, function(x) I(sort(unique(x))))

# families: genus -> family. A genus sits in exactly one family, so the first
# non-missing value is the family; where a genus somehow spans two, the entry is
# dropped rather than one being picked, since the checker only uses it to tell a
# submitter what they matched.
family_of <- tapply(as.character(clootl$FAMILY)[accepted$keep],
                    accepted$genus, function(x) {
                      u <- unique(x[!is.na(x) & nzchar(x)])
                      if (length(u) == 1) u else NA_character_
                    })
family_of <- family_of[!is.na(family_of)]

# ---- synonyms ---------------------------------------------------------------
# clootl carries the IOC, BirdLife and Howard & Moore name for each species. Any
# that differs from the eBird name is a synonym a submitter might reasonably use,
# and resolving it is how the checker can say "that is now Phylloscopus
# maculipennis" instead of "unrecognized".
synonym_pairs <- list()
for (column in c("IOC_name", "Birdlife_name", "H_M_name")) {
  if (!column %in% names(clootl)) next
  other <- trimws(as.character(clootl[[column]]))
  current <- trimws(as.character(clootl$SCI_NAME))
  differs <- !is.na(other) & nzchar(other) & other != current
  synonym_pairs[[column]] <- data.frame(
    from = other[differs], to = current[differs], stringsAsFactors = FALSE)
}
synonyms <- do.call(rbind, synonym_pairs)
synonyms <- synonyms[!duplicated(synonyms$from), , drop = FALSE]

# A synonym that is itself a currently accepted name is dropped: it would tell a
# submitter their correct name is an old one for something else, which is worse
# than saying nothing. This happens when a name was moved between species.
synonyms <- synonyms[!synonyms$from %in% clootl$SCI_NAME, , drop = FALSE]

syn_split <- split_binomial(synonyms$from)
synonyms <- synonyms[syn_split$keep, , drop = FALSE]
syn_by_genus <- split(
  data.frame(epithet = syn_split$epithet, to = synonyms$to,
             stringsAsFactors = FALSE),
  syn_split$genus)
syn_by_genus <- lapply(syn_by_genus, function(df) {
  out <- as.list(df$to)
  names(out) <- df$epithet
  out
})

cat("genera               :", length(accepted_by_genus), "\n")
cat("resolvable synonyms  :", nrow(synonyms), "\n")

payload <- list(
  generated   = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  source      = "clootl (eBird/Clements) as bundled with malaviR",
  clootl_year = if (is.na(clootl_year)) NULL else as.integer(clootl_year),
  n_species   = sum(accepted$keep),
  n_genera    = length(accepted_by_genus),
  n_synonyms  = nrow(synonyms),
  accepted    = accepted_by_genus,
  synonyms    = syn_by_genus,
  families    = as.list(family_of)
)

out_path <- file.path(repo_root, cfg$paths$docs_data_dir, "bird_names.json")
if (dry_run) {
  cat("\n[dry-run] would write:", out_path, "\n")
} else {
  dir.create(dirname(out_path), recursive = TRUE, showWarnings = FALSE)
  jsonlite::write_json(payload, out_path, auto_unbox = TRUE)
  cat("\nwrote:", out_path, " (",
      format(file.info(out_path)$size / 1024, digits = 4), "KB )\n", sep = "")
}
