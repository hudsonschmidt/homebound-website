#!/usr/bin/env python3
"""Build Homebound's per-resort ski trail bundles from OpenSkiData.

This is the published method for the ODbL 1.0 derivative database served at
https://ski.homeboundapp.com (see README.md next to this file, or
https://www.homeboundapp.com/skidata/).

What it does, once a week (GitHub Actions, .github/workflows/ski-bundles.yml):

1. Fetch https://tiles.openskimap.org/metadata.json. If OpenSkiData's build is the one we
   already built from (recorded in v1/build.json), stop: nothing to do.
2. Download https://tiles.openskimap.org/openskidata.gpkg (about 412 MB; at most once a day,
   per OpenSkiData's rules for automated downloads).
3. Keep the operating ski areas with downhill skiing, their downhill runs and their operating
   lifts. Write one bundle per area (v1/areas/{area_key}.json), an index of every area
   (v1/index.json), then v1/build.json.
4. With --upload, put each object in the R2 bucket gzip-compressed, skipping the ones whose
   content hash hasn't changed.

Run it locally:

    pip install -r skidata/requirements.txt
    python skidata/build_bundles.py --out /tmp/ski-bundles
    python skidata/build_bundles.py --gpkg openskidata.gpkg --only wd-Q3821979 --out /tmp/ski

The JSON format is documented in README.md. Change it only together with the backend
(src/services/ski_resorts.py) and the iOS app (SkiResortStore), and bump BUILDER_VERSION.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable
from typing import Any

import numpy as np
import pyogrio
import shapely
from pyogrio.raw import read as ogr_read

# --- constants -------------------------------------------------------------------------

METADATA_URL = "https://tiles.openskimap.org/metadata.json"
GPKG_URL = "https://tiles.openskimap.org/openskidata.gpkg"
USER_AGENT = "HomeboundSkiBundles/1 (+https://www.homeboundapp.com/skidata/)"

#: Bumped whenever the bundle format or the build rules change. A new value forces a full
#: rebuild even when OpenSkiData hasn't published a new build.
#: 2 (2026-11-15): OpenSkiData's full required credit in `attribution`; DISPLAY_NAMES.
BUILDER_VERSION = 2
#: The schema version inside every JSON object ("v"). Object keys live under v1/.
SCHEMA_VERSION = 1
#: The OpenSkiData format major version this script was written against. A different one
#: logs a warning; missing layers or columns fail the build outright.
TESTED_FORMAT_MAJOR = 16

LICENSE = "ODbL-1.0"
#: OpenSkiData's required credit, in full (its licence asks for every source it draws on).
ATTRIBUTION = ("Data from OpenSkiData / OpenSkiMap.org, © OpenStreetMap contributors (ODbL), "
               "Skimap.org, Who's On First, © Mapterhorn")

#: Display names that replace the source name of an area, by area key, in its bundle and
#: its index entry. Only for names people would not recognise; the better fix is an
#: OpenStreetMap edit (see README "Display names"), after which the entry can go.
DISPLAY_NAMES: dict[str, str] = {
    # OSM's `name` is the pre-2015 "Park City Mountain Resort"; the resort calls itself
    # "Park City Mountain" (parkcitymountain.com).
    "wd-Q3821979": "Park City Mountain",
}

PREFIX = "v1"
BUILD_KEY = f"{PREFIX}/build.json"
INDEX_KEY = f"{PREFIX}/index.json"
AREAS_PREFIX = f"{PREFIX}/areas/"

CONTENT_TYPE = "application/json"
CONTENT_ENCODING = "gzip"
CACHE_CONTROL = "public, max-age=3600"

SIMPLIFY_M = 5.0          # run, lift and bundle-hull geometry (Ramer-Douglas-Peucker)
INDEX_SIMPLIFY_M = 50.0   # hulls in index.json
HULL_BUFFER_M = 300.0     # around the convex hull of runs + lifts, when an area has no polygon
#: An area polygon that holds less than this share of its own run and lift vertices is
#: joined with the buffered convex hull. Many OSM ski-area polygons outline only a base
#: area or one sector (375 of 2,816 in the 2026-11-13 build; Ylläs's held 1% of its runs).
POLYGON_MIN_COVERAGE = 0.9
LIFT_REVERSE_MIN_M = 5.0  # a lift drawn downhill by at least this much is flipped bottom -> top
HASH_CHARS = 16           # hex characters of SHA-256 kept as an object's content hash

#: Below this many areas the source is assumed broken and nothing is published.
MIN_AREAS = 1000
#: A new index smaller than this share of the previous one is refused (--allow-shrink).
MAX_SHRINK = 0.8

EARTH_RADIUS_M = 6371008.8
UPLOAD_WORKERS = 16

#: Layers and columns this script reads. OpenSkiData says its format is "not stable": if any
#: of these disappear, the build fails loudly instead of publishing something half empty.
REQUIRED_LAYERS: dict[str, dict[str, Any]] = {
    "ski_areas_point": {
        "geometry": "Point",
        "fields": [
            "feature_id", "name", "status", "sources", "websites", "wikidata_id",
            "activities", "min_elevation", "max_elevation", "run_convention",
        ],
    },
    "ski_areas_multipolygon": {
        "geometry": "MultiPolygon",
        "fields": ["feature_id"],
    },
    "runs_linestring": {
        "geometry": "LineString",
        "fields": [
            "name", "status", "sources", "uses", "ref", "difficulty",
            "difficulty_convention", "oneway", "ski_area_ids",
        ],
    },
    "lifts_linestring": {
        "geometry": "LineString",
        "fields": ["name", "status", "sources", "lift_type", "ski_area_ids"],
    },
}

OPERATING = "operating"
DOWNHILL = "downhill"

KEY_RE = re.compile(r"^[a-z]+-[A-Za-z0-9]+$")
WIKIDATA_RE = re.compile(r"^Q[1-9][0-9]*$")
OSM_TYPE_LETTER = {"relation": "r", "way": "w", "node": "n"}
OSM_TYPE_RANK = {"r": 0, "w": 1, "n": 2}


class SchemaError(RuntimeError):
    """The GeoPackage (or metadata) isn't shaped the way this script expects."""


