#!/usr/bin/env Rscript
# @title Build the sampling-site points the website's map page draws
# @purpose Export every host record that carries site coordinates as a point on
#          a map, grouped by site, with the lineage, parasite genus, host
#          species, study and prevalence of each record at that site, so the
#          page can show "where has this host / this genus been found".
# @why MalAvi's records carry the coordinates of the sampling site as a text
#      cell ("57°10.00000', 016°58.00000'") that no map library can read. Parsing
#      it once here, at build time, gives the page decimal degrees and lets it
#      answer the question Tamara's Shiny app answered on the old site -- pick a
#      host species and a parasite genus, see the sites -- without a server.
# @input /mnt/ellisbiostore/malaviR (bundled release, via malaviR::extract_table)
# @input config/project.yml
# @output docs/assets/data/site_points.json
# @program Rscript
# @program malaviR
# @program jsonlite
# @critical-var release
# @critical-var COORDINATE_HALF
# =============================================================================
# Usage:
#   Rscript export/build_site_points.R              # write the points file
#   Rscript export/build_site_points.R --dry-run    # report only, write nothing
#
# Records whose coordinate cell cannot be read are counted and listed on the
# console and in the file ("unparsed"), never silently dropped: a new coordinate
# spelling in a future release should surface here, not vanish from the map.
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

release <- cfg$malaviR$release
if (identical(release, "latest")) release <- malavi_version()

out_path <- file.path(repo_root, "docs", "assets", "data", "site_points.json")

cat("== malavi_rebuild :: build_site_points ==\n")
cat("release :", release, "\n\n")

hosts <- extract_table("Hosts and Sites Table", version = release)

# ---- parse the coordinate cell ----------------------------------------------
# One half of a coordinate cell: an optional minus sign, whole degrees, a degree
# sign (either of the two characters the data uses), decimal minutes, and an
# optional minute mark in any of the five glyphs the data uses for it, then an
# optional hemisphere letter. The two halves are separated by a comma, with or
# without spaces around it.
COORDINATE_HALF <- "^\\s*(-?)\\s*([0-9]{1,3})\\s*[°º]\\s*([0-9]{1,2}(?:[.,][0-9]+)?)\\s*['’′´`]?\\s*([NSEWnsew])?\\s*$"

# parse_half(): one half -> decimal degrees, or NA when it does not read.
parse_half <- function(text) {
  m <- regmatches(text, regexec(COORDINATE_HALF, text, perl = TRUE))[[1]]
  if (length(m) == 0) return(NA_real_)
  degrees <- as.numeric(m[3])
  minutes <- as.numeric(sub(",", ".", m[4], fixed = TRUE))
  if (is.na(degrees) || is.na(minutes) || minutes >= 60) return(NA_real_)
  value <- degrees + minutes / 60
  # A hemisphere letter (S or W) is the same as a leading minus: the release's own
  # cells carry the sign, a submission's may carry the letter.
  hemisphere <- toupper(m[5])
  if (m[2] == "-" || hemisphere %in% c("S", "W")) -value else value
}

# parse_cell(): the whole cell -> c(lat, lon), or c(NA, NA).
parse_cell <- function(cell) {
  parts <- strsplit(cell, "\\s*,\\s*")[[1]]
  parts <- parts[nzchar(trimws(parts))]
  if (length(parts) != 2) return(c(NA_real_, NA_real_))
  lat <- parse_half(parts[1])
  lon <- parse_half(parts[2])
  if (is.na(lat) || is.na(lon) || abs(lat) > 90 || abs(lon) > 180)
    return(c(NA_real_, NA_real_))
  c(lat, lon)
}

coord_text <- trimws(as.character(hosts$SITE_COORDINATES))
coord_text[is.na(coord_text)] <- ""
has_coords <- nzchar(coord_text)

# Parse each DISTINCT cell once; 14,000 rows share about 1,400 sites.
distinct_cells <- unique(coord_text[has_coords])
parsed <- t(vapply(distinct_cells, parse_cell, numeric(2)))
colnames(parsed) <- c("lat", "lon")
lookup <- match(coord_text, distinct_cells)
lat <- ifelse(has_coords, parsed[lookup, "lat"], NA_real_)
lon <- ifelse(has_coords, parsed[lookup, "lon"], NA_real_)

placed <- has_coords & !is.na(lat)
unparsed_cells <- distinct_cells[is.na(parsed[, "lat"])]

