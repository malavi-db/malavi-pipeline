#!/usr/bin/env Rscript
# @title Build the host-range lookup behind the map's transmission view
# @purpose For every mapped host record that is a hatch-year bird (HOST_AGE
#          contains "Juvenile") and is not recorded as Resident, find which
#          BirdLife seasonal range polygon(s) of the host species contain the
#          sampling site, and write one row per (host, site) pair to a small CSV
#          that build_site_points.R joins onto the map payload.
# @why A hatch-year bird of a migratory species caught inside its breeding range
#      was infected where it was caught (a willow warbler in Sweden); caught on
#      its wintering grounds it may have been infected anywhere. The record has
#      no sampling date, so where the site lies relative to the species' seasonal
#      ranges is the only handle. Design: results/HANDOFF_2026-09-25.md. The range
#      polygons are licensed and stay on biostore; only this derived table ships.
# @input /mnt/ellisbiostore/malaviR (bundled release, via malaviR::extract_table)
# @input config/project.yml
# @input /mnt/ellisbiostore/bird_ranges/BOTW_2024_2.gpkg (BirdLife Birds of the World 2024.2, layer all_species)
# @input /mnt/ellisbiostore/birdsympatry_core/data-derived/crosswalk_malavi_2026-03-23.rds
# @input reference/host_range_splits.csv (MalAvi names that BirdLife splits: extra names to union)
# @output reference/host_range_lookup.csv
# @program Rscript
# @program malaviR
# @program sf
# @critical-var release
# @critical-var BOTW_GPKG
# @critical-var BOTW_EDITION
# @critical-var CROSSWALK_RDS
# @critical-var PRESENCE_KEEP
# @critical-var ORIGIN_KEEP
# @critical-var SEASONAL_BREEDING
# @critical-var SEASONAL_NONBREEDING
# =============================================================================
# Usage (biostore only; the range file is not on other machines):
#   Rscript export/build_range_lookup.R              # write reference/host_range_lookup.csv
#   Rscript export/build_range_lookup.R --dry-run    # report only, write nothing
#
# Runtime: the range file has no index on sci_name, so it is read with ONE query
# naming every species at once (about 4 minutes for ~70 species); reading species
# one at a time is a full scan each and does not finish. Point-in-polygon on the
# resulting few hundred polygons takes a few more minutes. Run it inside an
# interactive allocation or an sbatch job, never on the login node.
#
# Every host the crosswalk cannot name is LISTED on the console and left out of the
# lookup (its records read as "not tested" on the map). Never guess a mapping here;
# add it to the sympatry project's crosswalk inputs instead.
# =============================================================================

suppressMessages({
  library(malaviR)
  for (pkg in c("yaml", "sf")) if (!requireNamespace(pkg, quietly = TRUE))
    stop("The '", pkg, "' package is required. install.packages('", pkg, "').")
  library(sf)
})
sf_use_s2(FALSE)   # planar point-in-polygon on lon/lat: what the probe validated

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

# ---- the range data and the name join (biostore paths) ----------------------
# BirdLife Birds of the World range polygons. Licensed: read here, never copied.
BOTW_GPKG    <- "/mnt/ellisbiostore/bird_ranges/BOTW_2024_2.gpkg"
BOTW_LAYER   <- "all_species"
BOTW_EDITION <- "BirdLife BOTW 2024.2"
# MalAvi host name -> BirdLife name, built by the bird sympatry project.
CROSSWALK_RDS <- "/mnt/ellisbiostore/birdsympatry_core/data-derived/crosswalk_malavi_2026-03-23.rds"
# MalAvi names that BirdLife splits into several species: the extra BirdLife
# names whose polygons are unioned with the crosswalk's one. Committed, small.
SPLITS_CSV <- file.path(repo_root, "reference", "host_range_splits.csv")
# Polygon attribute filters, the same as the sympatry project uses.
PRESENCE_KEEP <- c(1L, 2L, 3L)   # extant, probably extant, possibly extant
ORIGIN_KEEP   <- c(1L, 2L)       # native, reintroduced
# Seasonal codes: 1 resident, 2 breeding, 3 non-breeding, 4 passage, 5 uncertain.
SEASONAL_BREEDING    <- c(1L, 2L)
SEASONAL_NONBREEDING <- c(3L, 4L)

