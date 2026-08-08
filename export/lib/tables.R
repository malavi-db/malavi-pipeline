# @title Shared accessor for the six MalAvi tables the website publishes
# @purpose Return every table the site exposes, keyed by the id the website uses,
#          so the statistics payload and the downloadable files are built from
#          one definition instead of two that can drift apart.
# @why build_site_stats.R reports a row count for each table and
#      build_downloads.R writes each table to CSV/XLSX. If each derived the
#      tables independently, a change to one (in particular the derived Table of
#      Lineage Names) would silently make the advertised row count disagree with
#      the file a user downloads.
# @input /mnt/ellisbiostore/malaviR (bundled release, via malaviR::extract_table)
# @output (none; sourced by other export scripts)
# @program R
# @program malaviR
# @critical-var MALAVI_TABLE_IDS

# The MalAvi release's own tables, in the order the site lists them. This is
# what the "Everything" archive means by a release, so a table only belongs here
# if it came out of the release itself.
MALAVI_TABLE_IDS <- c("hosts_and_sites", "grand_lineage_summary", "references",
                      "morpho_species", "vector_data", "lineage_names")

# Tables the site browses that are NOT part of the MalAvi release. The host
# taxonomy key is malaviR's, built by matching MalAvi's host names against the
# eBird/Clements taxonomy that the clootl phylogeny uses -- useful enough to
# publish, but not MalAvi data, so it stays out of the release archive.
DERIVED_TABLE_IDS <- c("taxonomy")

# Everything the site browses, release and derived alike.
SITE_TABLE_IDS <- c(MALAVI_TABLE_IDS, DERIVED_TABLE_IDS)

#' Build the Table of Lineage Names.
#'
#' malaviR does not ship this as its own table. MalAvi packs the names a paper
#' originally used into the hosts table's ALT_NAME column, comma-separated, so
#' one host row can carry several synonyms. Unpack it to one row per
#' (lineage, alternative name, reference).
malavi_lineage_names_table <- function(hosts) {
  alt <- hosts[!is.na(hosts$ALT_NAME) & hosts$ALT_NAME != "",
               c("LINEAGE_NAME", "ALT_NAME", "REFERENCE_NAME")]
  split_names <- strsplit(alt$ALT_NAME, "\\s*,\\s*")
  out <- data.frame(
    LINEAGE_NAME   = rep(alt$LINEAGE_NAME,   lengths(split_names)),
    ALT_NAME       = unlist(split_names),
    REFERENCE_NAME = rep(alt$REFERENCE_NAME, lengths(split_names)),
    stringsAsFactors = FALSE
  )
  unique(out[out$ALT_NAME != "", ])
}

#' Every table of one MalAvi release, as a named list of data frames.
#'
#' Names are the website's table ids, so a caller can go straight from an id in
#' site_stats.json to the data behind it.
malavi_tables <- function(release) {
  hosts <- extract_table("Hosts and Sites Table",  version = release)
  list(
    hosts_and_sites       = hosts,
    grand_lineage_summary = extract_table("Grand Lineage Summary",  version = release),
    references            = extract_table("Table of References",    version = release),
    morpho_species        = extract_table("Morpho Species Summary", version = release),
    vector_data           = extract_table("Vector Data Table",      version = release),
    lineage_names         = malavi_lineage_names_table(hosts)
  )
}

#' The host taxonomy key that malaviR ships as package data.
#'
#' One row per distinct host species in MalAvi, matched to the eBird/Clements
#' name used by the clootl avian phylogeny (McTavish et al. 2025), with the
#' order and family that follow from the match and a record of how each name
#' was matched.
#'
#' It is checked against the release rather than trusted: malaviR builds this
#' key from the release it bundles, so if the bundled data ever moves out from
#' under the pinned release the two stop agreeing, and that must fail here
#' rather than be published as a key with rows for hosts MalAvi does not have.
malavi_taxonomy_table <- function(release) {
  taxonomy <- get(utils::data("taxonomy", package = "malaviR",
                              envir = environment()))

  hosts <- extract_table("Hosts and Sites Table", version = release)
  host_species <- unique(hosts$SPECIES_NAME[!is.na(hosts$SPECIES_NAME) &
                                              hosts$SPECIES_NAME != ""])

  only_in_key     <- setdiff(taxonomy$malavi_species, host_species)
  only_in_release <- setdiff(host_species, taxonomy$malavi_species)
  if (length(only_in_key) || length(only_in_release)) {
    stop(sprintf(paste0("malaviR's taxonomy key does not match release %s: ",
                        "%d species only in the key (e.g. %s), ",
                        "%d only in the release (e.g. %s). ",
                        "Rebuild the key with malaviR::match_taxonomy() ",
                        "or re-pin the release."),
                 release,
                 length(only_in_key), paste(utils::head(only_in_key, 3), collapse = ", "),
                 length(only_in_release), paste(utils::head(only_in_release, 3), collapse = ", ")))
  }

  taxonomy[order(taxonomy$malavi_species), ]
}

#' Every table the website browses: the release's own, plus the derived ones.
site_tables <- function(release) {
  c(malavi_tables(release), list(taxonomy = malavi_taxonomy_table(release)))
}
