#!/usr/bin/env Rscript
# @title Build the full per-table JSON the website's table browser loads
# @purpose Write every row of every published MalAvi table to
#          docs/assets/data/tables/<id>.json, plus a small column-spec index at
#          docs/assets/data/tables_index.json that the page loads at startup.
# @why The site previously shipped docs/assets/data/tables_preview.json, a
#      hand-made SAMPLE (400 of 18,493 host rows) left over from the design
#      preview. Filtering it silently returned a fraction of the real records --
#      lineage PADOM01 showed 1 host record where the release has 39. The table
#      browser has to search the whole release, so the whole release has to be
#      exported.
# @input /mnt/ellisbiostore/malaviR (bundled release, via malaviR::extract_table)
# @input config/project.yml
# @output docs/assets/data/tables_index.json
# @output docs/assets/data/tables/hosts_and_sites.json
# @output docs/assets/data/tables/grand_lineage_summary.json
# @output docs/assets/data/tables/references.json
# @output docs/assets/data/tables/morpho_species.json
# @output docs/assets/data/tables/vector_data.json
# @output docs/assets/data/tables/lineage_names.json
# @output docs/assets/data/tables/taxonomy.json
# @program Rscript
# @program malaviR
# @program jsonlite
# @critical-var release
# @critical-var COLUMN_SPEC
# @critical-var DROPPED_COLUMNS
# =============================================================================
# Usage:
#   Rscript export/build_tables_json.R              # write the JSON
#   Rscript export/build_tables_json.R --dry-run    # report the plan, write nothing
#
# Output format, per table:
#
#   { "id": "hosts_and_sites",
#     "release": "2026-03-23",
#     "n_rows": 18493,
#     "columns": [ { "key": "lineage", "label": "Lineage", "type": "key" }, ... ],
#     "rows":    [ ["PADOM01", "", "Plasmodium", ...], ... ] }
#
# Rows are arrays, not objects, and a cell is found by its column's POSITION in
# `columns`. Object rows would repeat all twenty key names on all 18,493 host
# rows -- about 4 MB of nothing but punctuation, and 18,493 objects for the
# browser to allocate instead of 18,493 arrays.
#
# The tables are loaded one at a time, only when the visitor opens one, so no
# visitor downloads a table they never look at. That is why the column spec is
# split out into tables_index.json: the page needs the columns to build the
# table index at startup, but not the rows.
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

# The shared table accessor: the same tables, derived the same way, that
# build_site_stats.R counts and build_downloads.R writes to CSV/XLSX. Sourcing
# it rather than re-deriving here is what keeps the advertised row count, the
# downloadable file and the browsable table describing the same data.
source(file.path(script_dir, "lib", "tables.R"))

trailing <- commandArgs(trailingOnly = TRUE)
dry_run <- "--dry-run" %in% trailing

# Resolve the pinned release; "latest" defers to whatever malaviR bundles.
release <- cfg$malaviR$release
if (identical(release, "latest")) release <- malavi_version()

cat("== malavi_rebuild :: build_tables_json ==\n")
cat("release :", release, "\n\n")

# -----------------------------------------------------------------------------
# Column specification.
#
# One entry per column the browsable table shows, in display order. Each entry is
#   source : the column name in the malaviR data frame
#   key    : short stable id, used as the filter input's data-col attribute
#   label  : the column heading shown to a visitor
#   type   : how the cell is rendered by malavi.js / styled by malavi.css --
#              ""      plain text
#              "key"   monospace identifier (lineage codes, reference keys)
#              "genus" colored parasite-genus pill
#              "sci"   scientific name (italic)
#              "num"   right-aligned, tabular figures
#              "wrap"  long free text, allowed to wrap onto several lines
#              "seq"   a nucleotide sequence: monospace, wrapped, width-capped
#
# Every column malaviR ships is listed, so the browsable table carries the whole
# release rather than a chosen subset. DROPPED_COLUMNS below is the escape hatch
# for anything deliberately left out; it is currently empty.
# -----------------------------------------------------------------------------
col <- function(source, key, label, type = "") {
  list(source = source, key = key, label = label, type = type)
}

