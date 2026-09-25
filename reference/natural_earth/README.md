# Natural Earth country polygons (for the site-coordinate check)

`ne_10m_admin_0_countries_simplified.geojson.gz` is the Natural Earth 1:10m "Admin 0 -
Countries" layer, simplified, for `malavi_curation.site_coordinates`: the submission
screen's check that a site's coordinates fall inside the country the submitter named.
It exists because the site's own basemap (`docs/assets/data/world_map.json`, 1:110m) is
too coarse to tell a coastal site from one whose longitude lost its minus sign.

- Source: https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip
- Version: 5.1.1 (the zip's `ne_10m_admin_0_countries.VERSION.txt`), fetched 2026-09-25
- sha256 of the zip: `ce1ac7036499a0edd641fbc093cd209a98f96a49d2eca8480aaacad35138a7f6`
- License: public domain (Natural Earth places all its data in the public domain)
- Features kept: all 258, with the properties `NAME`, `NAME_LONG`, `ADMIN`, `SOVEREIGNT`,
  `ISO_A2`, `ISO_A3`, `TYPE`
- Simplification: `sf::st_simplify(dTolerance = 0.01, preserveTopology = TRUE)` in
  degrees, then `st_make_valid`; written as RFC 7946 GeoJSON with four decimal places.
  About 1 km of tolerance: 190,473 vertices, 4.4 MB uncompressed, 1.3 MB gzipped.

How it was made (R, on a compute node):

```r
library(sf); sf_use_s2(FALSE)
ne <- st_read("ne_10m_admin_0_countries.shp")
keep <- ne[, c("NAME", "NAME_LONG", "ADMIN", "SOVEREIGNT", "ISO_A2", "ISO_A3", "TYPE")]
simp <- st_make_valid(st_simplify(keep, dTolerance = 0.01, preserveTopology = TRUE))
st_write(simp, "ne_10m_admin_0_countries_simplified.geojson", driver = "GeoJSON",
         layer_options = c("COORDINATE_PRECISION=4", "RFC7946=YES"))
# then: gzip -9 ne_10m_admin_0_countries_simplified.geojson
```

`country_aliases.csv` maps MalAvi's country spellings that are not a Natural Earth
`ADMIN`, `NAME` or `NAME_LONG` onto the feature to test against. Overseas departments
(Guadeloupe, Martinique, Réunion, Mayotte) are part of the France feature in this layer,
so they map to France. A MalAvi country with no row here and no Natural Earth name is
reported as untestable, never guessed.
