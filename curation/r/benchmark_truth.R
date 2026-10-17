#!/usr/bin/env Rscript
# =============================================================================
# benchmark_truth.R
# -----------------------------------------------------------------------------
# Emit the MalAvi "ground truth" for one or more references as JSON, so the
# Python benchmark can score what the curation extractor recovers against what
# MalAvi already records for those papers.
#
# For each REFERENCE_NAME we collect, from the malaviR package (the source of
# truth):
#   - lineages   : LINEAGE_NAME reported under that reference (Hosts and Sites)
#   - accessions : GENBANK_ACC of those lineages (Grand Lineage Summary)
#   - hosts      : "Genus species" host binomials
#   - countries  : country names
#
# Usage:
#   Rscript curation/r/benchmark_truth.R "Ref A" "Ref B" ... > truth.json
#   Rscript curation/r/benchmark_truth.R --all-benchmark   > truth.json
#   Rscript curation/r/benchmark_truth.R --corpus          > truth_corpus.json
#   Rscript curation/r/benchmark_truth.R --holdout         > truth_holdout.json
#
# The --holdout reference list is NOT hard-coded here. It is read back out of
# curation/benchmark/holdout_manifest.json, which is the committed record of the
# seeded random draw. That indirection is the whole point: a hard-coded list in
# this file could be edited to drop an awkward paper, and nobody reading the
# output could tell. Reading the manifest means the reference set is whatever
# select_holdout_corpus.py drew, and re-running that script reproduces it.
# =============================================================================

suppressMessages({
  library(malaviR)
  if (!requireNamespace("jsonlite", quietly = TRUE))
    stop("jsonlite is required: install.packages('jsonlite').")
})

# Clean MalAvi's known export artifacts from accession strings: trailing
# whitespace / embedded newlines (e.g. "PV839588\n") and empty values.
clean_acc <- function(x) {
  x <- trimws(gsub("[\r\n]+", "", x))
  x <- x[nzchar(x) & !is.na(x)]
  sort(unique(x))
}

# The default benchmark set (recent papers; the references whose PDFs we obtain
# fresh — represents the "new papers from here on out" workflow).
BENCHMARK_REFS <- c(
  "Harl et al 2026", "Bell et al 2025b", "Duc et al 2025",
  "Pacheco et al 2025", "Markakis et al 2025a"
)

# The "old corpus" diversity set, drawn from the archived MalAvi Papers
# collection. Chosen to span publication year (2005-2018), journal/layout, and
# content type (host surveys, a vector study, a morphospecies description, and
# dense prevalence/community matrices). All are already curated in MalAvi, so
# they double as ground truth. See curation/benchmark/README.md for rationale.
CORPUS_REFS <- c(
  "Hellgren 2005",        # older single-topic survey (J Orn)
  "Martinsen et al 2008", # phylogenetics, parasite-genus heavy (MPE)
  "Ishtiaq et al 2010",   # biogeography, multi-region (J Biogeogr)
  "Marzal et al 2011",    # multi-continent survey (PLoS ONE)
  "Ventim et al 2012a",   # near-duplicate author/region pair (disambiguation)
  "Ventim et al 2012b",   # near-duplicate author/region pair (disambiguation)
  "Karamba et al 2012",   # obscure journal, lower scan/OCR quality (IJBMBR)
  "Kim & Tsuda 2012",     # vector study, mosquito (Mol Ecol)
  "Ilgunas et al 2013",   # formal morphospecies description (Zootaxa)
  "Okanga et al 2014",    # sub-Saharan Africa survey (PLoS ONE)
  "Lotta et al 2016",     # plate/figure-heavy morphology (Protist)
  "Fecchio et al 2018b"   # community ecology, dense prevalence matrices (Oikos)
)

# -----------------------------------------------------------------------------
# The held-out validation corpus, read back out of the committed draw record.
# -----------------------------------------------------------------------------

# Locate this script on disk so the manifest can be found regardless of the
# working directory the user happens to run from. commandArgs(FALSE) carries
# "--file=<path>" when the script is run through Rscript.
script_directory <- function() {
  invocation <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (length(invocation) == 0) return(NA_character_)
  dirname(normalizePath(sub("^--file=", "", invocation[1])))
}