COLUMN_SPEC <- list(

  # 20 of 20 malaviR columns.
  hosts_and_sites = list(
    col("LINEAGE_NAME",        "lineage",    "Lineage",        "key"),
    col("ALT_NAME",            "alt",        "Name in the paper"),
    col("PARASITE_GENUS",      "genus",      "Parasite",       "genus"),
    col("ORDER_NAME",          "order",      "Order"),
    col("FAMILY_NAME",         "family",     "Family"),
    col("GENUS_NAME",          "host_genus", "Host genus",     "sci"),
    col("SPECIES_NAME",        "host",       "Host species",   "sci"),
    col("SUB_SPECIES_NAME",    "subspecies", "Subspecies",     "sci"),
    col("HOST_STATUS",         "status",     "Status"),
    col("HOST_AGE",            "age",        "Age"),
    col("HOST_ENVIRONMENT",    "env",        "Environment"),
    col("CONTINENT_NAME",      "continent",  "Continent"),
    col("COUNTRY_NAME",        "country",    "Country"),
    col("COUNTRY_REGION_NAME", "region",     "Region"),
    col("SITE_NAME",           "site",       "Site"),
    col("SITE_COORDINATES",    "coords",     "Coordinates"),
    col("NUMBER_FOUND",        "found",      "Found",          "num"),
    col("NUMBER_TESTED",       "tested",     "Tested",         "num"),
    col("REFERENCE_NAME",      "ref",        "Reference"),
    col("COMMENT",             "note",       "Comment",        "wrap")
  ),

  # All 24 malaviR columns.
  grand_lineage_summary = list(
    col("LINEAGE_NAME",                 "lineage",       "Lineage",        "key"),
    col("GENBANK_ACC",                  "acc",           "GenBank"),
    col("SEQ_LENGTH",                   "len",           "Length",         "num"),
    col("GENUS_NAME",                   "genus",         "Parasite genus", "genus"),
    col("SPECIES_NAME",                 "species",       "Morphospecies",  "sci"),
    col("SUM_VECTORS",                  "vectors",       "Vectors",        "num"),
    col("SUM_HOST",                     "hosts",         "Hosts",          "num"),
    col("SUM_GENUS",                    "genera",        "Genera",         "num"),
    col("SUM_FAMILY",                   "fam",           "Families",       "num"),
    col("SUM_ORDER",                    "ord",           "Orders",         "num"),
    col("PASSERIFORMES",                "passeriformes", "Passeriformes",  "num"),
    col("EUROPE",                       "europe",        "Europe",         "num"),
    col("SUB_SAHARAN_AFRICA",           "ssafrica",      "Sub-Saharan Africa", "num"),
    col("NORTH_AFRICA_AND_MIDDLE_EAST", "nafrica_me",    "North Africa and Middle East", "num"),
    col("NORTH_AMERICA",                "namerica",      "North America",  "num"),
    col("HAWAI",                        "hawaii",        "Hawaii",         "num"),
    col("CENTRAL_AMERICA",              "camerica",      "Central America", "num"),
    col("SOUTH_AMERICA",                "samerica",      "South America",  "num"),
    col("ASIA",                         "asia",          "Asia",           "num"),
    col("AUSTRALIA_AND_NEW_ZEALAND",    "aunz",          "Australia and New Zealand", "num"),
    col("OCEANIA",                      "oceania",       "Oceania",        "num"),
    col("ANTARCTICA",                   "antarctica",    "Antarctica",     "num"),
    col("UNKNOWN_REGION",               "unknown",       "Unknown region", "num"),
    # Last, because it is ~479 characters wide and would otherwise push every
    # other column off the screen. It carries this table on its own: about
    # 2.6 MB of the file, roughly five times everything else put together. That
    # is accepted -- the old MalAvi site showed the sequence here and visitors
    # expect to find it -- and the table is only downloaded when opened.
    col("SEQUENCE",                     "sequence",      "Sequence",       "seq")
  ),

  # 6 of 6 malaviR columns.
  references = list(
    col("REFERENCE_NAME",   "ref",        "Reference",        "key"),
    col("PUBLICATION_YEAR", "year",       "Year",             "num"),
    col("TITLE",            "title",      "Title",            "wrap"),
    col("JOURNAL_NAME",     "journal",    "Journal"),
    col("VOLUME_PAGES",     "vol",        "Volume and pages"),
    col("STUDY_TYPE",       "study_type", "Study type")
  ),

  # 5 of 5 malaviR columns.
  morpho_species = list(
    col("LINEAGE_NAME",       "lineage", "Lineage",        "key"),
    col("GENUS_NAME",         "genus",   "Parasite genus", "genus"),
    col("SPECIES_NAME",       "species", "Species",        "sci"),
    col("REFERENCE_NAME",     "ref",     "Reference"),
    col("MORPHOLOGY_COMMENT", "note",    "Comment",        "wrap")
  ),

  # 6 of 6 malaviR columns.
  vector_data = list(
    col("LINEAGE_NAME",   "lineage", "Lineage",        "key"),
    col("VECTOR_SPECIES", "vector",  "Vector species", "sci"),
    col("VECTOR_METHOD",  "method",  "Method"),
    col("COUNTRY_NAME",   "country", "Country"),
    col("SITE_NAME",      "site",    "Site"),
    col("REFERENCE_NAME", "ref",     "Reference")
  ),

  # All 3 columns of the derived table (see malavi_lineage_names_table()).
  lineage_names = list(
    col("LINEAGE_NAME",   "lineage", "MalAvi name",       "key"),
    col("ALT_NAME",       "alt",     "Name in the paper", "key"),
    col("REFERENCE_NAME", "ref",     "Reference")
  ),

  # All 6 columns of malaviR's host taxonomy key. Not a MalAvi release table --
  # see malavi_taxonomy_table(). The MalAvi name comes first because that is the
  # name a visitor arrives with, having read it in the hosts table.
  taxonomy = list(
    col("malavi_species", "malavi", "Host in MalAvi", "sci"),
    col("ebird_species",  "ebird",  "eBird name",     "sci"),
    col("ott_name",       "ott",    "clootl tip",     "sci"),
    col("order",          "order",  "Order"),
    col("family",         "family", "Family"),
    col("match_type",     "match",  "How it matched")
  )
)