out_path <- file.path(repo_root, "reference", "host_range_lookup.csv")

cat("== malavi_rebuild :: build_range_lookup ==\n")
cat("release   :", release, "\n")
cat("ranges    :", BOTW_EDITION, "\n")
cat("crosswalk :", basename(CROSSWALK_RDS), "\n\n")

for (p in c(BOTW_GPKG, CROSSWALK_RDS)) if (!file.exists(p)) stop("missing input: ", p)

# ---- the records the rule applies to ----------------------------------------
hosts <- extract_table("Hosts and Sites Table", version = release)

# Reuse the map exporter's coordinate parser so a site lands on the same point
# here as on the map (the join below is on the rounded coordinates).
src <- readLines(file.path(script_dir, "build_site_points.R"))
from <- grep("^COORDINATE_HALF <- ", src)
to   <- grep("^coord_text <- ", src) - 1
eval(parse(text = src[from:to]))

coord_text <- trimws(as.character(hosts$SITE_COORDINATES)); coord_text[is.na(coord_text)] <- ""
age    <- trimws(as.character(hosts$HOST_AGE));    age[is.na(age)] <- ""
status <- trimws(as.character(hosts$HOST_STATUS)); status[is.na(status)] <- ""

# Hatch-year birds not recorded as Resident (residents are local without a range
# test). "Adult + Juvenile" counts: the row contains hatch-year birds.
wanted <- nzchar(coord_text) & grepl("juvenile", age, ignore.case = TRUE) &
          tolower(status) != "resident"
cat(sprintf("host records                       %6d\n", nrow(hosts)))
cat(sprintf("  hatch-year, not Resident, w/ coords %6d\n", sum(wanted)))

parsed <- t(vapply(coord_text[wanted], parse_cell, numeric(2)))
pairs <- data.frame(
  malavi_host = trimws(as.character(hosts$SPECIES_NAME[wanted])),
  lat = round(parsed[, 1], 5), lon = round(parsed[, 2], 5),
  stringsAsFactors = FALSE)
pairs <- pairs[!is.na(pairs$lat), ]
counts <- aggregate(list(n_records = rep(1L, nrow(pairs))), pairs, sum)
pairs <- counts
cat(sprintf("  distinct (host, site) pairs        %6d\n", nrow(pairs)))

# ---- name join ---------------------------------------------------------------
cw <- readRDS(CROSSWALK_RDS)
pairs$botw_name <- cw$botw_name[match(pairs$malavi_host, cw$malavi_host)]
missing_hosts <- sort(unique(pairs$malavi_host[is.na(pairs$botw_name)]))
if (length(missing_hosts)) {
  cat(sprintf("\nhosts NOT in the crosswalk (left out, not guessed): %d\n", length(missing_hosts)))
  for (h in missing_hosts) cat("    ", h, "\n")
}
pairs <- pairs[!is.na(pairs$botw_name), ]

splits <- if (file.exists(SPLITS_CSV)) read.csv(SPLITS_CSV, stringsAsFactors = FALSE) else
  data.frame(malavi_host = character(), extra_botw_name = character())
# Every BirdLife name whose polygons a MalAvi host should be tested against.
botw_names_for <- function(host, primary) {
  unique(c(primary, splits$extra_botw_name[splits$malavi_host == host]))
}
pairs$botw_names <- vapply(seq_len(nrow(pairs)), function(i)
  paste(botw_names_for(pairs$malavi_host[i], pairs$botw_name[i]), collapse = "|"), "")
