#!/usr/bin/env Rscript
# =============================================================================
# gazetteer.R
# -----------------------------------------------------------------------------
# Emit reference vocabularies from malaviR (the source of truth) that ground the
# Python host/geography extractor: the set of known avian host genera and host
# binomials, and the set of country names MalAvi uses. Matching candidate names
# against these gives the extractor high precision without hard-coding lists.
#
# A new host of a *known* genus (common case for a novel lineage) still passes
# the genus filter, so genus-level grounding keeps recall high too.
#
# Usage:
#   Rscript curation/r/gazetteer.R > curation/src/malavi_curation/data/gazetteer.json
# =============================================================================

suppressMessages({
  library(malaviR)
  if (!requireNamespace("jsonlite", quietly = TRUE))
    stop("jsonlite is required: install.packages('jsonlite').")
})

hs <- extract_table("Hosts and Sites Table")
vd <- extract_table("Vector Data Table")

# Helper: capitalized single-word genera from a column of names/binomials.
genera_from <- function(x) {
  g <- sort(unique(trimws(x)))
  g[nzchar(g) & grepl("^[A-Z][a-z]+$", g)]
}

# Helper: "Genus species" binomials; the source column sometimes already
# includes the genus, so take the last two tokens as the binomial.
binomials_from <- function(x) {
  b <- vapply(strsplit(trimws(x), "\\s+"), function(p) {
    if (length(p) >= 2) paste(tail(p, 2), collapse = " ") else NA_character_
  }, character(1))
  sort(unique(b[!is.na(b) & grepl("^[A-Z][a-z]+ [a-z]+$", b)]))
}

# Known avian host genera + binomials (Hosts and Sites Table).
genera <- genera_from(hs$GENUS_NAME)
binom  <- binomials_from(paste(hs$GENUS_NAME, hs$SPECIES_NAME))

# Known arthropod-vector genera + binomials (Vector Data Table). VECTOR_SPECIES
# is a free-text species name (e.g. "Culex pipiens"); its leading capitalized
# token is the genus. This grounds vector extraction the same way hosts are.
vec_genus_tok <- sub("\\s.*$", "", trimws(vd$VECTOR_SPECIES))
vector_genera <- genera_from(vec_genus_tok)
vector_binom  <- binomials_from(vd$VECTOR_SPECIES)

# Country names used by MalAvi (geography gazetteer; union of both tables).
countries <- sort(unique(trimws(c(hs$COUNTRY_NAME, vd$COUNTRY_NAME))))
countries <- countries[nzchar(countries)]

out <- list(
  source_release = malavi_version(),
  genera = as.list(genera),
  binomials = as.list(binom),
  vector_genera = as.list(vector_genera),
  vector_binomials = as.list(vector_binom),
  countries = as.list(countries)
)
cat(jsonlite::toJSON(out, auto_unbox = TRUE, pretty = TRUE))
cat("\n")