cat(sprintf("host records                 %6d\n", nrow(hosts)))
cat(sprintf("  with a coordinate cell     %6d\n", sum(has_coords)))
cat(sprintf("  placed on the map          %6d\n", sum(placed)))
cat(sprintf("  unreadable coordinate cell %6d row(s), %d distinct\n",
            sum(has_coords & !placed), length(unparsed_cells)))
for (cell in unparsed_cells) cat("    ", cell, "\n")

# ---- group records by site --------------------------------------------------
# A site is a distinct (lat, lon, site name, country). Coordinates are rounded
# to five decimals (about a meter) so that two spellings of the same cell that
# read to the same place become one point.
text_or_blank <- function(x) { x <- trimws(as.character(x)); x[is.na(x)] <- ""; x }

records <- data.frame(
  lat      = round(lat[placed], 5),
  lon      = round(lon[placed], 5),
  site     = text_or_blank(hosts$SITE_NAME[placed]),
  country  = text_or_blank(hosts$COUNTRY_NAME[placed]),
  lineage  = text_or_blank(hosts$LINEAGE_NAME[placed]),
  genus    = text_or_blank(hosts$PARASITE_GENUS[placed]),
  host     = text_or_blank(hosts$SPECIES_NAME[placed]),
  ref      = text_or_blank(hosts$REFERENCE_NAME[placed]),
  found    = suppressWarnings(as.integer(hosts$NUMBER_FOUND[placed])),
  tested   = suppressWarnings(as.integer(hosts$NUMBER_TESTED[placed])),
  stringsAsFactors = FALSE
)

# String tables, so each record is a short array of indexes rather than five
# repeated strings; the page rebuilds the rows from these. Zero-based for JS.
genera     <- unique(c(cfg_genera <- c("Plasmodium", "Haemoproteus", "Leucocytozoon"),
                       sort(unique(records$genus[nzchar(records$genus)]))))
hosts_tbl  <- sort(unique(records$host))
refs_tbl   <- sort(unique(records$ref))
lineages   <- sort(unique(records$lineage))

records$g <- match(records$genus, genera) - 1L
records$h <- match(records$host, hosts_tbl) - 1L
records$r <- match(records$ref, refs_tbl) - 1L
records$l <- match(records$lineage, lineages) - 1L
records$g[is.na(records$g)] <- -1L

site_key <- paste(records$lat, records$lon, records$site, records$country, sep = "\t")
site_ids <- match(site_key, unique(site_key))
n_sites  <- max(site_ids)

sites <- vector("list", n_sites)
for (i in seq_len(n_sites)) {
  rows <- which(site_ids == i)
  first <- rows[1]
  recs <- lapply(rows, function(j) {
    list(records$l[j], records$g[j], records$h[j], records$r[j],
         if (is.na(records$found[j])) NULL else records$found[j],
         if (is.na(records$tested[j])) NULL else records$tested[j])
  })
  sites[[i]] <- list(
    la = records$lat[first], lo = records$lon[first],
    s  = records$site[first], c = records$country[first],
    r  = recs
  )
}

cat(sprintf("\nsites                        %6d\n", n_sites))
cat(sprintf("host species with a point    %6d\n", length(hosts_tbl)))
cat(sprintf("lineages with a point        %6d\n", length(lineages)))
cat(sprintf("studies with a point         %6d\n", length(refs_tbl)))

payload <- list(
  release   = release,
  generated = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
  n_records = nrow(hosts),
  n_with_coordinates = sum(has_coords),
  n_placed  = sum(placed),
  unparsed  = as.list(unparsed_cells),
  genera    = as.list(genera),
  hosts     = as.list(hosts_tbl),
  references = as.list(refs_tbl),
  lineages  = as.list(lineages),
  sites     = sites
)

if (dry_run) {
  cat("\n[dry-run] would write:", out_path, "\n")
} else {
  # null is the JSON for a missing count; every other NA has been turned into a
  # blank string or -1 above, so nothing in the payload reads as text "NA".
  json <- jsonlite::toJSON(payload, auto_unbox = TRUE, null = "null", digits = NA,
                           pretty = FALSE)
  writeLines(json, out_path, useBytes = TRUE)
  cat(sprintf("\nwrote: %s (%.2f MB)\n", out_path, file.size(out_path) / 1e6))
}