# Columns deliberately left out of the browsable table, keyed by table id, with
# the reason. Nothing is currently dropped: every column of every table is shown.
# The check below treats this as the ONLY sanctioned way to omit a column, so a
# column removed from a spec without an entry here fails the build rather than
# quietly disappearing from the site.
DROPPED_COLUMNS <- list()

# ---- pull the tables --------------------------------------------------------
# Everything the site browses, which is the release's own tables plus the
# derived host taxonomy key.
tables <- site_tables(release)

# ---- verify the spec against the release ------------------------------------
# A release that renames, adds or drops a column must fail loudly here rather
# than quietly publishing a table with a blank or missing column. Every column
# in the release has to be either mapped in COLUMN_SPEC or listed in
# DROPPED_COLUMNS with a reason.
problems <- character(0)
for (id in SITE_TABLE_IDS) {
  spec_sources <- vapply(COLUMN_SPEC[[id]], function(c) c$source, character(1))
  released     <- colnames(tables[[id]])
  dropped      <- names(DROPPED_COLUMNS[[id]])

  # Mapped a column the release does not have.
  missing <- setdiff(spec_sources, released)
  if (length(missing))
    problems <- c(problems, sprintf("%s: spec maps column(s) not in the release: %s",
                                    id, paste(missing, collapse = ", ")))

  # The release has a column the spec neither shows nor explicitly drops.
  unaccounted <- setdiff(released, c(spec_sources, dropped))
  if (length(unaccounted))
    problems <- c(problems, sprintf("%s: release column(s) neither shown nor dropped: %s",
                                    id, paste(unaccounted, collapse = ", ")))
}
if (length(problems)) {
  cat("COLUMN SPEC DOES NOT MATCH RELEASE ", release, ":\n", sep = "")
  cat(paste0("  - ", problems, collapse = "\n"), "\n\n", sep = "")
  stop("Update COLUMN_SPEC / DROPPED_COLUMNS in export/build_tables_json.R.")
}