class PublishRefused(RuntimeError):
    """A sanity check stopped the upload."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- polyline (identical to the backend's route_stats.encode_line) ---------------------

def _round5(value: float) -> int:
    """Math.round(value * 1e5), as the reference encoder does (half up)."""
    return int(math.floor(value * 1e5 + 0.5))


def _encode_value(v: int) -> str:
    v = ~(v << 1) if v < 0 else v << 1
    chunks = []
    while v >= 0x20:
        chunks.append(chr((0x20 | (v & 0x1F)) + 63))
        v >>= 5
    chunks.append(chr(v + 63))
    return "".join(chunks)


def encode_line(points: Iterable[tuple[float, float]]) -> str:
    """Google encoded polyline, precision 5, from (lat, lon) pairs.

    Byte-for-byte the backend's ``src/services/route_stats.encode_line``, so the backend's
    ``decode_line`` and the app's ``RoutePolyline`` read bundle geometry unchanged.
    """
    out = []
    plat = plon = 0
    for lat, lon in points:
        ilat, ilon = _round5(lat), _round5(lon)
        out.append(_encode_value(ilat - plat))
        out.append(_encode_value(ilon - plon))
        plat, plon = ilat, ilon
    return "".join(out)


def decode_line(encoded: str) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    n = len(encoded)
    while index < n:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coords.append((lat / 1e5, lon / 1e5))
    return coords


def _dedupe_rounded(latlon: np.ndarray) -> list[tuple[float, float]]:
    """Drop consecutive points that round to the same 1e-5 cell (zero-length deltas)."""
    out: list[tuple[float, float]] = []
    last: tuple[int, int] | None = None
    for lat, lon in latlon:
        cell = (_round5(float(lat)), _round5(float(lon)))
        if cell != last:
            out.append((float(lat), float(lon)))
            last = cell
    return out


# --- local metric projection -----------------------------------------------------------
# Every feature is simplified in a local equirectangular projection centred on a reference
# point (the feature's own first vertex, or the area's point). Ski areas are a few km
# across, so the distortion is far below the 5 m tolerance.

def _to_local(lon: np.ndarray, lat: np.ndarray, lon0: Any, lat0: Any) -> tuple[np.ndarray, np.ndarray]:
    x = np.radians(lon - lon0) * EARTH_RADIUS_M * np.cos(np.radians(lat0))
    y = np.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def _from_local(x: np.ndarray, y: np.ndarray, lon0: Any, lat0: Any) -> tuple[np.ndarray, np.ndarray]:
    lat = lat0 + np.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + np.degrees(x / (EARTH_RADIUS_M * np.cos(np.radians(lat0))))
    return lon, lat


def _project_geom(geom: shapely.Geometry, lon0: float, lat0: float) -> shapely.Geometry:
    return shapely.transform(geom, lambda c: np.column_stack(_to_local(c[:, 0], c[:, 1], lon0, lat0)))


def _unproject_geom(geom: shapely.Geometry, lon0: float, lat0: float) -> shapely.Geometry:
    return shapely.transform(geom, lambda c: np.column_stack(_from_local(c[:, 0], c[:, 1], lon0, lat0)))


def simplify_lines(geoms: np.ndarray, tolerance_m: float) -> list[np.ndarray | None]:
    """Simplify LineStrings at ``tolerance_m`` metres; returns (lat, lon) arrays.

    Vectorised over the whole array. Each line is projected around its own first vertex,
    so a run shared by two areas simplifies identically in both bundles. Lines with fewer
    than two vertices come back as None.
    """
    out: list[np.ndarray | None] = [None] * len(geoms)
    if len(geoms) == 0:
        return out
    ok = ~shapely.is_missing(geoms) & (shapely.get_num_coordinates(geoms) >= 2)
    ok &= shapely.get_type_id(geoms) == 1  # LineString
    keep = np.flatnonzero(ok)
    if len(keep) == 0:
        return out
    coords, idx = shapely.get_coordinates(geoms[keep], return_index=True)
    first = np.searchsorted(idx, np.arange(len(keep)))
    lon0, lat0 = coords[first, 0][idx], coords[first, 1][idx]
    x, y = _to_local(coords[:, 0], coords[:, 1], lon0, lat0)
    local = shapely.linestrings(np.column_stack([x, y]), indices=idx)
    simple = shapely.simplify(local, tolerance_m, preserve_topology=True)
    c2, i2 = shapely.get_coordinates(simple, return_index=True)
    ref_lon, ref_lat = coords[first, 0], coords[first, 1]
    lon, lat = _from_local(c2[:, 0], c2[:, 1], ref_lon[i2], ref_lat[i2])
    bounds = np.searchsorted(i2, np.arange(len(keep) + 1))
    latlon = np.column_stack([lat, lon])
    for j, g in enumerate(keep):
        seg = latlon[bounds[j]:bounds[j + 1]]
        out[g] = seg if len(seg) >= 2 else None
    return out


def _polygon_rings(geom: shapely.Geometry) -> list[list[tuple[float, float]]]:
    """Every ring (exterior and holes) of a (Multi)Polygon as (lat, lon), open-ended."""
    rings: list[list[tuple[float, float]]] = []
    for poly in getattr(geom, "geoms", [geom]):
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        for ring in [poly.exterior, *poly.interiors]:
            pts = _dedupe_rounded(np.asarray(ring.coords)[:, [1, 0]])
            if len(pts) >= 2 and (_round5(pts[0][0]), _round5(pts[0][1])) == (_round5(pts[-1][0]), _round5(pts[-1][1])):
                pts = pts[:-1]
            if len(pts) >= 3:
                rings.append(pts)
    return rings


def _encode_rings(rings: list[list[tuple[float, float]]]) -> list[str]:
    return [encode_line(r) for r in rings]


# --- source reading ------------------------------------------------------------------------

@dataclass
class Source:
    """The parts of the GeoPackage this script uses, as plain columns."""

    areas: dict[str, np.ndarray]
    area_points: np.ndarray            # shapely Points
    area_polygons: dict[str, shapely.Geometry]
    runs: dict[str, np.ndarray]
    run_geoms: np.ndarray              # shapely LineStrings (with Z)
    lifts: dict[str, np.ndarray]
    lift_geoms: np.ndarray
    skipped_polygon_runs: int = 0


def check_schema(gpkg: Path) -> None:
    """Fail loudly when a layer, a column or a geometry type this script relies on is gone."""
    try:
        layers = {str(name): str(gtype) for name, gtype in pyogrio.list_layers(gpkg)}
    except Exception as exc:  # pyogrio raises DataSourceError for non-GeoPackages
        raise SchemaError(f"{gpkg} can't be opened as a GeoPackage: {exc}") from exc
    problems: list[str] = []
    for layer, want in REQUIRED_LAYERS.items():
        if layer not in layers:
            problems.append(f"layer {layer!r} is missing (found: {sorted(layers)})")
            continue
        info = pyogrio.read_info(gpkg, layer=layer)
        gtype = str(info.get("geometry_type") or "")
        if not gtype.lower().startswith(want["geometry"].lower()):
            problems.append(f"layer {layer!r} has geometry {gtype!r}, expected {want['geometry']!r}")
        fields = set(info["fields"])
        missing = [c for c in want["fields"] if c not in fields]
        if missing:
            problems.append(f"layer {layer!r} is missing column(s) {missing}")
        crs = str(info.get("crs") or "")
        if crs and crs.upper() not in ("EPSG:4326", "OGC:CRS84"):
            problems.append(f"layer {layer!r} is in {crs}, expected EPSG:4326")
    if problems:
        raise SchemaError(
            "OpenSkiData's GeoPackage no longer matches what build_bundles.py expects:\n  - "
            + "\n  - ".join(problems)
            + "\nUpdate REQUIRED_LAYERS and the reader together, then bump BUILDER_VERSION."
        )


def _read_layer(gpkg: Path, layer: str, read_geometry: bool = True) -> tuple[dict[str, np.ndarray], np.ndarray | None]:
    want = REQUIRED_LAYERS[layer]["fields"]
    meta, _fids, geometry, fields = ogr_read(gpkg, layer=layer, columns=want, read_geometry=read_geometry)
    cols = dict(zip(meta["fields"], fields))
    for c in want:  # columns= silently drops unknown names; check_schema should have caught it
        if c not in cols:
            raise SchemaError(f"layer {layer!r} came back without column {c!r}")
    geoms = shapely.from_wkb(geometry) if read_geometry else None
    return cols, geoms


def load_source(gpkg: Path) -> Source:
    check_schema(gpkg)
    t0 = time.monotonic()
    areas, area_points = _read_layer(gpkg, "ski_areas_point")
    poly_cols, poly_geoms = _read_layer(gpkg, "ski_areas_multipolygon")
    polygons = {str(fid): g for fid, g in zip(poly_cols["feature_id"], poly_geoms) if g is not None and not g.is_empty}
    runs, run_geoms = _read_layer(gpkg, "runs_linestring")
    lifts, lift_geoms = _read_layer(gpkg, "lifts_linestring")
    skipped_polygon_runs = 0
    if "runs_multipolygon" in {str(n) for n, _ in pyogrio.list_layers(gpkg)}:
        try:
            meta, _f, _g, flds = ogr_read(gpkg, layer="runs_multipolygon", columns=["uses"], read_geometry=False)
            skipped_polygon_runs = sum(1 for u in flds[0] if DOWNHILL in _split(u))
        except Exception:  # only a statistic
            skipped_polygon_runs = -1
    log(f"read {len(area_points)} areas, {len(polygons)} area polygons, {len(run_geoms)} runs, "
        f"{len(lift_geoms)} lifts in {time.monotonic() - t0:.1f}s")
    return Source(areas, area_points, polygons, runs, run_geoms, lifts, lift_geoms, skipped_polygon_runs)


def _split(value: Any, sep: str = ",") -> list[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    return [v.strip() for v in str(value).split(sep) if v.strip()]


def _str_or_none(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    s = str(value).strip()
    return s or None


def _float_or_none(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, digits)


def _json_list(value: Any, what: str) -> list[Any]:
    if value is None or value == "":
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{what} isn't JSON: {value!r}") from exc
    if not isinstance(parsed, list):
        raise SchemaError(f"{what} isn't a JSON list: {value!r}")
    return parsed


def osm_ref(sources_json: Any, what: str = "sources") -> str | None:
    """The feature's stable OpenStreetMap id, e.g. ``w260786241``.

    OpenSkiData merges ways, so a feature can carry several OSM sources; the lowest
    (relation before way before node, then the smallest number) is kept, so the id only
    changes if that exact OSM object is deleted.
    """
    best: tuple[int, int, str] | None = None
    for src in _json_list(sources_json, what):
        if not isinstance(src, dict) or "type" not in src or "id" not in src:
            raise SchemaError(f"{what} entry isn't {{type, id}}: {src!r}")
        if src["type"] != "openstreetmap":
            continue
        kind, _, num = str(src["id"]).partition("/")
        letter = OSM_TYPE_LETTER.get(kind)
        if letter is None or not num.isdigit():
            raise SchemaError(f"unexpected OpenStreetMap id {src['id']!r} in {what}")
        cand = (OSM_TYPE_RANK[letter], int(num), f"{letter}{int(num)}")
        if best is None or cand < best:
            best = cand
    return best[2] if best else None


def _skimap_ref(sources_json: Any) -> str | None:
    ids = []
    for src in _json_list(sources_json, "ski area sources"):
        if isinstance(src, dict) and src.get("type") == "skimap.org" and str(src.get("id", "")).isdigit():
            ids.append(int(src["id"]))
    return str(min(ids)) if ids else None


def _osm_sort_key(ref: str) -> tuple[int, int]:
    return (OSM_TYPE_RANK[ref[0]], int(ref[1:]))


def area_key(wikidata_id: Any, sources_json: Any, member_osm_ids: Iterable[str] = (),
             shared_osm_ids: frozenset[str] | set[str] = frozenset()) -> str | None:
    """A key that survives OpenSkiData rebuilds (its feature ids are content hashes).

    In order: ``wd-{Wikidata id}``; ``osm-{r|w|n}{id}`` from the area's own OpenStreetMap
    object; ``sm-{id}`` from Skimap.org; else ``gen-{r|w|n}{id}``, the lowest OSM id among
    the area's runs and lifts (OpenSkiData generates such areas around unclaimed runs),
    preferring one that belongs to no other area (``shared_osm_ids``) so two generated
    areas that share a run don't collide.
    """
    wd = _str_or_none(wikidata_id)
    if wd and WIKIDATA_RE.match(wd):
        return f"wd-{wd}"
    osm = osm_ref(sources_json, "ski area sources")
    if osm:
        return f"osm-{osm}"
    sm = _skimap_ref(sources_json)
    if sm:
        return f"sm-{sm}"
    members = sorted(set(member_osm_ids), key=_osm_sort_key)
    exclusive = [m for m in members if m not in shared_osm_ids]
    if exclusive:
        return f"gen-{exclusive[0]}"
    if members:
        return f"gen-{members[0]}"
    return None


def run_color(convention: str | None, difficulty: str | None) -> str:
    """OpenSkiData's ``getRunColorName`` (openskidata-format src/Run.ts), ported.

    The GeoPackage doesn't carry the colour, so it's derived the way OpenSkiMap does it.
    """
    if convention == "europe":
        table = {"novice": "green", "easy": "blue", "intermediate": "red",
                 "advanced": "black", "expert": "black", "freeride": "orange", "extreme": "orange"}
    elif convention == "japan":
        table = {"novice": "green", "easy": "green", "intermediate": "red",
                 "advanced": "black", "expert": "black", "freeride": "orange", "extreme": "orange"}
    else:  # north_america (and anything unknown, as upstream's switch has no other branch)
        table = {"novice": "green", "easy": "green", "intermediate": "blue",
                 "advanced": "black", "expert": "black", "freeride": "orange", "extreme": "orange"}
    return table.get(difficulty or "", "grey")


# --- bundle building -------------------------------------------------------------------------

@dataclass
class AreaOut:
    key: str
    name: str | None
    raw: bytes                 # the bundle JSON (UTF-8, compact)
    hash: str
    gz_bytes: int
    index_entry: dict[str, Any]
    runs: int
    lifts: int
    named_runs: int
    graded_runs: int
    named_lifts: int


@dataclass
class BuildResult:
    areas: dict[str, AreaOut] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def gzip_bytes(raw: bytes) -> bytes:
    """Deterministic gzip (no timestamp), so identical JSON gives identical objects."""
    return gzip.compress(raw, compresslevel=9, mtime=0)


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:HASH_CHARS]


def _bbox(latlons: Iterable[np.ndarray]) -> list[float] | None:
    arrs = [a for a in latlons if a is not None and len(a)]
    if not arrs:
        return None
    allp = np.vstack(arrs)
    lat, lon = allp[:, 0], allp[:, 1]
    return [round(float(lat.min()), 5), round(float(lon.min()), 5),
            round(float(lat.max()), 5), round(float(lon.max()), 5)]


def _hull_geometry(polygon: shapely.Geometry | None, member_coords: list[np.ndarray],
                   lon0: float, lat0: float) -> tuple[shapely.Geometry | None, str]:
    """The area's hull in local metres, and which rule made it.

    - ``polygon``: the area's own polygon, when it holds at least POLYGON_MIN_COVERAGE of
      the area's run and lift vertices;
    - ``polygon+runs``: that polygon joined with the runs + lifts' convex hull buffered
      300 m, when it holds less;
    - ``runs``: no polygon, so the runs + lifts' convex hull buffered 300 m.
    """
    member_hull = None
    x = y = None
    if member_coords:
        pts = np.vstack(member_coords)  # (lon, lat)
        x, y = _to_local(pts[:, 0], pts[:, 1], lon0, lat0)
        member_hull = shapely.convex_hull(shapely.multipoints(np.column_stack([x, y]))).buffer(HULL_BUFFER_M, quad_segs=4)
    if polygon is not None and not polygon.is_empty:
        local = _project_geom(shapely.force_2d(polygon), lon0, lat0)
        if not local.is_valid:
            local = shapely.make_valid(local)
            local = shapely.union_all([g for g in getattr(local, "geoms", [local]) if g.geom_type in ("Polygon", "MultiPolygon")])
        if not local.is_empty:
            if member_hull is None:
                return local, "polygon"
            coverage = float(shapely.contains_xy(local, x, y).mean())
            if coverage >= POLYGON_MIN_COVERAGE:
                return local, "polygon"
            return shapely.union(local, member_hull), "polygon+runs"
    if member_hull is None:
        return None, "none"
    return member_hull, "runs"


def _simplified_rings(local: shapely.Geometry, tolerance_m: float, lon0: float, lat0: float) -> list[list[tuple[float, float]]]:
    simple = shapely.simplify(local, tolerance_m, preserve_topology=True)
    if simple.is_empty:
        simple = local
    rings = _polygon_rings(_unproject_geom(simple, lon0, lat0))
    if not rings:  # a sliver that simplified away: fall back to the unsimplified shape
        rings = _polygon_rings(_unproject_geom(local, lon0, lat0))
    return rings


def display_name(key: str, source_name: str | None) -> str | None:
    """The name an area is published under: its DISPLAY_NAMES entry, else the source's."""
    return DISPLAY_NAMES.get(key, source_name)


