#!/usr/bin/env Rscript
# @title Build the website's statistics payload from the pinned malaviR release
# @purpose Derive every figure the MalAvi website displays -- totals, per-genus
#          breakdowns, host-taxon reach, per-country study counts, table row
#          counts -- and write them to docs/assets/data/site_stats.json.
# @why The site must be deterministic: when a new MalAvi release is bundled, every
#      number on every page has to update from that release, with nothing typed
#      into the HTML by hand. This script is the single source of those numbers.
# @input /mnt/ellisbiostore/malaviR (bundled release, via malaviR::extract_table)
# @input config/project.yml
# @input config/country_map_aliases.json
# @output docs/assets/data/site_stats.json
# @program Rscript
# @program malaviR
# @program jsonlite
# @critical-var release
# @critical-var GENERA
# @critical-var NON_COUNTRY
# =============================================================================
# Usage:
#   Rscript export/build_site_stats.R              # write the payload
#   Rscript export/build_site_stats.R --dry-run    # compute and print, write nothing
#
# The website reads the emitted JSON at load and populates the DOM from it. No
# page contains a hard-coded count. Bump `malaviR.release` in config/project.yml,
# re-run this, and the site follows.
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

# Shared table accessor: defines functions only, no side effects. Sourced for
# malavi_taxonomy_table(), which carries the check that malaviR's taxonomy key
# and the pinned release still describe the same host species.
source(file.path(script_dir, "lib", "tables.R"))

trailing <- commandArgs(trailingOnly = TRUE)
dry_run <- "--dry-run" %in% trailing

# Resolve the pinned release; "latest" defers to whatever malaviR bundles.
release <- cfg$malaviR$release
if (identical(release, "latest")) release <- malavi_version()

cat("== malavi_rebuild :: build_site_stats ==\n")
cat("release :", release, "\n\n")

# ---- pull the tables --------------------------------------------------------
hosts     <- extract_table("Hosts and Sites Table",  version = release)
lineages  <- extract_table("Grand Lineage Summary",  version = release)
morpho    <- extract_table("Morpho Species Summary", version = release)
refs      <- extract_table("Table of References",    version = release)
vectors   <- extract_table("Vector Data Table",      version = release)

# The three parasite genera the site reports on, in the fixed display order the
# website's categorical colors are assigned in. Anything outside this set (blank
# or "N/A" genus) is counted as unassigned rather than silently dropped.
GENERA <- c("Plasmodium", "Haemoproteus", "Leucocytozoon")

# Host binomial, used wherever "host species" is counted. Built once so every
# figure below counts the same thing.
hosts$BINOMIAL <- trimws(paste(hosts$GENUS_NAME, hosts$SPECIES_NAME))

# Helper: distinct non-missing values in a column.
n_distinct <- function(x) length(unique(x[!is.na(x) & x != ""]))

# MalAvi records a locality-unknown placeholder in COUNTRY_NAME. It is not a
# country and must never be counted as one -- the choropleth already drops it,
# so counting it in the headline made the map caption disagree with the strip
# ("N of the 124 countries shown", where N + unplaced only ever reached 123).
# Every country figure on the site goes through this one helper.
NON_COUNTRY <- c("Unknown Country")
n_countries <- function(x) length(setdiff(unique(x[!is.na(x) & x != ""]), NON_COUNTRY))

