#!/usr/bin/env python3
# @title Build the interactive map's land layer from Natural Earth 1:10m
# @purpose Write docs/assets/data/world_land.json: every country's outline at a resolution
#   that keeps small islands, as compact rings the map view draws under its points.
# @why The site's basemap (world_map.json) is the 1:110m layer the front-page choropleths
#   need. At that scale whole island groups do not exist: the Lesser Antilles, Hawaii,
#   the Canaries, the Galapagos. Sampling sites there were drawn over open sea.
# @input reference/natural_earth/ne_10m_admin_0_countries_simplified.geojson.gz
# @output docs/assets/data/world_land.json
# @program python3
# @critical-var SOURCE
# @critical-var TOLERANCE_DEGREES
# @critical-var DECIMALS
# @critical-var MIN_RING_VERTICES
"""Build the interactive map's land layer.

    python3 export/build_land_layer.py            # write docs/assets/data/world_land.json
    python3 export/build_land_layer.py --dry-run  # report what it would write

The source is the Natural Earth 1:10m "Admin 0 - Countries" layer already kept in
``reference/natural_earth/`` for the submission screen's coordinate check (public domain;
its README records the version, the checksum and how it was simplified). This program
takes the OUTER ring of every polygon of every country, simplifies it a little further,
and writes the rings as flat ``[lon, lat, lon, lat, ...]`` arrays.

Three decisions worth knowing:

* **Outer rings only.** A hole in a country polygon is another country (Lesotho inside
  South Africa, San Marino inside Italy), which is drawn by its own outer ring. Lakes are
  not holes in this layer. So the union of the outer rings is the land.
* **No ring is ever dropped for being small.** Simplification removes vertices from a
  ring; it never removes the ring. An island smaller than the tolerance comes out as the
  few vertices that survive, never as nothing: small islands are the reason this file
  exists.
* **Country outlines, not one merged landmass**, so borders are drawn as they are on the
  1:110m basemap this layer replaces in the map view.

The file is used only by the interactive map (the "Map" view). The front-page maps and
the country gazetteer keep reading world_map.json, which this program does not touch.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence, Tuple

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "reference" / "natural_earth" / "ne_10m_admin_0_countries_simplified.geojson.gz"
OUTPUT = REPO / "docs" / "assets" / "data" / "world_land.json"

# How far a vertex may sit from the simplified outline before it is kept, in degrees.
# 0.02 degrees is about 2 km. The map's closest zoom draws about 0.005 degrees per pixel
# at the equator, so the outline is true to a few pixels at the closest zoom and exact at
# every wider one. The source was already simplified at 0.01.
TOLERANCE_DEGREES = 0.02

# Coordinates are written to this many decimal places: 0.001 degree is about 100 m.
DECIMALS = 3

# A ring keeps at least this many vertices however small it is (a triangle and its
# closing point are four in GeoJSON; the closing point is not written, so three).
MIN_RING_VERTICES = 3

Point = Tuple[float, float]


def simplify(ring: Sequence[Point], tolerance: float) -> List[Point]:
    """Douglas-Peucker on an open ring, iteratively (no recursion limit to hit).

    The ring is split at its first vertex and at the vertex farthest from it, so that the
    two halves are open lines with distinct ends, which is what the algorithm needs.
    """
    if len(ring) <= MIN_RING_VERTICES:
        return list(ring)

    def squared_distance(p: Point, a: Point, b: Point) -> float:
        (px, py), (ax, ay), (bx, by) = p, a, b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return (px - ax) ** 2 + (py - ay) ** 2
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return (px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2

    def line(points: Sequence[Point]) -> List[int]:
        """Indexes of the vertices of an open line that survive simplification."""
        keep = [False] * len(points)
        keep[0] = keep[-1] = True
        stack = [(0, len(points) - 1)]
        limit = tolerance * tolerance
        while stack:
            first, last = stack.pop()
            worst, at = 0.0, -1
            for i in range(first + 1, last):
                d = squared_distance(points[i], points[first], points[last])
                if d > worst:
                    worst, at = d, i
            if at >= 0 and worst > limit:
                keep[at] = True
                stack.append((first, at))
                stack.append((at, last))
        return [i for i, kept in enumerate(keep) if kept]

    far = max(range(len(ring)),
              key=lambda i: (ring[i][0] - ring[0][0]) ** 2 + (ring[i][1] - ring[0][1]) ** 2)
    if far == 0:
        return list(ring[:MIN_RING_VERTICES])
    first_half = [ring[i] for i in line(ring[:far + 1])]
    # The second half runs from the farthest vertex back round to the first one.
    closing = list(ring[far:]) + [ring[0]]
    second_half = [closing[i] for i in line(closing)]
    # Its two ends are already in hand: the farthest vertex ends the first half and the
    # first vertex begins it.
    out = first_half + second_half[1:-1]
    if len(out) < MIN_RING_VERTICES:
        # Too small to survive at this tolerance: keep it as its first, farthest and one
        # more vertex rather than lose the island.
        middle = ring[far // 2] if far >= 2 else ring[-1]
        out = [ring[0], middle, ring[far]]
    return out


def outer_rings(geometry: dict) -> List[List[Point]]:
    """The outer ring of every polygon of one feature, opened (no repeated last vertex)."""
    if geometry["type"] == "Polygon":
        polygons = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        return []
    rings = []
    for polygon in polygons:
        ring = [(float(x), float(y)) for x, y in polygon[0]]
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) >= MIN_RING_VERTICES:
            rings.append(ring)
    return rings


def build() -> dict:
    if not SOURCE.is_file():
        raise SystemExit(f"No Natural Earth layer at {SOURCE}. See its README for the source.")
    with gzip.open(SOURCE, "rt", encoding="utf-8") as handle:
        source = json.load(handle)

    rings_out: List[List[float]] = []
    vertices_in = vertices_out = 0
    for feature in source["features"]:
        for ring in outer_rings(feature["geometry"]):
            vertices_in += len(ring)
            kept = simplify(ring, TOLERANCE_DEGREES)
            # Rounding can make neighbors equal; drop the repeats, never the ring.
            rounded: List[Point] = []
            for x, y in kept:
                point = (round(x, DECIMALS), round(y, DECIMALS))
                if not rounded or rounded[-1] != point:
                    rounded.append(point)
            if len(rounded) > 1 and rounded[0] == rounded[-1]:
                rounded.pop()
            if len(rounded) < MIN_RING_VERTICES:
                # An island that rounds to a point or a line: draw it as a triangle about
                # 300 m across around where it is, so it is on the map at all.
                x, y = rounded[0]
                step = 3 * 10 ** -DECIMALS
                rounded = [(x, y), (round(x + step, DECIMALS), y),
                           (x, round(y + step, DECIMALS))]
            vertices_out += len(rounded)
            rings_out.append([value for point in rounded for value in point])

    return {
        "source": "Natural Earth 1:10m Admin 0 - Countries, version 5.1.1 (public domain)",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tolerance_degrees": TOLERANCE_DEGREES,
        "n_countries": len(source["features"]),
        "n_rings": len(rings_out),
        "n_vertices": vertices_out,
        "n_vertices_source": vertices_in,
        # Each ring is a flat [lon, lat, lon, lat, ...] array, open (the first vertex is
        # not repeated at the end).
        "rings": rings_out,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be written; write nothing")
    arguments = parser.parse_args(argv)

    print("== malavi_rebuild :: build_land_layer ==")
    print(f"source : {SOURCE.relative_to(REPO)}")
    layer = build()
    text = json.dumps(layer, separators=(",", ":"))
    print(f"countries: {layer['n_countries']}; rings: {layer['n_rings']}; vertices: "
          f"{layer['n_vertices']:,} (of {layer['n_vertices_source']:,} in the source)")
    print(f"size   : {len(text) / 1e6:.2f} MB, "
          f"{len(gzip.compress(text.encode('utf-8'))) / 1e6:.2f} MB gzipped")
    if arguments.dry_run:
        print(f"[dry-run] nothing written. Would write {OUTPUT.relative_to(REPO)}.")
        return 0
    OUTPUT.write_text(text + "\n", encoding="utf-8")
    print(f"wrote  : {OUTPUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
