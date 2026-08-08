#!/usr/bin/env Rscript
# =============================================================================
# host_geo_flag.R
# -----------------------------------------------------------------------------
# Flag improbable host or locality records for a lineage -- a capability that does
# NOT yet exist in malaviR. Example: a lineage that is otherwise a Passeriformes
# specialist turning up in a frigatebird (Suliformes) is more likely lab
# cross-contamination than a real host shift, and deserves a curator's eyes.
#
# Granularity: host ORDER is the key signal. ~93% of MalAvi lineages are recorded
# from a single host order, so a record outside the lineage's known order(s) is
# the informative anomaly; severity is graded by how specialized the lineage is.
# Family- and country-level novelty are reported as softer notes.
#
# Once validated here the intent is to UPSTREAM this into malaviR as an exported
# function so the check lives with the rest of the validation suite.
#
# Sourceable (defines host_geo_flag / host_geo_flag_profile) or run as a CLI:
#   Rscript curation/r/host_geo_flag.R <lineage> <host species> [country]
# =============================================================================

suppressMessages(library(malaviR))

# Build a lineage's observed host/geography profile from the Hosts and Sites table.
host_geo_flag_profile <- function(lineage_name, hosts_table) {
  prof <- hosts_table[hosts_table$LINEAGE_NAME == lineage_name, ]
  list(
    n_records   = nrow(prof),
    orders      = sort(unique(prof$ORDER_NAME[nzchar(prof$ORDER_NAME)])),
    families    = sort(unique(prof$FAMILY_NAME[nzchar(prof$FAMILY_NAME)])),
    host_species = sort(unique(trimws(paste(prof$GENUS_NAME, prof$SPECIES_NAME)))),
    countries   = sort(unique(prof$COUNTRY_NAME[nzchar(prof$COUNTRY_NAME)]))
  )
}

# Flag a candidate (lineage, host, locality) against the lineage's known profile.
#
# Returns a list: $known_lineage, profile summary, and $flags (each a list with
# type / severity / reason). severity in {info, warn, strong}.
host_geo_flag <- function(lineage_name, host_species = NULL, country = NULL,
                          version = "latest", hosts_table = NULL) {
  if (is.null(hosts_table)) {
    hosts_table <- extract_table("Hosts and Sites Table", version = version)
  }
  flags <- list()
  add_flag <- function(type, severity, reason) {
    flags[[length(flags) + 1]] <<- list(type = type, severity = severity, reason = reason)
  }

  prof <- host_geo_flag_profile(lineage_name, hosts_table)

  # Unknown lineage: nothing to profile against (a genuinely new lineage).
  if (prof$n_records == 0) {
    add_flag("unknown_lineage", "info",
             sprintf("Lineage '%s' is not yet in MalAvi; host range cannot be profiled.",
                     lineage_name))
    return(list(lineage = lineage_name, host = host_species, known_lineage = FALSE,
                flags = flags))
  }

  # --- Host plausibility ----------------------------------------------------
  if (!is.null(host_species) && nzchar(host_species)) {
    if (host_species %in% prof$host_species) {
      # Already a recorded host for this lineage -> nothing to flag.
    } else {
      # Reconcile the new host to avian taxonomy to get its order/family.
      tax <- tryCatch(match_taxonomy(host_species, version = version),
                      error = function(e) NULL)
      new_order <- NA_character_; new_family <- NA_character_; mt <- "none"
      if (!is.null(tax) && nrow(tax$key) >= 1) {
        # eBird/clootl names carry a parenthetical gloss, e.g.
        # "Fregatidae (Frigatebirds)"; MalAvi's ORDER_NAME/FAMILY_NAME are bare.
        # Strip the gloss so the two vocabularies compare on equal terms.
        strip_gloss <- function(x) trimws(sub("\\s*\\(.*$", "", x))
        new_order  <- strip_gloss(tax$key$order[1])
        new_family <- strip_gloss(tax$key$family[1])
        mt         <- tax$key$match_type[1]
      }

      if (is.na(new_order) || identical(mt, "none")) {
        add_flag("unrecognized_host", "warn",
                 sprintf("Host '%s' did not reconcile to known avian taxonomy (possible misspelling or non-avian host).",
                         host_species))
      } else if (new_family %in% prof$families) {
        # Same family as a known host -> plausible; no flag.
      } else if (new_order %in% prof$orders) {
        add_flag("host_family_novel", "info",
                 sprintf("New host family (%s), but order %s is within the lineage's known host range.",
                         new_family, new_order))
      } else {
        # Host order outside the lineage's known range -> grade by specialization.
        n_ord <- length(prof$orders)
        severity <- if (n_ord <= 1) "strong" else if (n_ord <= 3) "warn" else "info"
        add_flag("host_order_outlier", severity,
                 sprintf("Lineage recorded from order(s) %s across %d host species; this record is %s (%s) -- outside the known host range%s.",
                         paste(prof$orders, collapse = "/"), length(prof$host_species),
                         new_order, new_family,
                         if (severity == "strong") " -- possible cross-contamination" else ""))
      }
    }
  }

  # --- Geography novelty (soft) ---------------------------------------------
  if (!is.null(country) && nzchar(country) && !(country %in% prof$countries)) {
    add_flag("geography_novel", "info",
             sprintf("Country '%s' is not among the lineage's %d recorded countries.",
                     country, length(prof$countries)))
  }

  list(lineage = lineage_name, host = host_species, known_lineage = TRUE,
       known_orders = prof$orders, n_host_species = length(prof$host_species),
       flags = flags)
}

# --- CLI ---------------------------------------------------------------------
if (sys.nframe() == 0) {
  if (!requireNamespace("jsonlite", quietly = TRUE))
    stop("jsonlite is required for the CLI.")
  a <- commandArgs(trailingOnly = TRUE)
  if (length(a) < 2)
    stop("Usage: host_geo_flag.R <lineage> <host species> [country]")
  res <- host_geo_flag(a[1], a[2], if (length(a) >= 3) a[3] else NULL)
  cat(jsonlite::toJSON(res, auto_unbox = TRUE, pretty = TRUE), "\n")
}