all_names <- unique(unlist(strsplit(pairs$botw_names, "|", fixed = TRUE)))
cat(sprintf("BirdLife names to read             %6d\n", length(all_names)))

# ---- ONE read of the range file ---------------------------------------------
names_sql <- paste(sprintf("'%s'", gsub("'", "''", all_names)), collapse = ",")
query <- sprintf("SELECT * FROM %s WHERE sci_name IN (%s) AND presence IN (%s) AND origin IN (%s)",
                 BOTW_LAYER, names_sql, paste(PRESENCE_KEEP, collapse = ","),
                 paste(ORIGIN_KEEP, collapse = ","))
t0 <- Sys.time()
poly <- suppressWarnings(st_read(BOTW_GPKG, query = query, quiet = TRUE))
cat(sprintf("polygons read                      %6d  (%.1f min)\n", nrow(poly),
            as.numeric(difftime(Sys.time(), t0, units = "mins"))))
poly <- st_make_valid(poly)
no_polygon <- setdiff(all_names, unique(poly$sci_name))
if (length(no_polygon)) {
  cat(sprintf("BirdLife names with NO polygon: %d\n", length(no_polygon)))
  for (h in no_polygon) cat("    ", h, "\n")
}

# ---- point-in-polygon --------------------------------------------------------
pts <- st_as_sf(pairs, coords = c("lon", "lat"), crs = 4326, remove = FALSE)
hits <- suppressMessages(st_intersects(pts, poly))
pairs$seasonal_codes <- NA_character_
pairs$verdict <- NA_character_
for (k in seq_len(nrow(pairs))) {
  names_k <- strsplit(pairs$botw_names[k], "|", fixed = TRUE)[[1]]
  i <- hits[[k]]; i <- i[poly$sci_name[i] %in% names_k]
  codes <- sort(unique(poly$seasonal[i]))
  if (!any(names_k %in% poly$sci_name)) {
    pairs$seasonal_codes[k] <- ""; pairs$verdict[k] <- "no_polygon"
  } else if (!length(codes)) {
    pairs$seasonal_codes[k] <- ""; pairs$verdict[k] <- "outside"
  } else {
    pairs$seasonal_codes[k] <- paste(codes, collapse = "+")
    in_b <- any(codes %in% SEASONAL_BREEDING); in_n <- any(codes %in% SEASONAL_NONBREEDING)
    pairs$verdict[k] <- if (in_b && !in_n) "breeding_or_resident" else
                        if (in_n && !in_b) "nonbreeding_or_passage" else
                        if (in_b && in_n) "mixed" else "uncertain"
  }
}

cat("\nverdict                       pairs  records\n")
for (v in c("breeding_or_resident", "nonbreeding_or_passage", "mixed", "uncertain", "outside", "no_polygon")) {
  sel <- pairs$verdict == v
  cat(sprintf("  %-26s %6d  %6d\n", v, sum(sel), sum(pairs$n_records[sel])))
}
cat("\n'outside' pairs (a site outside every polygon of its host; check the coordinates' signs):\n")
o <- pairs[pairs$verdict == "outside", ]
for (k in seq_len(nrow(o))) cat(sprintf("    %-30s %9.4f %9.4f  (%d record%s)\n", o$malavi_host[k], o$lat[k], o$lon[k], o$n_records[k], if (o$n_records[k] == 1) "" else "s"))

out <- pairs[order(pairs$malavi_host, pairs$lat, pairs$lon),
             c("malavi_host", "botw_names", "lat", "lon", "n_records", "seasonal_codes", "verdict")]
out$range_edition <- BOTW_EDITION
out$crosswalk <- basename(CROSSWALK_RDS)
out$release <- release

if (dry_run) {
  cat("\n[dry-run] would write:", out_path, "\n")
} else {
  write.csv(out, out_path, row.names = FALSE)
  cat(sprintf("\nwrote: %s (%d rows)\n", out_path, nrow(out)))
}