# Read the selected reference names out of holdout_manifest.json.
#
# The manifest nests them as strata -> <stratum> -> selected -> reference_name.
# Strata are visited in the order the manifest lists them, and papers in the
# order they were drawn within each stratum, so the reference list is stable
# across runs. Reserves are deliberately ignored: a reserve only enters the
# corpus through a recorded substitution, at which point it appears in
# "selected" and is picked up here automatically.
read_holdout_refs <- function() {
  here <- script_directory()
  # curation/r/ -> curation/ -> repository root.
  manifest_path <- if (is.na(here)) {
    "curation/benchmark/holdout_manifest.json"
  } else {
    file.path(dirname(dirname(here)), "curation", "benchmark", "holdout_manifest.json")
  }

  if (!file.exists(manifest_path))
    stop("--holdout needs the draw record, but it is not at: ", manifest_path,
         "\nRegenerate it with: python curation/benchmark/select_holdout_corpus.py")

  manifest <- jsonlite::fromJSON(manifest_path, simplifyVector = FALSE)

  selected <- unlist(lapply(manifest$strata, function(stratum)
    vapply(stratum$selected, function(entry) entry$reference_name, character(1))),
    use.names = FALSE)

  if (length(selected) == 0)
    stop("holdout_manifest.json lists no selected references; the draw is empty.")

  # A substitution is a legitimate, recorded swap, but it changes which papers
  # the number covers, so it must never pass silently into a report.
  if (length(manifest$substitutions) > 0)
    message("NOTE: holdout_manifest.json records ", length(manifest$substitutions),
            " substitution(s). The reference set is not the original draw.")

  selected
}

args <- commandArgs(trailingOnly = TRUE)
refs <- if (length(args) == 0 || identical(args[1], "--all-benchmark")) {
  BENCHMARK_REFS
} else if (identical(args[1], "--corpus")) {
  CORPUS_REFS
} else if (identical(args[1], "--holdout")) {
  read_holdout_refs()
} else {
  args
}

hs  <- extract_table("Hosts and Sites Table")
gls <- extract_table("Grand Lineage Summary")
vd  <- extract_table("Vector Data Table")

# ---------------------------------------------------------------------------
# Warn when a reference name matches more than one paper.
#
# Records are selected below with grepl(..., fixed = TRUE), which is a SUBSTRING
# match, not equality. That is load-bearing for names that appear inside a longer
# multi-reference string, but it also means "Ventim et al 2012" would silently
# pool the records of "Ventim et al 2012a" and "Ventim et al 2012b" into one
# truth set -- inflating truth and depressing measured recall, with nothing in
# the output to show it happened.
#
# This only warns. Changing the matching rule could alter existing published
# numbers, which is not something to do as a side effect of adding a new mode.
# ---------------------------------------------------------------------------
all_reference_names <- unique(c(hs$REFERENCE_NAME, vd$REFERENCE_NAME))
for (r in refs) {
  matched <- all_reference_names[grepl(r, all_reference_names, fixed = TRUE)]
  if (length(matched) > 1)
    message("WARNING: reference '", r, "' matches ", length(matched),
            " release references and their records are pooled: ",
            paste(matched, collapse = " | "))
  if (length(matched) == 0)
    message("WARNING: reference '", r, "' matches no records in the release.")
}

truth <- lapply(refs, function(r) {
  # fixed = TRUE so reference names with regex-special characters match literally.
  rows  <- hs[grepl(r, hs$REFERENCE_NAME, fixed = TRUE), ]
  vrows <- vd[grepl(r, vd$REFERENCE_NAME, fixed = TRUE), ]

  # Lineages and accessions are pooled across host AND vector records so that
  # vector-only papers (whose lineages live in the Vector Data Table) get
  # complete accession ground truth, not just the host-table subset.
  lineages <- sort(unique(c(rows$LINEAGE_NAME, vrows$LINEAGE_NAME)))
  lineages <- lineages[nzchar(lineages)]
  accessions <- clean_acc(gls$GENBANK_ACC[gls$LINEAGE_NAME %in% lineages])
  hosts <- sort(unique(trimws(paste(rows$GENUS_NAME, rows$SPECIES_NAME))))
  vectors <- sort(unique(trimws(vrows$VECTOR_SPECIES[nzchar(vrows$VECTOR_SPECIES)])))
  countries <- sort(unique(c(rows$COUNTRY_NAME, vrows$COUNTRY_NAME)))
  countries <- countries[nzchar(countries)]
  list(
    reference   = r,
    n_records   = nrow(rows),
    n_vector_records = nrow(vrows),
    lineages    = as.list(lineages),
    accessions  = as.list(accessions),
    hosts       = as.list(hosts),
    vectors     = as.list(vectors),
    countries   = as.list(countries)
  )
})
names(truth) <- refs

cat(jsonlite::toJSON(truth, auto_unbox = TRUE, pretty = TRUE))
cat("\n")
