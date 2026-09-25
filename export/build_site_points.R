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
# @input reference/host_range_lookup.csv (optional; from export/build_range_lookup.R)
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
#
# Transmission view (2026-09-25). Each record also carries its HOST_AGE and
# HOST_STATUS, and a range flag from reference/host_range_lookup.csv that says
# whether a hatch-year bird's site lies inside its species' breeding range. The
# page turns these into three classes (local transmission / possibly acquired
# elsewhere / undetermined); the same rule is applied here, once, so that the
# class counts written into the payload can be checked against the page's own
# arithmetic (docs/assets/js/tests/test_transmission.mjs).
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
  age      = text_or_blank(hosts$HOST_AGE[placed]),
  status   = text_or_blank(hosts$HOST_STATUS[placed]),
  stringsAsFactors = FALSE
)

# ---- the host-range flag for hatch-year birds ------------------------------
# reference/host_range_lookup.csv (export/build_range_lookup.R, biostore only)
# holds, per (host, site), whether the site lies inside the host's BirdLife
# breeding/resident range. Codes shipped to the page:
#   -1 not tested (no lookup row: residents, adults, hosts the crosswalk lacks)
#    0 outside every polygon, or no polygon for the species
#    1 inside a breeding or resident polygon only
#    2 inside a non-breeding or passage polygon only
#    3 inside both kinds (overlap zone), or seasonally uncertain
RANGE_CODE <- c(outside = 0L, no_polygon = 0L, breeding_or_resident = 1L,
                nonbreeding_or_passage = 2L, mixed = 3L, uncertain = 3L)
lookup_path <- file.path(repo_root, "reference", "host_range_lookup.csv")
records$range <- -1L
range_edition <- NULL
if (file.exists(lookup_path)) {
  lookup <- read.csv(lookup_path, stringsAsFactors = FALSE)
  key_rec <- paste(records$host, records$lat, records$lon, sep = "\t")
  key_lk  <- paste(lookup$malavi_host, round(lookup$lat, 5), round(lookup$lon, 5), sep = "\t")
  hit <- match(key_rec, key_lk)
  code <- RANGE_CODE[lookup$verdict[hit]]
  code[is.na(code)] <- -1L
  # A lookup row only ever applies to the records the lookup was built for
  # (hatch-year, not Resident); other records at the same site keep -1.
  applies <- grepl("juvenile", records$age, ignore.case = TRUE) &
             tolower(records$status) != "resident"
  records$range <- ifelse(applies & !is.na(hit), as.integer(code), -1L)
  range_edition <- unique(lookup$range_edition)[1]
  cat(sprintf("range lookup                 %6d rows (%s); %d records flagged\n",
              nrow(lookup), range_edition, sum(records$range >= 0)))
  if (!identical(unique(lookup$release), release))
    cat("  NOTE: the lookup was built for release", unique(lookup$release),
        "and this is", release, "-- re-run export/build_range_lookup.R\n")
} else {
  cat("range lookup                 (none: reference/host_range_lookup.csv missing; hatch-year",
      "birds of migratory species will all read as undetermined)\n")
}

# ---- the transmission class, as the page computes it ------------------------
# ONE rule, mirrored in docs/assets/js/transmission.mjs. Local: the bird was
# infected where it was sampled (a resident of any age, a nestling, or a
# hatch-year bird inside its breeding range). Elsewhere: a migratory adult may
# have been infected anywhere on its route. Undetermined: everything else.
transmission_class <- function(age, status, range) {
  a <- tolower(age); s <- tolower(status)
  ifelse(s == "resident", "local",
  ifelse(grepl("nestling", a, fixed = TRUE), "local",
  ifelse(grepl("juvenile", a, fixed = TRUE) & range == 1L, "local",
  ifelse(a == "adult" & s == "migratory", "elsewhere", "undetermined"))))
}
records$class <- transmission_class(records$age, records$status, records$range)
class_counts <- as.list(table(factor(records$class, c("local", "elsewhere", "undetermined"))))
cat(sprintf("transmission classes         local %d, elsewhere %d, undetermined %d\n",
            class_counts$local, class_counts$elsewhere, class_counts$undetermined))

# String tables, so each record is a short array of indexes rather than five
# repeated strings; the page rebuilds the rows from these. Zero-based for JS.
genera     <- unique(c(cfg_genera <- c("Plasmodium", "Haemoproteus", "Leucocytozoon"),
                       sort(unique(records$genus[nzchar(records$genus)]))))
hosts_tbl  <- sort(unique(records$host))
refs_tbl   <- sort(unique(records$ref))
lineages   <- sort(unique(records$lineage))
ages       <- sort(unique(records$age))
statuses   <- sort(unique(records$status))

records$g <- match(records$genus, genera) - 1L
records$h <- match(records$host, hosts_tbl) - 1L
records$r <- match(records$ref, refs_tbl) - 1L
records$l <- match(records$lineage, lineages) - 1L
records$a <- match(records$age, ages) - 1L
records$s <- match(records$status, statuses) - 1L
records$g[is.na(records$g)] <- -1L

site_key <- paste(records$lat, records$lon, records$site, records$country, sep = "\t")
site_ids <- match(site_key, unique(site_key))
n_sites  <- max(site_ids)

sites <- vector("list", n_sites)
for (i in seq_len(n_sites)) {
  rows <- which(site_ids == i)
  first <- rows[1]
  recs <- lapply(rows, function(j) {
    # [lineage, genus, host, reference, found, tested, age, status, range]
    list(records$l[j], records$g[j], records$h[j], records$r[j],
         if (is.na(records$found[j])) NULL else records$found[j],
         if (is.na(records$tested[j])) NULL else records$tested[j],
         records$a[j], records$s[j], records$range[j])
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
  ages      = as.list(ages),
  statuses  = as.list(statuses),
  # The transmission view's inputs and the counts the page must reproduce.
  transmission = list(
    range_edition  = if (is.null(range_edition)) NULL else range_edition,
    n_range_tested = sum(records$range >= 0),
    counts         = class_counts
  ),
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