# ---- headline totals --------------------------------------------------------
# Counts here are of *things*, not of table rows: the Grand Lineage Summary can
# carry a lineage on more than one row (one row per morphospecies link), so the
# lineage headline counts distinct names. Row counts live in `tables` below,
# where they describe the download file rather than the biology.
totals <- list(
  lineages       = n_distinct(lineages$LINEAGE_NAME),
  host_records   = nrow(hosts),
  host_species   = n_distinct(hosts$BINOMIAL),
  host_genera    = n_distinct(hosts$GENUS_NAME),
  host_families  = n_distinct(hosts$FAMILY_NAME),
  host_orders    = n_distinct(hosts$ORDER_NAME),
  # Countries anywhere in the database, so this stays the denominator the map's
  # "N of the M countries are shown" note divides into. The map counts vector
  # screening as well as host records, so a country known only from a vector
  # study still has to be in M. (On the 2026-03-23 release the vector table adds
  # no country the host records lack, so this is 123 either way.)
  countries      = n_countries(c(hosts$COUNTRY_NAME, vectors$COUNTRY_NAME)),
  references     = nrow(refs),
  # Distinct described species, and the number of lineage-to-species links that
  # connect them. These differ (many lineages can map to one morphospecies), so
  # both are emitted under names that say which is which.
  morpho_species = n_distinct(morpho$SPECIES_NAME),
  morpho_links   = nrow(morpho),
  vector_records = nrow(vectors),
  vector_species = n_distinct(vectors$VECTOR_SPECIES)
)

# ---- parasite genus for every lineage ---------------------------------------
# The genus of a lineage is a property of the LINEAGE, so it must be read from
# the Grand Lineage Summary, not inferred from whether some host record happens
# to mention it.
#
# This distinction is not cosmetic. Deriving the genus from Hosts and Sites (the
# way this script previously did) silently drops every lineage that has no host
# record at all: vector-only detections and GenBank-only submissions. On the
# 2026-03-23 release that was 614 lineages -- reported as "unassigned" even
# though the Grand Lineage Summary names their genus outright.
#
# Fallback: a handful of rows carry GENUS_NAME "N/A" but still name a
# morphospecies in SPECIES_NAME ("Haemoproteus nisi"). The genus is the first
# word of that binomial, so recover it rather than throwing the lineage away.
# Only lineages with neither field remain unassigned.
lineage_genus_of <- function(genus_name, species_name) {
  genus_name  <- trimws(as.character(genus_name))
  species_name <- trimws(as.character(species_name))
  usable <- !is.na(genus_name) & genus_name %in% GENERA
  # First word of the morphospecies binomial, where there is one.
  from_species <- sub("\\s.*$", "", ifelse(is.na(species_name), "", species_name))
  ifelse(usable, genus_name,
         ifelse(from_species %in% GENERA, from_species, NA_character_))
}

# One row per distinct lineage, so a lineage linked to several morphospecies is
# not counted more than once.
lineage_first <- lineages[!duplicated(lineages$LINEAGE_NAME), ]
lineage_genus <- lineage_genus_of(lineage_first$GENUS_NAME, lineage_first$SPECIES_NAME)
names(lineage_genus) <- lineage_first$LINEAGE_NAME

# ---- per-genus breakdown ----------------------------------------------------
# One entry per parasite genus, carrying everything the home page shows about it:
# the donut value (lineages), the composition bar (records), and the host-reach
# bars (species / genera / families / orders).
#
# `lineages` comes from the lineage table (above); everything else is a
# host-reach figure and must keep coming from the host records, since a lineage
# with no host record contributes no hosts, countries or studies by definition.
genus_rows <- lapply(GENERA, function(gen) {
  slice <- hosts[!is.na(hosts$PARASITE_GENUS) & hosts$PARASITE_GENUS == gen, ]
  list(
    key           = gen,
    lineages      = sum(lineage_genus == gen, na.rm = TRUE),
    records       = nrow(slice),
    host_species  = n_distinct(slice$BINOMIAL),
    host_genera   = n_distinct(slice$GENUS_NAME),
    host_families = n_distinct(slice$FAMILY_NAME),
    host_orders   = n_distinct(slice$ORDER_NAME),
    countries     = n_countries(slice$COUNTRY_NAME),
    studies       = n_distinct(slice$REFERENCE_NAME)
  )
})
names(genus_rows) <- GENERA

# Lineages whose genus is recorded nowhere: neither GENUS_NAME nor a
# morphospecies binomial. The site states this explicitly rather than letting
# the donuts silently not sum.
unassigned_lineages <- sum(is.na(lineage_genus))

