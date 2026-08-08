#!/usr/bin/env Rscript
# =============================================================================
# gate_reference.R
# -----------------------------------------------------------------------------
# Emit a compact snapshot of the CURRENT MalAvi database (from malaviR, the
# source of truth) that the Python pre-ingest validation gate uses to check new
# additions WITHOUT calling R at submission time:
#   - lineages             : every known LINEAGE_NAME (Grand Lineage Summary)
#   - accession_to_lineage : GenBank accession -> the lineage it is curated under
#
# These let the gate answer, for a new submission: is this lineage name already
# taken? is this accession already in MalAvi (and under which lineage)? — the
# referential-integrity / duplicate-collision checks.
#
# Usage:
#   Rscript curation/r/gate_reference.R \
#     > curation/src/malavi_curation/data/db_snapshot.json
# =============================================================================

suppressMessages({
  library(malaviR)
  if (!requireNamespace("jsonlite", quietly = TRUE))
    stop("jsonlite is required: install.packages('jsonlite').")
})

gls <- extract_table("Grand Lineage Summary")

# Known lineage names.
lineages <- sort(unique(trimws(gls$LINEAGE_NAME)))
lineages <- lineages[nzchar(lineages)]

# Accession -> lineage. GENBANK_ACC can hold export artifacts (embedded
# newlines) and, rarely, more than one accession; split on non-accession
# separators and map each token to its lineage.
acc_map <- list()
for (i in seq_len(nrow(gls))) {
  lin <- trimws(gls$LINEAGE_NAME[i])
  raw <- gls$GENBANK_ACC[i]
  if (is.na(raw) || !nzchar(trimws(raw)) || !nzchar(lin)) next
  toks <- unlist(strsplit(raw, "[^A-Za-z0-9.]+"))
  toks <- toupper(trimws(toks))
  # Strip version suffix (".1") so comparison is version-agnostic.
  toks <- sub("\\.[0-9]+$", "", toks)
  # Keep only well-formed nucleotide accessions (same shape the Python miner
  # uses: 1-2 letters + 5-6 digits), so stray words like "LINEAGE"/"NAME" that
  # appear in some cells are not treated as accessions.
  toks <- toks[grepl("^[A-Z]{1,2}[0-9]{5,6}$", toks)]
  for (t in toks) acc_map[[t]] <- lin
}

out <- list(
  source_release       = malavi_version(),
  n_lineages           = length(lineages),
  lineages             = as.list(lineages),
  accession_to_lineage = acc_map
)
cat(jsonlite::toJSON(out, auto_unbox = TRUE, pretty = FALSE))
cat("\n")