def build(source: Source, only: str | None = None, min_areas: int | None = None) -> BuildResult:
    t0 = time.monotonic()
    min_areas = MIN_AREAS if min_areas is None else min_areas
    A = source.areas
    n_areas = len(A["feature_id"])

    # Areas: operating, with downhill skiing.
    selected = [
        i for i in range(n_areas)
        if A["status"][i] == OPERATING and DOWNHILL in _split(A["activities"][i])
    ]
    if not selected:
        raise SchemaError("no operating downhill ski areas found: has the status/activities vocabulary changed?")
    sel_ids = {str(A["feature_id"][i]): i for i in selected}

    # Runs: downhill use, operating, in at least one selected area.
    R = source.runs
    run_rows = [
        j for j in range(len(source.run_geoms))
        if R["status"][j] == OPERATING and DOWNHILL in _split(R["uses"][j])
        and any(a in sel_ids for a in _split(R["ski_area_ids"][j]))
    ]
    # Lifts: operating, in at least one selected area.
    L = source.lifts
    lift_rows = [
        j for j in range(len(source.lift_geoms))
        if L["status"][j] == OPERATING and any(a in sel_ids for a in _split(L["ski_area_ids"][j]))
    ]
    log(f"selected {len(selected)} areas, {len(run_rows)} downhill runs, {len(lift_rows)} lifts")

    run_simple = simplify_lines(source.run_geoms[run_rows], SIMPLIFY_M)
    lift_simple = simplify_lines(source.lift_geoms[lift_rows], SIMPLIFY_M)
    log(f"simplified geometry in {time.monotonic() - t0:.1f}s")

    members_runs: dict[str, list[int]] = {}
    members_lifts: dict[str, list[int]] = {}
    run_items: list[dict[str, Any] | None] = []
    lift_items: list[dict[str, Any] | None] = []
    run_raw_xy: list[np.ndarray | None] = []
    lift_raw_xy: list[np.ndarray | None] = []
    dropped_degenerate = 0

    for k, j in enumerate(run_rows):
        latlon = run_simple[k]
        osm = osm_ref(R["sources"][j], "run sources")
        pts = _dedupe_rounded(latlon) if latlon is not None else []
        if osm is None or len(pts) < 2:
            dropped_degenerate += 1
            run_items.append(None)
            run_raw_xy.append(None)
            continue
        difficulty = _str_or_none(R["difficulty"][j])
        convention = _str_or_none(R["difficulty_convention"][j])
        run_items.append({
            "osm_id": osm,
            "name": _str_or_none(R["name"][j]),
            "ref": _str_or_none(R["ref"][j]),
            "difficulty": difficulty,
            "color": run_color(convention, difficulty),
            "oneway": bool(R["oneway"][j]) if R["oneway"][j] is not None else False,
            "geom": encode_line(pts),
            "_bbox": latlon,
        })
        run_raw_xy.append(shapely.get_coordinates(source.run_geoms[j]))
        idx = len(run_items) - 1
        for a in _split(R["ski_area_ids"][j]):
            if a in sel_ids:
                members_runs.setdefault(a, []).append(idx)

    for k, j in enumerate(lift_rows):
        latlon = lift_simple[k]
        osm = osm_ref(L["sources"][j], "lift sources")
        if osm is None or latlon is None:
            dropped_degenerate += 1
            lift_items.append(None)
            lift_raw_xy.append(None)
            continue
        z = shapely.get_coordinates(source.lift_geoms[j], include_z=True)[:, 2]
        z0 = _float_or_none(z[0]) if len(z) else None
        z1 = _float_or_none(z[-1]) if len(z) else None
        if z0 is not None and z1 is not None and z0 - z1 >= LIFT_REVERSE_MIN_M:
            latlon = latlon[::-1]
            z0, z1 = z1, z0
        pts = _dedupe_rounded(latlon)
        if len(pts) < 2:
            dropped_degenerate += 1
            lift_items.append(None)
            lift_raw_xy.append(None)
            continue
        lift_items.append({
            "osm_id": osm,
            "name": _str_or_none(L["name"][j]),
            "type": _str_or_none(L["lift_type"][j]),
            "geom": encode_line(pts),
            "bottom_alt": z0,
            "top_alt": z1,
            "_bbox": latlon,
        })
        lift_raw_xy.append(shapely.get_coordinates(source.lift_geoms[j]))
        idx = len(lift_items) - 1
        for a in _split(L["ski_area_ids"][j]):
            if a in sel_ids:
                members_lifts.setdefault(a, []).append(idx)

    # OSM ids that sit in more than one selected area (for generated-area keys).
    membership: dict[str, int] = {}
    for table, items in ((members_runs, run_items), (members_lifts, lift_items)):
        for idxs in table.values():
            for x in idxs:
                osm = items[x]["osm_id"]
                membership[osm] = membership.get(osm, 0) + 1
    shared = {osm for osm, count in membership.items() if count > 1}

    result = BuildResult()
    no_geometry = 0
    hull_kinds = {"polygon": 0, "polygon+runs": 0, "runs": 0}
    seen_keys: dict[str, str] = {}

    for i in selected:
        fid = str(A["feature_id"][i])
        r_idx = members_runs.get(fid, [])
        l_idx = members_lifts.get(fid, [])
        runs = [run_items[x] for x in r_idx]
        lifts = [lift_items[x] for x in l_idx]
        member_ids = [it["osm_id"] for it in runs + lifts]
        key = area_key(A["wikidata_id"][i], A["sources"][i], member_ids, shared)
        if key is None:
            no_geometry += 1
            continue
        if only is not None and key != only:
            continue
        polygon = source.area_polygons.get(fid)
        if polygon is None and not runs and not lifts:
            no_geometry += 1
            continue
        if not KEY_RE.match(key):
            raise SchemaError(f"area key {key!r} has unexpected characters")
        if key in seen_keys:
            # Two areas resolving to one key would overwrite each other's bundle. The data
            # has none today; if it ever does, keep the first and say so.
            log(f"::warning::duplicate area key {key} ({seen_keys[key]} and {fid}); keeping the first")
            continue
        seen_keys[key] = fid

        point = source.area_points[i]
        lon0, lat0 = (float(point.x), float(point.y)) if point is not None and not point.is_empty else (None, None)
        member_coords = [run_raw_xy[x] for x in r_idx] + [lift_raw_xy[x] for x in l_idx]
        member_coords = [c[:, :2] for c in member_coords if c is not None]
        if lon0 is None:
            ref = member_coords[0][0] if member_coords else np.asarray(shapely.get_coordinates(polygon)[0])
            lon0, lat0 = float(ref[0]), float(ref[1])
        hull_local, hull_kind = _hull_geometry(polygon, member_coords, lon0, lat0)
        if hull_local is None:
            no_geometry += 1
            continue
        hull_kinds[hull_kind] += 1
        hull_rings = _simplified_rings(hull_local, SIMPLIFY_M, lon0, lat0)
        index_rings = _simplified_rings(hull_local, INDEX_SIMPLIFY_M, lon0, lat0)
        hull_latlon = [np.asarray(r) for r in hull_rings]
        bbox = _bbox(hull_latlon + [it["_bbox"] for it in runs + lifts])

        runs_sorted = sorted(runs, key=lambda it: _osm_sort_key(it["osm_id"]))
        lifts_sorted = sorted(lifts, key=lambda it: _osm_sort_key(it["osm_id"]))
        websites = _json_list(A["websites"][i], "ski area websites")
        bundle = {
            "v": SCHEMA_VERSION,
            "key": key,
            "name": display_name(key, _str_or_none(A["name"][i])),
            "website": _str_or_none(websites[0]) if websites else None,
            "convention": _str_or_none(A["run_convention"][i]),
            "bbox": bbox,
            "hull": _encode_rings(hull_rings),
            "min_alt": _float_or_none(A["min_elevation"][i]),
            "max_alt": _float_or_none(A["max_elevation"][i]),
            "runs": [{k: v for k, v in it.items() if not k.startswith("_")} for it in runs_sorted],
            "lifts": [{k: v for k, v in it.items() if not k.startswith("_")} for it in lifts_sorted],
            "license": LICENSE,
            "attribution": ATTRIBUTION,
        }
        raw = dumps(bundle)
        gz = len(gzip_bytes(raw))
        h = content_hash(raw)
        result.areas[key] = AreaOut(
            key=key,
            name=bundle["name"],
            raw=raw,
            hash=h,
            gz_bytes=gz,
            index_entry={
                "key": key,
                "name": bundle["name"],
                "centroid": [round(lat0, 5), round(lon0, 5)],
                "bbox": bbox,
                "hull": _encode_rings(index_rings),
                "bytes": gz,
                "hash": h,
            },
            runs=len(runs_sorted),
            lifts=len(lifts_sorted),
            named_runs=sum(1 for r in runs_sorted if r["name"]),
            graded_runs=sum(1 for r in runs_sorted if r["difficulty"]),
            named_lifts=sum(1 for lf in lifts_sorted if lf["name"]),
        )

    if only is not None and only not in result.areas:
        raise SystemExit(f"--only {only}: no operating downhill area has that key")
    if only is None and len(result.areas) < min_areas:
        raise SchemaError(
            f"only {len(result.areas)} areas built (expected at least {min_areas}): "
            "refusing to publish what looks like a broken source"
        )
    result.stats = {
        "areas": len(result.areas),
        "runs": sum(a.runs for a in result.areas.values()),
        "lifts": sum(a.lifts for a in result.areas.values()),
        "hull_from_polygon": hull_kinds["polygon"],
        "hull_from_polygon_and_runs": hull_kinds["polygon+runs"],
        "hull_from_runs": hull_kinds["runs"],
        "skipped_areas_without_geometry": no_geometry,
        "dropped_degenerate_features": dropped_degenerate,
        "skipped_polygon_runs": source.skipped_polygon_runs,
        "seconds": round(time.monotonic() - t0, 1),
    }
    log(f"built {len(result.areas)} bundles in {result.stats['seconds']}s")
    return result