# ---- per-country study counts, per genus ------------------------------------
# "Studies" is distinct references, not records: a single large paper should not
# outweigh ten small ones on the map.
#
# A study counts for a country if it reported that genus there in EITHER the host
# records or the vector screening data. The map's caption is "studies per
# country", not "host studies per country", and counting host records alone left
# 54 country-by-genus study counts short on the 2026-03-23 release -- Thailand's
# Leucocytozoon count was half what the release supports (4 of 8), because those
# studies screened vectors rather than birds.
#
# The vector table carries no PARASITE_GENUS column, so the genus of a vector row
# is the genus of the lineage it reports, via the lineage -> genus lookup above.

# Reference names are compared as text, so collapse internal whitespace before
# treating one as a study's identity. The release carries at least one name that
# differs from another only by a trailing newline ("Pramual et al 2020"), which
# would otherwise be counted as two separate studies.
normalize_ref <- function(x) gsub("[[:space:]]+", " ", trimws(as.character(x)))

# Long form, one row per (country, genus, reference) claim, from both sources.
# Building both sources into the same shape means the counting below happens once
# and cannot drift between them.
host_claims <- data.frame(
  country   = trimws(as.character(hosts$COUNTRY_NAME)),
  genus     = trimws(as.character(hosts$PARASITE_GENUS)),
  reference = normalize_ref(hosts$REFERENCE_NAME),
  stringsAsFactors = FALSE
)
vector_claims <- data.frame(
  country   = trimws(as.character(vectors$COUNTRY_NAME)),
  # Unnamed so rbind() below does not inherit lineage names as row names.
  genus     = unname(lineage_genus[trimws(as.character(vectors$LINEAGE_NAME))]),
  reference = normalize_ref(vectors$REFERENCE_NAME),
  stringsAsFactors = FALSE
)

# Drop anything that cannot be placed on the map: no country, the
# locality-unknown placeholder, a genus outside the three the site reports on (or
# a vector row whose lineage has no genus), or no reference to count as a study.
claims <- rbind(host_claims, vector_claims)
claims <- claims[!is.na(claims$country)   & claims$country != "" &
                 !claims$country %in% NON_COUNTRY &
                 !is.na(claims$genus)     & claims$genus %in% GENERA &
                 !is.na(claims$reference) & claims$reference != "", ]
# One row per distinct claim, so a row count IS a distinct-study count below.
claims <- unique(claims)

country_studies <- list()
for (country in sort(unique(claims$country))) {
  per_genus <- list()
  for (gen in GENERA) {
    n <- sum(claims$country == country & claims$genus == gen)
    # Keyed by first initial (P/H/L) to keep the payload small; the site expands it.
    if (n > 0) per_genus[[substr(gen, 1, 1)]] <- n
  }
  if (length(per_genus)) country_studies[[country]] <- per_genus
}

# ---- warn about countries the basemap cannot place --------------------------
# The choropleth basemap is a fixed asset; only the counts above change per
# release. If a release introduces a country the basemap has no shape for, that
# must surface here rather than vanishing from the map silently.
#
# The alias table is a build input, not a website asset: the browser never
# fetches it, only this script reads it. It therefore lives in config/ rather
# than docs/assets/data/, so that nothing is published that the site does not
# actually use.
alias_path <- file.path(repo_root, "config", "country_map_aliases.json")
unplaced <- character(0)
if (file.exists(alias_path)) {
  aliases <- jsonlite::read_json(alias_path, simplifyVector = TRUE)
  known <- union(aliases$atlas_names, names(aliases$aliases))
  unplaced <- setdiff(names(country_studies), known)
  if (length(unplaced)) {
    cat("NOTE: ", length(unplaced), " countries have records but no shape on the basemap:\n", sep = "")
    cat(paste0("  - ", unplaced, collapse = "\n"), "\n\n", sep = "")
    cat("  These appear in the tables but not the map. Add an alias to\n")
    cat("  config/country_map_aliases.json if the basemap does have them\n")
    cat("  under a different name.\n\n")
  }
} else {
  cat("NOTE: ", basename(alias_path), " not found -- skipping basemap coverage check.\n\n", sep = "")
}