# ---- report the plan --------------------------------------------------------
json_dir  <- file.path(repo_root, cfg$paths$docs_data_dir)
table_dir <- file.path(json_dir, "tables")

cat("writes JSON to :", table_dir, "\n\n")
for (id in SITE_TABLE_IDS) {
  cat(sprintf("  %-24s %6d rows x %2d columns\n",
              paste0(id, ".json"), nrow(tables[[id]]), length(COLUMN_SPEC[[id]])))
}
for (id in names(DROPPED_COLUMNS)) {
  for (dropped in names(DROPPED_COLUMNS[[id]]))
    cat(sprintf("\n  note: %s omits %s from the browsable grid.\n", id, dropped))
}

if (dry_run) {
  cat("\n[--dry-run] No files written. Exiting.\n")
  quit(status = 0)
}

# ---- write the per-table payloads -------------------------------------------
dir.create(table_dir, recursive = TRUE, showWarnings = FALSE)

#' One table's cells as a character matrix, in the spec's column order.
#'
#' Everything is emitted as a string and every missing value as "", so a cell is
#' never the JSON `null` that the page would have to render as the text "null",
#' and a filter never has to think about types. malaviR already ships most of
#' these columns as character; the numeric ones (NUMBER_FOUND, NUMBER_TESTED)
#' are formatted rather than coerced so that a whole number stays "12" and does
#' not become "12.0" or "1.2e+01".
cells_for <- function(df, spec) {
  columns <- lapply(spec, function(c) {
    values <- df[[c$source]]
    text <- if (is.numeric(values)) {
      format(values, trim = TRUE, scientific = FALSE, drop0trailing = TRUE)
    } else {
      as.character(values)
    }
    text[is.na(values)] <- ""   # test the ORIGINAL values: format() renders NA as "NA"
    text
  })
  matrix(unlist(columns), nrow = nrow(df), ncol = length(spec))
}

# The column spec without the internal `source` field. The website has no use for
# the malaviR column name, and publishing it would put the upstream schema on the
# public site as an implicit promise we do not intend to keep.
public_spec <- function(spec) {
  lapply(spec, function(c) list(key = c$key, label = c$label, type = c$type))
}

index_entry <- list()
for (id in SITE_TABLE_IDS) {
  spec <- COLUMN_SPEC[[id]]
  df   <- tables[[id]]

  payload <- list(
    id      = id,
    release = release,
    n_rows  = nrow(df),
    columns = public_spec(spec),
    # A one-row table would otherwise be unboxed from [[a, b]] to [a, b], so the
    # nesting is forced. dataframe/matrix rows serialize as arrays of arrays.
    rows    = cells_for(df, spec)
  )

  out_path <- file.path(table_dir, paste0(id, ".json"))
  jsonlite::write_json(payload, out_path, auto_unbox = TRUE, na = "string")

  # The index the page loads at startup: columns and row count, no rows.
  index_entry[[id]] <- list(n_rows = nrow(df), columns = public_spec(spec))

  cat(sprintf("  wrote %-28s %6d rows  %6.1f MB\n",
              paste0("tables/", id, ".json"), nrow(df),
              file.info(out_path)$size / 1024^2))
}

index_path <- file.path(json_dir, "tables_index.json")
jsonlite::write_json(
  list(release = release, tables = index_entry),
  index_path, auto_unbox = TRUE, na = "string"
)
cat(sprintf("\n  wrote %-28s %6.1f KB\n", "tables_index.json",
            file.info(index_path)$size / 1024))

cat("\nDone.\n")