def build_index(result: BuildResult, osd_build: str) -> bytes:
    index = {
        "v": SCHEMA_VERSION,
        "osd_build": osd_build,
        "areas": [result.areas[k].index_entry for k in sorted(result.areas)],
        "license": LICENSE,
        "attribution": ATTRIBUTION,
    }
    return dumps(index)


# --- stores ---------------------------------------------------------------------------------

class Store:
    """Where objects go: a local directory or the R2 bucket."""

    name = "store"

    def read_json(self, key: str) -> Any | None:
        raise NotImplementedError

    def list_keys(self, prefix: str) -> set[str]:
        raise NotImplementedError

    def put_json(self, key: str, raw: bytes, sha: str) -> None:
        raise NotImplementedError


class LocalStore(Store):
    """Writes the uncompressed JSON at the object keys under ``root`` (handy to inspect or
    to serve with any static server)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.name = f"local:{self.root}"

    def read_json(self, key: str) -> Any | None:
        path = self.root / key
        if not path.exists():
            return None
        data = path.read_bytes()
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return json.loads(data)

    def list_keys(self, prefix: str) -> set[str]:
        base = self.root / prefix
        if not base.exists():
            return set()
        return {str(p.relative_to(self.root)).replace(os.sep, "/") for p in base.rglob("*") if p.is_file()}

    def put_json(self, key: str, raw: bytes, sha: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(path)


class R2Store(Store):
    """Cloudflare R2 through its S3 API. Objects are stored gzip-compressed with
    ``Content-Encoding: gzip`` so the public domain serves them compressed."""

    def __init__(self, client: Any, bucket: str):
        self.client = client
        self.bucket = bucket
        self.name = f"r2:{bucket}"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> R2Store:
        env = dict(os.environ if env is None else env)
        names = ["R2_ACCOUNT_ID", "R2_SKI_ACCESS_KEY_ID", "R2_SKI_SECRET_ACCESS_KEY", "R2_SKI_BUCKET"]
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise SystemExit(f"--upload needs these environment variables: {', '.join(missing)}")
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=env["R2_SKI_ACCESS_KEY_ID"],
            aws_secret_access_key=env["R2_SKI_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 8, "mode": "standard"},
                max_pool_connections=UPLOAD_WORKERS * 2,
                # Cloudflare's R2 guidance for boto3 >= 1.36: only send checksums when required.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return cls(client, env["R2_SKI_BUCKET"])

    def read_json(self, key: str) -> Any | None:
        from botocore.exceptions import ClientError

        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise
        data = obj["Body"].read()
        if data[:2] == b"\x1f\x8b":  # boto3 doesn't undo Content-Encoding
            data = gzip.decompress(data)
        return json.loads(data)

    def list_keys(self, prefix: str) -> set[str]:
        keys: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                keys.add(obj["Key"])
        return keys

    def put_json(self, key: str, raw: bytes, sha: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=gzip_bytes(raw),
            ContentType=CONTENT_TYPE,
            ContentEncoding=CONTENT_ENCODING,
            CacheControl=CACHE_CONTROL,
            Metadata={"sha256-16": sha},
        )


# --- build id and publishing ---------------------------------------------------------------

def osd_build_id(metadata: dict[str, Any]) -> str:
    """OpenSkiData's build identity: when its build finished (unique per daily build)."""
    run = metadata.get("run") if isinstance(metadata, dict) else None
    finished = run.get("finishedAt") if isinstance(run, dict) else None
    if isinstance(finished, str) and finished:
        return finished
    log("::warning::metadata.json has no run.finishedAt; using a hash of the metadata as the build id")
    return "sha-" + content_hash(dumps(metadata))


