# =============================================================================
# lineage_plausibility.R
# -----------------------------------------------------------------------------
# Has this lineage been recorded in this host, in this place, before?
#
# Where lineage_qc() asks "is this sequence plausible?", this asks "is this
# lineage plausible in this host, in this place?" -- a different kind of
# evidence, drawn from MalAvi's own host records rather than from the sequence.
# The whole function is a set of lookups into the Hosts and Sites table plus the
# per-region columns of the Grand Lineage Summary. Nothing is modeled; the
# output is a description of what has been recorded before.
#
# WHY THIS LIVES HERE AND NOT IN malaviR
# --------------------------------------
# It was in malaviR (added dfb3866) and was removed on 2026-08-05 (941360a),
# because as an exported function it invited being run against the *curated*
# database and used as a reason not to examine real records. That misuse is the
# thing to prevent: every column here describes SAMPLING, not biology.
#
# Pre-ingest is different. A submission under review already has a curator's eye
# on it, and "MalAvi has no prior record of this lineage in this host family" is
# exactly the kind of triage signal that decides what a curator looks at first.
# So the capability lives in the curation repo, sourced by validate_record.R the
# same way host_geo_flag.R is, and is never shipped as a public malaviR verb.
#
# Read "novel" as "no prior record", never as "impossible", and never treat a
# flag on its own as grounds to discard a detection.
#
# Requires: library(malaviR) for extract_table(), clean_names(), lineage_studies()
# and malavi_version() -- all exported. Sourced by: curation/r/validate_record.R
# =============================================================================

# --- provenance helpers ------------------------------------------------------
# The recovered function used three of malaviR's unexported internals for this.
# Reaching into another package's internals with ::: is fragile -- they can be
# renamed in a patch release without warning, and this file is no longer shipped
# alongside them. They are five lines each, so they are simply defined here and
# the file depends on nothing but malaviR's public surface.

## Attach a compact provenance list as attr(x, "malavi_meta"), dropping NULLs.
## An attribute leaves names(), print.data.frame() and element access untouched.
.malavi_attach_meta <- function(x, ...) {
  meta <- list(...)
  meta <- meta[!vapply(meta, is.null, logical(1))]
  attr(x, "malavi_meta") <- meta
  x
}

## One-line rendering of that list for the print method, e.g. "MalAvi 2026-03-23".
.malavi_meta_line <- function(x) {
  meta <- attr(x, "malavi_meta")
  if (is.null(meta) || length(meta) == 0) return(NULL)
  bits <- character(0)
  if (!is.null(meta$malavi_version) && !is.na(meta$malavi_version))
    bits <- c(bits, paste0("MalAvi ", meta$malavi_version))
  if (!is.null(meta$method)) bits <- c(bits, paste0("method: ", meta$method))
  if (length(bits) == 0) return(NULL)
  paste(bits, collapse = "  |  ")
}

## Which bundled release the lookups actually read, for the report to state.
.malavi_resolve_version <- function(version = "latest") {
  resolved <- tryCatch(
    if (identical(version, "latest")) malavi_version() else version,
    error = function(e) NA_character_)
  if (length(resolved) != 1) NA_character_ else as.character(resolved)
}

## Host and biogeographic plausibility of a lineage detection. Where lineage_qc()
## asks "is this sequence plausible?", this asks "is this lineage plausible in
## this host, in this place?" -- a different kind of evidence, drawn from the
## MalAvi host records rather than from the sequence.
##
## The whole function is a set of lookups into the Hosts and Sites table plus the
## per-region columns of the Grand Lineage Summary. Nothing is modeled; the
## output is a description of what has been recorded before.

## The per-region presence columns of the Grand Lineage Summary. Listed
## explicitly because the table also holds non-region columns (PASSERIFORMES,
## SUM_*), and intersected with the real column names at use so that a release
## that drops or renames one degrades rather than crashes.
.malavi_region_columns <- c(
  "EUROPE", "SUB_SAHARAN_AFRICA", "NORTH_AFRICA_AND_MIDDLE_EAST",
  "NORTH_AMERICA", "HAWAI", "CENTRAL_AMERICA", "SOUTH_AMERICA", "ASIA",
  "AUSTRALIA_AND_NEW_ZEALAND", "OCEANIA", "ANTARCTICA", "UNKNOWN_REGION"
)

## Normalize a user-supplied region to a Grand Lineage Summary column name:
## upper case, non-alphanumerics to underscore, plus the handful of spellings
## people reliably get "wrong" because MalAvi's own spelling is unusual.
.malavi_normalize_region <- function(region) {
  norm <- toupper(trimws(as.character(region)))
  norm <- gsub("[^A-Z0-9]+", "_", norm)
  norm <- gsub("^_|_$", "", norm)
  alias <- c(HAWAII = "HAWAI",
             MIDDLE_EAST = "NORTH_AFRICA_AND_MIDDLE_EAST",
             NORTH_AFRICA = "NORTH_AFRICA_AND_MIDDLE_EAST",
             AFRICA = "SUB_SAHARAN_AFRICA",
             AUSTRALIA = "AUSTRALIA_AND_NEW_ZEALAND",
             NEW_ZEALAND = "AUSTRALIA_AND_NEW_ZEALAND")
  hit <- match(norm, names(alias))
  norm[!is.na(hit)] <- unname(alias[hit[!is.na(hit)]])
  norm
}