# ---- what the choropleth can actually show ----------------------------------
# The countries above that the basemap cannot place do not just lose their shape:
# any study reported ONLY from them disappears from the map entirely. The site
# has to be able to say so, and the per-panel caption has to be able to state the
# real number of countries with records rather than the number of shapes it
# managed to shade. Both figures are therefore computed here, from the same
# claims table the map counts come from, instead of being inferred in the browser
# from whatever happened to get drawn.
placed_countries <- setdiff(names(country_studies), unplaced)
for (gen in GENERA) {
  in_genus <- claims[claims$genus == gen, ]
  shown    <- in_genus[in_genus$country %in% placed_countries, ]
  genus_rows[[gen]]$map_countries       <- length(unique(in_genus$country))
  genus_rows[[gen]]$map_countries_shown <- length(unique(shown$country))
  genus_rows[[gen]]$map_studies         <- length(unique(in_genus$reference))
  genus_rows[[gen]]$map_studies_shown   <- length(unique(shown$reference))
}
# Across all three genera: a study is "shown" if any country it reported from has
# a shape on the basemap. Counted over distinct references, not summed across
# genera or countries, since one study can appear in several of both.
map_studies       <- length(unique(claims$reference))
map_studies_shown <- length(unique(claims$reference[claims$country %in% placed_countries]))

# ---- table metadata ---------------------------------------------------------
# Row counts come from the tables themselves so the "N rows" label on the site
# can never drift from the data behind the download link.
# `group` decides which block of the tables page a table is listed under:
# "release" for MalAvi's own data, "derived" for anything built on top of it.
# The page carries the headings; the data decides the membership.
table_meta <- list(
  list(id = "hosts_and_sites",       title = "Hosts and Sites", group = "release",
       rows = nrow(hosts),    description = cfg$tables$hosts_and_sites$description),
  list(id = "grand_lineage_summary", title = "Grand Lineage Summary", group = "release",
       rows = nrow(lineages), description = cfg$tables$grand_lineage_summary$description),
  list(id = "references",            title = "Table of References", group = "release",
       rows = nrow(refs),     description = cfg$tables$references$description),
  list(id = "morpho_species",        title = "Morpho Species Summary", group = "release",
       rows = nrow(morpho),   description = cfg$tables$morpho_species$description),
  list(id = "vector_data",           title = "Vector Data", group = "release",
       rows = nrow(vectors),  description = cfg$tables$vector_data$description)
)

# The Table of Lineage Names is not shipped by malaviR as its own table; it is
# derived here from the ALT_NAME column, which packs synonyms comma-separated.
alt <- hosts[!is.na(hosts$ALT_NAME) & hosts$ALT_NAME != "",
             c("LINEAGE_NAME", "ALT_NAME", "REFERENCE_NAME")]
split_names <- strsplit(alt$ALT_NAME, "\\s*,\\s*")
lineage_names <- data.frame(
  LINEAGE_NAME   = rep(alt$LINEAGE_NAME,   lengths(split_names)),
  ALT_NAME       = unlist(split_names),
  REFERENCE_NAME = rep(alt$REFERENCE_NAME, lengths(split_names)),
  stringsAsFactors = FALSE
)
lineage_names <- unique(lineage_names[lineage_names$ALT_NAME != "", ])

# Unpacked from a MalAvi column rather than supplied as its own table, but it is
# still MalAvi's data, so it belongs with the release group.
table_meta[[length(table_meta) + 1]] <- list(
  id = "lineage_names", title = "Table of Lineage Names", group = "release",
  rows = nrow(lineage_names),
  description = paste("MalAvi lineage names against the names used in the original",
                      "publications. Derived from the ALT_NAME column of the hosts table.")
)

# malaviR's host taxonomy key, listed last because it is the one table here that
# is not MalAvi's own. malavi_taxonomy_table() refuses to build it if the key and
# the pinned release ever stop describing the same set of host species.
taxonomy_key <- malavi_taxonomy_table(release)
table_meta[[length(table_meta) + 1]] <- list(
  id = "taxonomy", title = "Host taxonomy key", group = "derived",
  rows = nrow(taxonomy_key),
  description = cfg$tables$taxonomy$description
)