def is_unchanged(previous: dict[str, Any] | None, osd_build: str) -> bool:
    return (
        isinstance(previous, dict)
        and previous.get("osd_build") == osd_build
        and previous.get("builder") == BUILDER_VERSION
    )


def area_object_key(key: str) -> str:
    return f"{AREAS_PREFIX}{key}.json"


def publish(store: Store, result: BuildResult, metadata: dict[str, Any], osd_build: str,
            allow_shrink: bool = False, workers: int = UPLOAD_WORKERS) -> dict[str, Any]:
    """Write changed bundles, then the index, then build.json (last, so a run that dies
    halfway is retried in full next time)."""
    previous_index = store.read_json(INDEX_KEY)
    prev_hash: dict[str, str] = {}
    if isinstance(previous_index, dict):
        prev_hash = {a.get("key"): a.get("hash") for a in previous_index.get("areas", []) if isinstance(a, dict)}
    if prev_hash and not allow_shrink and len(result.areas) < MAX_SHRINK * len(prev_hash):
        raise PublishRefused(
            f"the new build has {len(result.areas)} areas, the published one {len(prev_hash)}: "
            f"more than {int((1 - MAX_SHRINK) * 100)}% fewer. Check OpenSkiData, then rerun with --allow-shrink."
        )
    existing = store.list_keys(AREAS_PREFIX)
    todo = [
        a for k, a in sorted(result.areas.items())
        if area_object_key(k) not in existing or prev_hash.get(k) != a.hash
    ]
    log(f"{store.name}: {len(todo)} of {len(result.areas)} bundles changed")

    def put(a: AreaOut) -> None:
        store.put_json(area_object_key(a.key), a.raw, a.hash)

    if workers > 1 and len(todo) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in concurrent.futures.as_completed([pool.submit(put, a) for a in todo]):
                fut.result()  # re-raise the first failure
    else:
        for a in todo:
            put(a)

    index_raw = build_index(result, osd_build)
    index_hash = content_hash(index_raw)
    store.put_json(INDEX_KEY, index_raw, index_hash)

    source = metadata.get("source", {}) if isinstance(metadata, dict) else {}
    osm = source.get("openStreetMap", {}) if isinstance(source, dict) else {}
    build_info = {
        "v": SCHEMA_VERSION,
        "osd_build": osd_build,
        "osd_format": metadata.get("formatVersion") if isinstance(metadata, dict) else None,
        "osm_data_at": osm.get("dataTimestamp") if isinstance(osm, dict) else None,
        "builder": BUILDER_VERSION,
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "areas": result.stats.get("areas"),
        "runs": result.stats.get("runs"),
        "lifts": result.stats.get("lifts"),
        "index_hash": index_hash,
        "index_bytes": len(gzip_bytes(index_raw)),
        "changed": len(todo),
        "license": LICENSE,
        "attribution": ATTRIBUTION,
    }
    build_raw = dumps(build_info)
    store.put_json(BUILD_KEY, build_raw, content_hash(build_raw))
    return {"changed": len(todo), "index_raw": len(index_raw), "index_gz": build_info["index_bytes"]}


