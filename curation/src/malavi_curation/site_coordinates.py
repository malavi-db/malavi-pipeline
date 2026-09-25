# @title Do a site's coordinates fall inside the country the submitter named?
# @purpose Parse the LATITUDE / LONGITUDE cells of a submission's Sites sheet into
#          decimal degrees, test the point against the named country's polygon, and
#          when it falls outside say how far, which country it does fall in, and
#          whether flipping a sign would put it inside.
# @why Two release sites turned out to be in the sea because a longitude lost its
#      minus sign (found 2026-09-25 by the map's host-range lookup). Nothing in the
#      screen read a coordinate at all. This is the deterministic, license-free
#      version of that check; it runs on every submission.
# @input reference/natural_earth/ne_10m_admin_0_countries_simplified.geojson.gz
# @input reference/natural_earth/country_aliases.csv
# @output (none; a function the template screen calls)
# @program python
# @critical-var TOLERANCE_KM
# @critical-var KM_PER_DEGREE_LAT
"""Where a submitted site really is.

Everything here is plain arithmetic on the Natural Earth polygons shipped in
``reference/natural_earth`` (public domain; see its README): a ray-casting
point-in-polygon test and a point-to-segment distance. No geospatial library, so the
check runs wherever the screen runs.

The verdicts a caller gets from :func:`check_site`:

* ``inside``            the point is inside the named country's polygon.
* ``near``              outside it, but within :data:`TOLERANCE_KM` of its boundary.
                        Coastal sites and coarse coordinates land here; not a finding.
* ``outside``           further away than that. The result says how far, which country
                        (if any) contains the point, and which single sign flip or
                        latitude/longitude swap would put it inside the named country.
* ``country_unknown``   the named country is not a Natural Earth feature and has no
                        alias; nothing was tested, and the caller should say so.

A coordinate that cannot be read is the caller's finding (:func:`parse_coordinate`
returns ``None``), separate from all of the above.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import repo_root

# How far outside its country's polygon a site may fall before it is reported. The
# polygons are simplified to about 1 km; sampling sites are often on a coast, an island
# or a border; and MalAvi coordinates are commonly rounded to whole minutes (about
# 1.8 km). Ten kilometers passes all of that and still catches a dropped minus sign,
# which moves a point by twice its longitude.
TOLERANCE_KM = 10.0

# Kilometers per degree of latitude; longitude is scaled by cos(latitude).
KM_PER_DEGREE_LAT = 111.32

_POLYGONS = ("reference", "natural_earth", "ne_10m_admin_0_countries_simplified.geojson.gz")
_ALIASES = ("reference", "natural_earth", "country_aliases.csv")

Ring = List[Tuple[float, float]]           # [(lon, lat), ...], closed
Polygon = List[Ring]                       # outer ring first, then holes


# ---------------------------------------------------------------------------------------
# Parsing one coordinate cell
# ---------------------------------------------------------------------------------------

# Degrees, minutes and optional seconds, in any of the glyphs seen in submissions and in
# the release: "11°28.20000'", "-021°46.98000'", "46°14.99167'N", "12°30'15\"S".
_DMS = re.compile(
    r"""^\s*(?P<sign>[-+])?\s*
        (?P<deg>\d{1,3})\s*[°º]\s*
        (?:(?P<min>\d{1,2}(?:[.,]\d+)?)\s*['’′´`]?\s*)?
        (?:(?P<sec>\d{1,2}(?:[.,]\d+)?)\s*["”″]?\s*)?
        (?P<hemi>[NSEWnsew])?\s*$""", re.VERBOSE)
# Decimal degrees, with an optional hemisphere letter: "11.47", "-46.62", "46.62 W".
_DECIMAL = re.compile(r"^\s*(?P<sign>[-+])?\s*(?P<value>\d{1,3}(?:[.,]\d+)?)\s*(?P<hemi>[NSEWnsew])?\s*$")


def parse_coordinate(text: Optional[str], *, is_latitude: bool) -> Optional[float]:
    """One cell -> signed decimal degrees, or None when it does not read.

    South and west may be written as a leading minus or as a hemisphere letter; both
    together (``-12°S``) cancel, as :func:`store_ingest.signed_coordinate` treats them.
    The range is checked (latitude within 90, longitude within 180, minutes and seconds
    below 60) so a cell that is numbers but not a coordinate also returns None.
    """
    value = (text or "").strip()
    if not value:
        return None
    match = _DMS.match(value)
    if match:
        degrees = float(match.group("deg"))
        minutes = float((match.group("min") or "0").replace(",", "."))
        seconds = float((match.group("sec") or "0").replace(",", "."))
        if minutes >= 60 or seconds >= 60:
            return None
        magnitude = degrees + minutes / 60 + seconds / 3600
    else:
        match = _DECIMAL.match(value)
        if not match:
            return None
        magnitude = float(match.group("value").replace(",", "."))
    hemisphere = (match.group("hemi") or "").upper()
    if hemisphere and (hemisphere in "NS") != is_latitude:
        return None            # an N/S letter on a longitude, or E/W on a latitude
    negative = (match.group("sign") == "-") != (hemisphere in ("S", "W"))
    result = -magnitude if negative else magnitude
    limit = 90.0 if is_latitude else 180.0
    return result if abs(result) <= limit else None


# ---------------------------------------------------------------------------------------
# The polygons
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Country:
    admin: str
    polygons: Tuple[Tuple[Tuple[Tuple[float, float], ...], ...], ...]
    bbox: Tuple[float, float, float, float]      # min lon, min lat, max lon, max lat


@lru_cache(maxsize=1)
def _atlas() -> Tuple[Dict[str, Country], Dict[str, str]]:
    """(ADMIN -> Country, lookup name -> ADMIN), read once."""
    path = repo_root().joinpath(*_POLYGONS)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        collection = json.load(handle)
    countries: Dict[str, Country] = {}
    names: Dict[str, str] = {}
    for feature in collection["features"]:
        props = feature["properties"]
        geometry = feature["geometry"]
        raw = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        polygons = []
        lons: List[float] = []
        lats: List[float] = []
        for polygon in raw:
            rings = []
            for ring in polygon:
                pts = [(float(x), float(y)) for x, y in ring]
                if pts and pts[0] != pts[-1]:
                    pts.append(pts[0])
                rings.append(tuple(pts))
                lons.extend(p[0] for p in pts)
                lats.extend(p[1] for p in pts)
            polygons.append(tuple(rings))
        admin = props["ADMIN"]
        countries[admin] = Country(admin=admin, polygons=tuple(polygons),
                                   bbox=(min(lons), min(lats), max(lons), max(lats)))
        # ADMIN is the feature's own name; NAME and NAME_LONG are looked up only when no
        # feature has that ADMIN (so "France" the feature wins over "France" the
        # sovereignty of New Caledonia). SOVEREIGNT is deliberately not consulted.
        for key in ("NAME", "NAME_LONG"):
            names.setdefault(props[key], admin)
    for admin in countries:
        names[admin] = admin
    aliases_path = repo_root().joinpath(*_ALIASES)
    if aliases_path.exists():
        with open(aliases_path, encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                target = (row.get("natural_earth_admin") or "").strip()
                source = (row.get("malavi_country") or "").strip()
                if source and target in countries:
                    names[source] = target
    return countries, names


def country_for(name: str) -> Optional[Country]:
    """The Natural Earth feature for a MalAvi country spelling, or None."""
    countries, names = _atlas()
    admin = names.get((name or "").strip())
    return countries.get(admin) if admin else None


def atlas_version() -> str:
    return "Natural Earth 1:10m Admin 0 countries, v5.1.1, simplified to ~1 km"


# ---------------------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------------------

def _in_ring(lon: float, lat: float, ring: Sequence[Tuple[float, float]]) -> bool:
    """Ray casting: is (lon, lat) inside this closed ring?"""
    inside = False
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_cross:
                inside = not inside
    return inside


def contains(country: Country, lon: float, lat: float) -> bool:
    """Inside any outer ring and not inside that polygon's holes."""
    x0, y0, x1, y1 = country.bbox
    if not (x0 <= lon <= x1 and y0 <= lat <= y1):
        return False
    for polygon in country.polygons:
        if _in_ring(lon, lat, polygon[0]):
            if not any(_in_ring(lon, lat, hole) for hole in polygon[1:]):
                return True
    return False


def _segment_km(lon: float, lat: float, a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Distance in km from a point to a segment, on a locally flat earth."""
    kx = KM_PER_DEGREE_LAT * math.cos(math.radians(lat))
    ky = KM_PER_DEGREE_LAT
    px, py = 0.0, 0.0
    ax, ay = (a[0] - lon) * kx, (a[1] - lat) * ky
    bx, by = (b[0] - lon) * kx, (b[1] - lat) * ky
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(ax - px, ay - py)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def distance_km(country: Country, lon: float, lat: float) -> float:
    """Kilometers from the point to the nearest boundary of the country (0 if inside)."""
    if contains(country, lon, lat):
        return 0.0
    best = math.inf
    for polygon in country.polygons:
        for ring in polygon:
            for i in range(len(ring) - 1):
                d = _segment_km(lon, lat, ring[i], ring[i + 1])
                if d < best:
                    best = d
    return best


def country_containing(lon: float, lat: float) -> Optional[str]:
    """The ADMIN name of whichever feature contains the point, or None (open water)."""
    countries, _ = _atlas()
    for admin, country in countries.items():
        if contains(country, lon, lat):
            return admin
    return None


# ---------------------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SiteCheck:
    verdict: str                              # inside | near | outside | country_unknown
    country: str                              # as the submitter wrote it
    admin: Optional[str]                      # the feature tested against
    lat: float
    lon: float
    distance_km: Optional[float] = None       # from the named country, when outside/near
    found_in: Optional[str] = None            # the country the point is actually in
    fix: Optional[str] = None                 # a single edit that would put it inside

    def as_dict(self) -> Dict[str, object]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


_FIXES = (
    ("negate the longitude", lambda lat, lon: (lat, -lon)),
    ("negate the latitude", lambda lat, lon: (-lat, lon)),
    ("negate both", lambda lat, lon: (-lat, -lon)),
    ("swap latitude and longitude", lambda lat, lon: (lon, lat)),
)


def check_site(country_name: str, lat: float, lon: float,
               tolerance_km: float = TOLERANCE_KM) -> SiteCheck:
    """Test one (country, lat, lon). See the module docstring for the verdicts."""
    country = country_for(country_name)
    if country is None:
        return SiteCheck("country_unknown", country_name, None, lat, lon)
    if contains(country, lon, lat):
        return SiteCheck("inside", country_name, country.admin, lat, lon, distance_km=0.0)
    distance = distance_km(country, lon, lat)
    if distance <= tolerance_km:
        return SiteCheck("near", country_name, country.admin, lat, lon, distance_km=distance)
    fix = None
    for label, transform in _FIXES:
        flat, flon = transform(lat, lon)
        if abs(flat) <= 90 and abs(flon) <= 180 and distance_km(country, flon, flat) <= tolerance_km:
            fix = label
            break
    return SiteCheck("outside", country_name, country.admin, lat, lon,
                     distance_km=distance, found_in=country_containing(lon, lat), fix=fix)