# What the taxonomy key was matched against. Both of these move on their own
# schedule -- the eBird/Clements taxonomy is revised every year, and malaviR can
# rebuild the key without the MalAvi release changing at all -- so the page has
# to state them rather than leave a visitor to assume the release date covers
# them too. Read from the package, never typed here.
taxonomy_source <- list(
  clootl_year     = clootl_taxonomy_version(),
  malaviR_version = as.character(utils::packageVersion("malaviR"))
)
cat("\ntaxonomy key    : eBird/Clements", taxonomy_source$clootl_year,
    "via clootl, built by malaviR", taxonomy_source$malaviR_version, "\n")

# ---- release integrity checks ----------------------------------------------
# Signals that a headline figure would misstate the release. These are reported
# every run and carried in the payload, so a problem in a future release shows
# up here instead of on the home page. None of them stop the build: they are
# properties of the upstream MalAvi release, which we report rather than edit.
integrity <- list()

# (1) A lineage carried on more than one Grand Lineage Summary row -- typically
#     one row per morphospecies the lineage is linked to. Row count would
#     therefore overstate the number of lineages.
dup_counts     <- table(lineages$LINEAGE_NAME)
duplicated_gls <- sort(names(dup_counts)[dup_counts > 1])
integrity$grand_lineage_summary_rows      <- nrow(lineages)
integrity$grand_lineage_summary_lineages  <- totals$lineages
# I() keeps this a JSON array even when a single lineage is duplicated, which
# auto_unbox would otherwise collapse to a bare string.
integrity$lineages_on_multiple_rows       <- I(duplicated_gls)

# (2) Reference names used by host records that have no entry in the References
#     table (mostly unpublished submissions), and entries never cited by any
#     record. Both are expected to be non-zero; a sharp change is worth a look.
refs_cited     <- setdiff(unique(hosts$REFERENCE_NAME[!is.na(hosts$REFERENCE_NAME) &
                                                      hosts$REFERENCE_NAME != ""]), NA)
integrity$references_table_rows     <- nrow(refs)
integrity$reference_names_in_hosts  <- length(refs_cited)
integrity$cited_but_not_in_refs     <- length(setdiff(refs_cited, refs$REFERENCE_NAME))
integrity$in_refs_but_never_cited   <- length(setdiff(refs$REFERENCE_NAME, refs_cited))
integrity$host_records_missing_ref  <- sum(is.na(hosts$REFERENCE_NAME) | hosts$REFERENCE_NAME == "")

# (3) Host records placed in the locality-unknown bucket, excluded from every
#     country figure and from the map.
integrity$host_records_unknown_country <- sum(hosts$COUNTRY_NAME %in% NON_COUNTRY, na.rm = TRUE)

# (4) Vector records the map cannot count: no usable country, or a lineage whose
#     genus cannot be resolved from the Grand Lineage Summary. Both are zero on
#     the 2026-03-23 release; a future release that breaks either link would
#     otherwise drop those studies from the map without saying so.
integrity$vector_records_unknown_country <- sum(is.na(vector_claims$country) |
                                                vector_claims$country == "" |
                                                vector_claims$country %in% NON_COUNTRY)
integrity$vector_records_no_genus <- sum(is.na(vector_claims$genus))

cat("\nrelease integrity:\n")
cat(sprintf("  grand lineage summary   %d rows -> %d distinct lineages\n",
            integrity$grand_lineage_summary_rows, integrity$grand_lineage_summary_lineages))
if (length(duplicated_gls)) {
  cat(sprintf("    lineage(s) on >1 row: %s\n", paste(duplicated_gls, collapse = ", ")))
}
cat(sprintf("  references              %d in table, %d names cited by records\n",
            integrity$references_table_rows, integrity$reference_names_in_hosts))
cat(sprintf("    cited but not in the references table: %d\n", integrity$cited_but_not_in_refs))
cat(sprintf("    in the table but never cited:          %d\n", integrity$in_refs_but_never_cited))
cat(sprintf("    host records with no reference name:   %d\n", integrity$host_records_missing_ref))
cat(sprintf("  host records with unknown country:       %d (excluded from country counts)\n",
            integrity$host_records_unknown_country))