# --- network ----------------------------------------------------------------------------------

def http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_metadata(url: str = METADATA_URL) -> dict[str, Any]:
    data = json.loads(http_get(url))
    if not isinstance(data, dict):
        raise SchemaError("metadata.json isn't a JSON object")
    return data


def download(url: str, dest: Path, expected_bytes: int | None = None) -> Path:
    """Stream ``url`` to ``dest`` (one download per run: OpenSkiData asks for at most one a day)."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
        length = resp.headers.get("Content-Length")
        shutil.copyfileobj(resp, fh, length=1 << 20)
    size = tmp.stat().st_size
    if length is not None and int(length) != size:
        raise RuntimeError(f"download of {url} was cut short: {size} of {length} bytes")
    if expected_bytes and expected_bytes != size:
        log(f"::warning::{url} is {size} bytes, metadata.json said {expected_bytes} (a newer build mid-download?)")
    tmp.replace(dest)
    log(f"downloaded {size / 1e6:.0f} MB in {time.monotonic() - t0:.0f}s")
    return dest


# --- reporting --------------------------------------------------------------------------------

def summary_lines(result: BuildResult, top: int = 10) -> list[str]:
    total_raw = sum(len(a.raw) for a in result.areas.values())
    total_gz = sum(a.gz_bytes for a in result.areas.values())
    lines = [
        f"areas: {result.stats.get('areas')}  runs: {result.stats.get('runs')}  lifts: {result.stats.get('lifts')}",
        f"bundles: {total_raw / 1e6:.1f} MB raw, {total_gz / 1e6:.1f} MB gzip",
        f"hulls: {result.stats.get('hull_from_polygon')} area polygons, "
        f"{result.stats.get('hull_from_polygon_and_runs')} polygons joined with runs + lifts "
        f"(polygon held < {int(POLYGON_MIN_COVERAGE * 100)}% of them), "
        f"{result.stats.get('hull_from_runs')} from runs + lifts",
        f"skipped: {result.stats.get('skipped_areas_without_geometry')} areas without runs, lifts or polygon; "
        f"{result.stats.get('dropped_degenerate_features')} degenerate features; "
        f"{result.stats.get('skipped_polygon_runs')} downhill run polygons in the source (not bundled: lines only)",
        "largest bundles (gzip):",
    ]
    for a in sorted(result.areas.values(), key=lambda a: -a.gz_bytes)[:top]:
        lines.append(f"  {a.gz_bytes / 1e3:7.1f} KB  {a.key}  {a.name or '(unnamed)'}  ({a.runs} runs, {a.lifts} lifts)")
    return lines


def write_step_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("## Ski bundles\n\n```\n" + "\n".join(lines) + "\n```\n")


# --- CLI ----------------------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, help="write the objects (uncompressed JSON) under this directory")
    p.add_argument("--upload", action="store_true",
                   help="upload to R2 (needs R2_ACCOUNT_ID, R2_SKI_ACCESS_KEY_ID, R2_SKI_SECRET_ACCESS_KEY, R2_SKI_BUCKET)")
    p.add_argument("--gpkg", type=Path, help="use this local openskidata.gpkg instead of downloading it")
    p.add_argument("--metadata", type=Path, help="use this local metadata.json instead of fetching it")
    p.add_argument("--only", metavar="KEY", help="build one area (e.g. wd-Q3821979) and print its stats; no index")
    p.add_argument("--force", action="store_true", help="build even if OpenSkiData's build is unchanged")
    p.add_argument("--allow-shrink", action="store_true",
                   help=f"publish even with >{int((1 - MAX_SHRINK) * 100)}%% fewer areas than before")
    args = p.parse_args(argv)
    if not args.out and not args.upload:
        p.error("nothing to do: pass --out DIR and/or --upload")
    if args.only and args.upload:
        p.error("--only is for local debugging; it can't be combined with --upload")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    metadata = json.loads(args.metadata.read_text()) if args.metadata else fetch_metadata()
    osd_build = osd_build_id(metadata)
    fmt = str(metadata.get("formatVersion") or "")
    if fmt and fmt.split(".")[0] != str(TESTED_FORMAT_MAJOR):
        log(f"::warning::OpenSkiData format {fmt}; this script was written for {TESTED_FORMAT_MAJOR}.x")
    log(f"OpenSkiData build {osd_build} (format {fmt or '?'})")

    stores: list[Store] = []
    if args.upload:
        stores.append(R2Store.from_env())
    if args.out:
        stores.append(LocalStore(args.out))

    if not args.only and not args.force:
        previous = stores[0].read_json(BUILD_KEY)
        if is_unchanged(previous, osd_build):
            log(f"unchanged: {stores[0].name} already has OpenSkiData build {osd_build} (builder {BUILDER_VERSION})")
            return 0

    workdir: Path | None = None
    try:
        if args.gpkg:
            gpkg = args.gpkg
        else:
            workdir = Path(tempfile.mkdtemp(prefix="skidata-"))
            expected = (metadata.get("files") or {}).get("geoPackage")
            gpkg = download(GPKG_URL, workdir / "openskidata.gpkg", expected if isinstance(expected, int) else None)
        source = load_source(gpkg)
        result = build(source, only=args.only)
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

    if args.only:
        a = result.areas[args.only]
        print(json.dumps({
            "key": a.key, "name": a.name, "raw_bytes": len(a.raw), "gzip_bytes": a.gz_bytes,
            "runs": a.runs, "named_runs": a.named_runs, "graded_runs": a.graded_runs,
            "lifts": a.lifts, "named_lifts": a.named_lifts,
        }, ensure_ascii=False, indent=2))
        if args.out:
            LocalStore(args.out).put_json(area_object_key(a.key), a.raw, a.hash)
        return 0

    lines = summary_lines(result)
    for store in stores:
        pub = publish(store, result, metadata, osd_build, allow_shrink=args.allow_shrink)
        lines.append(f"{store.name}: {pub['changed']} bundles written; index {pub['index_raw'] / 1e6:.2f} MB raw, "
                     f"{pub['index_gz'] / 1e6:.2f} MB gzip")
    for line in lines:
        log(line)
    write_step_summary(lines)
    return 0


def cli(argv: list[str] | None = None) -> int:
    """main() with failures turned into exit codes and GitHub Actions error annotations:
    2 = the source isn't shaped as expected, 3 = a publish sanity check refused."""
    try:
        return main(argv)
    except SchemaError as exc:
        log(f"::error::{exc}")
        return 2
    except PublishRefused as exc:
        log(f"::error::{exc}")
        return 3


if __name__ == "__main__":
    sys.exit(cli())
