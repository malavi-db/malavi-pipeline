#!/usr/bin/env Rscript
# =============================================================================
# validate_record.R
# -----------------------------------------------------------------------------
# Validate candidate MalAvi records using malaviR's existing checks, and emit
# flags for the curator report. THIN WRAPPER -- the logic lives in malaviR (and
# in host_geo_flag.R); this script only adapts a candidate submission's records
# to those inputs and collects results as JSON.
#
# Reuses (do not reimplement):
#   malaviR::match_taxonomy()  -- reconcile host names to standard avian taxonomy
#   host_geo_flag()            -- improbable host/locality for a lineage (sourced)
#   malaviR::lineage_qc()      -- (optional) plausibility screen for a cytb barcode
#                                 when a record carries an actual sequence
#
# I/O contract (JSON):
#   in : { "version": "latest",
#          "records": [ {"host_species": "...", "lineage_name": "..."(opt),
#                        "country": "..."(opt), "sequence": "..."(opt)}, ... ] }
#   out: { "host_taxonomy": [ {host_species, ebird_species, order, family,
#                              match_type, flagged, reason} ],
#          "record_flags":  [ {host_species, lineage_name, type, severity, reason} ] }
#
# Usage:
#   Rscript curation/r/validate_record.R input.json > flags.json
#   (or pipe JSON on stdin)
# =============================================================================

suppressMessages({
  library(malaviR)
  if (!requireNamespace("jsonlite", quietly = TRUE))
    stop("jsonlite is required.")
})

# Source host_geo_flag() from the same directory as this script.
.this_dir <- tryCatch({
  a <- commandArgs(trailingOnly = FALSE)
  fa <- sub("^--file=", "", a[grep("^--file=", a)])
  if (length(fa)) dirname(normalizePath(fa)) else getwd()
}, error = function(e) getwd())
source(file.path(.this_dir, "host_geo_flag.R"), local = FALSE)
# Host and biogeographic plausibility. Deliberately not part of malaviR -- see the
# header of that file for why it must not become a public verb over curated data.
source(file.path(.this_dir, "lineage_plausibility.R"), local = FALSE)

validate_records <- function(records, version = "latest") {
  hosts_table <- extract_table("Hosts and Sites Table", version = version)

  host_names <- unique(unlist(lapply(records, function(r) r$host_species)))
  host_names <- host_names[!is.na(host_names) & nzchar(host_names)]

  # --- Batch host-name reconciliation --------------------------------------
  #
  # Two malaviR interfaces answer this question and they do not agree. The function
  # match_taxonomy() returns "none" for 173 of MalAvi's 2,339 host binomials; the shipped
  # `taxonomy` dataset resolves every one of those 173 -- 169 as "reassigned:family", 1 as
  # "reassigned:order", 3 as "legacy". The function carries the genus-level reassignment
  # rule and not the family- or order-level ones that built the dataset.
  #
  # So the dataset is consulted for anything the function gives up on. Not instead of it:
  # the dataset only covers host names already in a MalAvi release, and a submission's
  # whole point is to bring names that are not. The function handles those; the dataset
  # repairs the ones it wrongly rejects.
  #
  # Found 2026-08-19, when the first real submission reported Grus leucogeranus as a
  # "possible misspelling or non-avian host". It is neither: it is a MalAvi host, and the
  # dataset maps it to Leucogeranus leucogeranus. See
  # malaviR/data-raw/TAXONOMY_CROSSWALK_GAPS.md.
  shipped <- tryCatch(malaviR::taxonomy, error = function(e) NULL)

  host_tax <- list()
  if (length(host_names)) {
    key <- tryCatch(match_taxonomy(host_names, version = version)$key,
                    error = function(e) NULL)
    if (!is.null(key)) {
      host_tax <- lapply(seq_len(nrow(key)), function(i) {
        mt   <- key$match_type[i]
        name <- key$malavi_species[i]
        ebird <- key$ebird_species[i]
        ord   <- key$order[i]
        fam   <- key$family[i]

        if (identical(mt, "none") && !is.null(shipped)) {
          hit <- shipped[!is.na(shipped$malavi_species) &
                           shipped$malavi_species == name, , drop = FALSE]
          if (nrow(hit) && !is.na(hit$ebird_species[1])) {
            ebird <- hit$ebird_species[1]
            ord   <- hit$order[1]
            fam   <- hit$family[1]
            mt    <- hit$match_type[1]
          }
        }

        flagged <- mt %in% c("none", "generic")
        reason <- if (identical(mt, "none"))
          "Host name did not reconcile to avian taxonomy (possible misspelling or non-avian host)."
        else if (identical(mt, "generic"))
          "Only the host genus could be matched; species-level name unresolved."
        else NA_character_
        list(host_species = name,
             ebird_species = ebird,
             order = ord, family = fam,
             match_type = mt, flagged = flagged, reason = reason)
      })
    }
  }

  # --- Per-record host/geography flags (only where a lineage is given) ------
  record_flags <- list()
  for (r in records) {
    lin <- r$lineage_name
    if (is.null(lin) || is.na(lin) || !nzchar(lin)) next
    res <- host_geo_flag(lin, host_species = r$host_species,
                         country = if (!is.null(r$country)) r$country else NULL,
                         version = version, hosts_table = hosts_table)
    for (f in res$flags) {
      record_flags[[length(record_flags) + 1]] <- list(
        host_species = r$host_species, lineage_name = lin,
        type = f$type, severity = f$severity, reason = f$reason)
    }
  }

  # --- Host and biogeographic plausibility ---------------------------------
  # What has been recorded before for this lineage, in this host, in this place.
  # Only rows carrying both a lineage and a host can be asked the question.
  plausibility <- list()
  askable <- Filter(function(r)
    !is.null(r$lineage_name) && nzchar(as.character(r$lineage_name)) &&
      !is.null(r$host_species) && nzchar(as.character(r$host_species)), records)
  if (length(askable)) {
    got <- tryCatch({
      as_chr <- function(field, default = NA_character_)
        vapply(askable, function(r) {
          v <- r[[field]]
          if (is.null(v) || !nzchar(as.character(v))) default else as.character(v)
        }, character(1))
      pl <- lineage_plausibility(
        lineage = as_chr("lineage_name"),
        host    = as_chr("host_species"),
        country = as_chr("country"),
        version = version)
      lapply(seq_len(nrow(pl)), function(i) {
        as.list(pl[i, c("lineage", "host", "country", "call", "flags",
                        "n_studies", "n_host_records", "n_countries",
                        "host_recorded", "host_family_recorded",
                        "country_recorded"), drop = FALSE])
      })
    }, error = function(e) {
      # Reported as a failed sub-check rather than taking the whole validation
      # down: the host-name reconciliation above is independently useful.
      list(list(error = paste("lineage_plausibility failed:", conditionMessage(e))))
    })
    plausibility <- got
  }

  list(host_taxonomy = host_tax, record_flags = record_flags,
       plausibility = plausibility)
}