cat(sprintf("  vector records with unknown country:     %d (excluded from the map)\n",
            integrity$vector_records_unknown_country))
cat(sprintf("  vector records with unresolved genus:    %d (excluded from the map)\n",
            integrity$vector_records_no_genus))

# ---- known issues in the release --------------------------------------------
# malaviR::malavi_issues() re-derives each known problem from the release it has
# loaded and writes its sentence from that result, so the text carried here is
# always about THIS release. Captured rather than retyped into the HTML for the
# same reason as every other figure on the site: the page must follow the
# release, not a curator's memory of it. `title` and `text` are the only two
# fields the function returns.
#
# Wrapped in tryCatch so an older installed malaviR (one predating
# malavi_issues) degrades to an empty list rather than failing the whole export;
# the About page renders nothing in that case rather than stale text.
data_issues <- tryCatch({
  issues <- utils::capture.output(iss <- malavi_issues(version = release))
  if (nrow(iss) == 0) list() else
    lapply(seq_len(nrow(iss)),
           function(i) list(title = iss$title[i], text = iss$text[i]))
}, error = function(e) {
  cat(sprintf("\n  NOTE: malavi_issues() unavailable (%s); no issues written.\n",
              conditionMessage(e)))
  list()
})

cat(sprintf("\nknown data issues recorded for this release: %d\n", length(data_issues)))
for (di in data_issues) cat(sprintf("  - %s\n", di$title))

# ---- assemble ---------------------------------------------------------------
payload <- list(
  release             = release,
  generated           = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  malaviR_bundled     = malavi_version(),
  alignment_length_bp = cfg$alignment$length_bp,
  totals              = totals,
  genera              = unname(genus_rows),
  genus_order         = GENERA,
  unassigned_lineages = unassigned_lineages,
  country_studies     = country_studies,
  countries_unplaced  = I(unplaced),
  map_studies         = map_studies,
  map_studies_shown   = map_studies_shown,
  tables              = table_meta,
  taxonomy_source     = taxonomy_source,
  integrity           = integrity,
  data_issues         = data_issues
)

# ---- report -----------------------------------------------------------------
cat("totals:\n")
for (nm in names(totals)) cat(sprintf("  %-15s %s\n", nm, format(totals[[nm]], big.mark = ",")))
# Host-record figures. The map's own countries/studies are reported separately
# below, since the map also counts vector screening.
cat("\nper genus (lineages / host records / host species / host countries / host studies):\n")
for (g in genus_rows) {
  cat(sprintf("  %-15s %5d %6d %5d %4d %4d\n",
              g$key, g$lineages, g$records, g$host_species, g$countries, g$studies))
}
cat(sprintf("\n  unassigned lineages: %d\n", unassigned_lineages))
cat(sprintf("  countries on the map: %d\n", length(country_studies)))
cat("\nmap coverage (host records + vector screening):\n")
for (g in genus_rows) {
  cat(sprintf("  %-15s %4d countries (%d drawn), %4d studies (%d drawn)\n",
              g$key, g$map_countries, g$map_countries_shown,
              g$map_studies, g$map_studies_shown))
}
cat(sprintf("  %-15s %4d countries (%d drawn), %4d studies (%d drawn)\n",
            "all genera", length(country_studies), length(placed_countries),
            map_studies, map_studies_shown))
cat("\ntables:\n")
for (t in table_meta) cat(sprintf("  %-24s %s rows\n", t$id, format(t$rows, big.mark = ",")))

# ---- write ------------------------------------------------------------------
out_path <- file.path(repo_root, cfg$paths$docs_data_dir, "site_stats.json")
if (dry_run) {
  cat("\n[dry-run] would write:", out_path, "\n")
} else {
  dir.create(dirname(out_path), recursive = TRUE, showWarnings = FALSE)
  # auto_unbox so single values serialize as scalars, not one-element arrays.
  jsonlite::write_json(payload, out_path, auto_unbox = TRUE, pretty = TRUE, digits = NA)
  cat("\nwrote:", out_path, "\n")
}