lineage_plausibility <- function(lineage, host = NULL, country = NULL,
                                 region = NULL, version = "latest") {
  lineage <- clean_names(as.character(lineage))
  n <- length(lineage)
  if (n == 0) stop("`lineage` is empty.", call. = FALSE)

  ## Recycle the optional arguments to the number of detections. Only length 1
  ## and length n are allowed: silent partial recycling would quietly pair the
  ## wrong host with the wrong lineage, which is the one mistake this function
  ## must not make.
  recycle <- function(x, nm) {
    if (is.null(x)) return(rep(NA_character_, n))
    x <- as.character(x)
    if (length(x) == 1L) return(rep(x, n))
    if (length(x) != n) {
      stop("`", nm, "` must be length 1 or the same length as `lineage` (",
           n, "), not ", length(x), ".", call. = FALSE)
    }
    x
  }
  host    <- recycle(host, "host")
  country <- recycle(country, "country")
  region  <- recycle(region, "region")

  hosts <- extract_table("Hosts and Sites Table", version = version)
  gls   <- extract_table("Grand Lineage Summary", version = version)
  st    <- lineage_studies(version = version)

  ## ---- what MalAvi has recorded for each lineage -------------------------
  ## Only the queried lineages matter, so cut the ~18k host records down to
  ## their rows before grouping. Indexing all ~5,000 lineages when the user
  ## asked about three is the difference between a second and ten.
  wanted   <- unique(stats::na.omit(lineage))
  rel_rows <- which(hosts$LINEAGE_NAME %in% wanted)
  rel      <- hosts[rel_rows, , drop = FALSE]

  ## Group the relevant records by lineage, then reduce each group to the sets
  ## of hosts, host taxa and countries it covers. Doing this per detection
  ## would rescan the records every time.
  by_lineage <- split(seq_len(nrow(rel)), rel$LINEAGE_NAME)

  set_of <- function(column) {
    lapply(by_lineage, function(i) unique(stats::na.omit(rel[[column]][i])))
  }
  host_species_of <- set_of("SPECIES_NAME")
  host_genus_of   <- set_of("GENUS_NAME")
  host_family_of  <- set_of("FAMILY_NAME")
  host_order_of   <- set_of("ORDER_NAME")
  country_of      <- set_of("COUNTRY_NAME")

  ## ---- host taxonomy, as MalAvi knows it ---------------------------------
  ## Family and order for a host species, taken from any record of that species
  ## anywhere in MalAvi (not just records of this lineage).
  host_key    <- hosts$SPECIES_NAME
  first_hit   <- match(host, host_key)
  host_family <- hosts$FAMILY_NAME[first_hit]
  host_order  <- hosts$ORDER_NAME[first_hit]
  ## Host genus is the first word of the binomial; that is how MalAvi's
  ## GENUS_NAME relates to its SPECIES_NAME, and it still works for a host that
  ## has never been sampled.
  host_genus  <- ifelse(is.na(host), NA_character_,
                        sub("^([^ ]+).*$", "\\1", trimws(host)))
  host_in_malavi <- !is.na(host) & !is.na(first_hit)

  ## ---- region presence, from the Grand Lineage Summary --------------------
  region_norm <- .malavi_normalize_region(region)
  region_cols <- intersect(.malavi_region_columns, names(gls))
  supplied    <- unique(stats::na.omit(region_norm))
  unknown     <- setdiff(supplied, region_cols)
  if (length(unknown) > 0) {
    stop("Unknown region: ", paste(unknown, collapse = ", "),
         ".\nValid regions: ", paste(region_cols, collapse = ", "),
         call. = FALSE)
  }
  ## the region columns are marked "1" where the lineage occurs and blank
  ## (NA) where it does not
  gls_lineage_row <- match(lineage, gls$LINEAGE_NAME)

  ## ---- assemble one row per detection -------------------------------------
  study_row <- match(lineage, st$lineage)
  in_malavi <- !is.na(study_row)

  ## logical helper: is `value` in the recorded set for this lineage?
  recorded_in <- function(sets, value) {
    vapply(seq_len(n), function(i) {
      if (is.na(value[i]) || is.na(lineage[i])) return(NA)
      s <- sets[[lineage[i]]]
      if (is.null(s)) return(FALSE)
      value[i] %in% s
    }, logical(1))
  }

  host_recorded        <- recorded_in(host_species_of, host)
  host_genus_recorded  <- recorded_in(host_genus_of,   host_genus)
  host_family_recorded <- recorded_in(host_family_of,  host_family)
  host_order_recorded  <- recorded_in(host_order_of,   host_order)
  country_recorded     <- recorded_in(country_of,      country)

  ## counts of records backing the host and country hits, over the relevant
  ## rows only (same reason as the grouping above)
  count_records <- function(column, value) {
    vapply(seq_len(n), function(i) {
      if (is.na(value[i])) return(NA_integer_)
      rows <- by_lineage[[lineage[i]]]
      if (is.null(rows)) return(0L)
      col <- rel[[column]][rows]
      sum(!is.na(col) & col == value[i])
    }, integer(1))
  }
  n_records_host    <- count_records("SPECIES_NAME", host)
  n_records_country <- count_records("COUNTRY_NAME", country)

  region_recorded <- vapply(seq_len(n), function(i) {
    if (is.na(region_norm[i]) || is.na(gls_lineage_row[i])) return(NA)
    val <- gls[[region_norm[i]]][gls_lineage_row[i]]
    !is.na(val) && nzchar(trimws(as.character(val)))
  }, logical(1))

  ## ---- flags and the one-word call ---------------------------------------
  flags <- vector("list", n)
  call  <- character(n)
  for (i in seq_len(n)) {
    f <- character(0)

    if (!is.na(host[i]) && !host_in_malavi[i]) f <- c(f, "host_not_in_malavi")

    if (!in_malavi[i]) {
      ## A lineage with no host record at all is trivially new in every host
      ## and every place, so the novelty flags would say nothing. Report only
      ## that it is unknown. (The logical columns still record FALSE, which is
      ## factually what the lookup found.)
      f <- c(f, "lineage_not_in_malavi")
    } else {
      if (isTRUE(st$n_studies[study_row[i]] == 1)) f <- c(f, "single_study_lineage")
      if (isFALSE(host_recorded[i]))        f <- c(f, "new_host_species")
      if (isFALSE(host_genus_recorded[i]))  f <- c(f, "new_host_genus")
      if (isFALSE(host_family_recorded[i])) f <- c(f, "new_host_family")
      if (isFALSE(host_order_recorded[i]))  f <- c(f, "new_host_order")
      if (isFALSE(country_recorded[i]))     f <- c(f, "new_country")
      if (isFALSE(region_recorded[i]))      f <- c(f, "new_region")
    }

    ## The call collapses the flags into the single distinction a user acts on:
    ## is the host new, is the place new, are both.
    new_host_any <- isFALSE(host_recorded[i])
    new_place    <- isFALSE(country_recorded[i]) || isFALSE(region_recorded[i])
    call[i] <-
      if (!in_malavi[i])                          "lineage_not_in_malavi"
      else if (isFALSE(host_family_recorded[i]))  "new_host_family"
      else if (new_host_any && new_place)         "new_host_and_location"
      else if (new_host_any)                      "new_host"
      else if (new_place)                         "new_location"
      else                                        "previously_recorded"

    flags[[i]] <- f
  }

  out <- data.frame(
    lineage              = lineage,
    call                 = call,
    flags                = vapply(flags, paste, character(1), collapse = "; "),
    n_studies            = st$n_studies[study_row],
    n_host_records       = st$n_host_records[study_row],
    n_countries          = st$n_countries[study_row],
    host                 = host,
    host_family          = host_family,
    host_order           = host_order,
    host_recorded        = host_recorded,
    host_genus_recorded  = host_genus_recorded,
    host_family_recorded = host_family_recorded,
    host_order_recorded  = host_order_recorded,
    n_records_host       = n_records_host,
    country              = country,
    country_recorded     = country_recorded,
    n_records_country    = n_records_country,
    region               = region_norm,
    region_recorded      = region_recorded,
    stringsAsFactors     = FALSE
  )
  rownames(out) <- NULL

  out <- .malavi_attach_meta(out, malavi_version = .malavi_resolve_version(version))
  class(out) <- c("malavi_plausibility", class(out))
  out
}

print.malavi_plausibility <- function(x, ...) {
  cat("MalAvi host and biogeographic plausibility\n")
  meta <- .malavi_meta_line(x)
  if (!is.null(meta)) cat("  ", meta, "\n", sep = "")
  cat("  Novelty here means no prior record, not implausibility: MalAvi\n")
  cat("  records where people have looked. Weigh it with read abundance.\n\n")

  ## a compact view; the full table is still there as a data frame
  cols <- c("lineage", "call", "n_studies", "host", "host_recorded",
            "country", "country_recorded", "region", "region_recorded")
  print(as.data.frame(x)[, intersect(cols, names(x)), drop = FALSE])

  if (any(nzchar(x$flags))) {
    cat("\nFlags:\n")
    for (i in which(nzchar(x$flags))) {
      cat("  ", x$lineage[i], ": ", x$flags[i], "\n", sep = "")
    }
  }
  invisible(x)
}