#' Screen each submitted sequence with malaviR::lineage_qc().
#'
#' Returns one entry per sequence carrying the QC verdict and the numbers behind
#' it. Failures are reported per sequence, never raised: one unscreenable
#' sequence must not cost the curator the screen on all the others.
qc_sequences <- function(sequences, version = "latest") {
  out <- list()
  for (s in sequences) {
    seq_text <- s$sequence_clean
    label <- if (!is.null(s$lineage_name)) as.character(s$lineage_name) else NA_character_
    if (is.null(seq_text) || !nzchar(as.character(seq_text))) next
    entry <- tryCatch({
      # details = TRUE only so the chimera windows come back. The call itself is the
      # same either way; without them a "possible_chimera" verdict reaches the curator
      # as a bare assertion, and the evidence for it -- which stretch of the barcode
      # matches which lineage -- is exactly what makes it judgable.
      qc <- lineage_qc(as.character(seq_text), version = version, details = TRUE)
      summary_row <- as.list(qc$summary[1, , drop = FALSE])
      entry <- list(lineage_name = label,
           call = as.character(qc$call),
           score = as.numeric(qc$score),
           flags = paste(qc$flags, collapse = "; "),
           nearest_lineage = summary_row$nearest_lineage,
           nearest_distance = summary_row$nearest_distance,
           n_mutations = summary_row$n_mutations,
           n_nonsynonymous = summary_row$n_nonsynonymous,
           n_stop_codons = summary_row$n_stop_codons,
           message = if (!is.null(qc$message)) as.character(qc$message) else NULL)

      ch <- qc$chimera
      if (!is.null(ch) && !is.null(ch$windows) && nrow(ch$windows)) {
        w <- ch$windows
        # Collapse consecutive windows with the same best match into runs. Nineteen
        # overlapping window rows is the raw output; what a reader can hold is "this
        # stretch looks like X, that stretch looks like Y".
        runs <- list(); cur <- NULL
        for (i in seq_len(nrow(w))) {
          nm <- w$nearest_lineage[i]
          if (is.null(cur) || !identical(nm, cur$lineage)) {
            if (!is.null(cur)) runs[[length(runs) + 1]] <- cur
            cur <- list(lineage = nm, start = w$window_start[i],
                        end = w$window_end[i], dist = w$nearest_distance[i])
          } else {
            cur$end <- w$window_end[i]
            cur$dist <- min(cur$dist, w$nearest_distance[i])
          }
        }
        if (!is.null(cur)) runs[[length(runs) + 1]] <- cur
        entry$chimera_parent_switches <- as.integer(ch$parent_switches)
        entry$chimera_delta <- as.numeric(ch$chimera_delta)
        entry$chimera_best_single <- as.character(ch$best_single_lineage)
        entry$chimera_best_single_distance <- as.numeric(ch$best_single_distance)
        entry$chimera_runs <- lapply(runs, function(r)
          list(lineage = as.character(r$lineage), start = as.integer(r$start),
               end = as.integer(r$end), distance = as.numeric(r$dist)))
      }
      entry
    }, error = function(e) {
      list(lineage_name = label,
           error = paste("lineage_qc failed:", conditionMessage(e)))
    })
    out[[length(out) + 1]] <- entry
  }
  out
}

# --- CLI ---------------------------------------------------------------------
if (sys.nframe() == 0) {
  args <- commandArgs(trailingOnly = TRUE)
  con <- if (length(args) >= 1) args[1] else "stdin"
  payload <- jsonlite::fromJSON(con, simplifyVector = FALSE)
  version <- if (!is.null(payload$version)) payload$version else "latest"
  out <- validate_records(payload$records, version = version)
  # Sequences are present for template submissions and absent for PDF extraction,
  # which mines accessions rather than sequences. An absent list is not a failure.
  out$sequence_qc <- if (!is.null(payload$sequences))
    qc_sequences(payload$sequences, version = version) else list()
  out$malavi_version <- tryCatch(as.character(malavi_version()),
                                 error = function(e) NA_character_)
  cat(jsonlite::toJSON(out, auto_unbox = TRUE, pretty = TRUE, null = "null"), "\n")
}
