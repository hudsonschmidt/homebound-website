"""Shared fixtures: a tiny OpenSkiData-shaped GeoPackage written by the tests themselves.

The layer and column names copy OpenSkiData's format 16 (checked against the real
openskidata.gpkg of 2026-11-13). Coordinates sit near Park City; distances are built in
metres so the hull tests can reason about the 300 m buffer.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shapely
from pyogrio.raw import write as ogr_write

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_bundles as bb  # noqa: E402

LAT0, LON0 = 40.60, -111.50
M_PER_DEG_LAT = 111195.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(LAT0))


def ll(east_m: float, north_m: float, origin: tuple[float, float] = (LAT0, LON0)) -> tuple[float, float]:
    """(lon, lat) of a point ``east_m`` / ``north_m`` metres from ``origin`` (lat, lon)."""
    return (origin[1] + east_m / M_PER_DEG_LON, origin[0] + north_m / M_PER_DEG_LAT)


def line(*pts_m: tuple[float, float], origin: tuple[float, float] = (LAT0, LON0), z: list[float] | None = None) -> bytes:
    coords = [ll(e, n, origin) for e, n in pts_m]
    if z is not None:
        coords = [(x, y, zz) for (x, y), zz in zip(coords, z)]
    else:
        coords = [(x, y, 2000.0) for x, y in coords]
    return shapely.to_wkb(shapely.LineString(coords))


def box(e0: float, n0: float, e1: float, n1: float, origin: tuple[float, float] = (LAT0, LON0)) -> bytes:
    ring = [ll(e0, n0, origin), ll(e1, n0, origin), ll(e1, n1, origin), ll(e0, n1, origin), ll(e0, n0, origin)]
    return shapely.to_wkb(shapely.MultiPolygon([shapely.Polygon(ring)]))


def point(e: float, n: float, origin: tuple[float, float] = (LAT0, LON0)) -> bytes:
    return shapely.to_wkb(shapely.Point(*ll(e, n, origin)))


def osm(*ids: str) -> str:
    return json.dumps([{"id": i, "type": "openstreetmap"} for i in ids])


# Origins of the fixture areas (lat, lon), far enough apart not to overlap.
A_ORIGIN = (40.60, -111.50)   # wd-Q42: polygon covering its runs
B_ORIGIN = (40.70, -111.50)   # osm-r200: no polygon -> buffered convex hull
C_ORIGIN = (40.80, -111.50)   # two generated areas sharing a run
H_ORIGIN = (40.90, -111.50)   # osm-w400: polygon that misses its runs -> polygon + runs
D_ORIGIN = (41.00, -111.50)   # sm-77: Skimap.org only
X_ORIGIN = (41.10, -111.50)   # excluded areas


def area_rows(fid: dict[str, str]) -> list[dict[str, Any]]:
    """Ski areas (point layer). ``fid`` maps a logical name to this build's feature id."""
    base = {"websites": "[]", "wikidata_id": None, "min_elevation": 2100.0, "max_elevation": 2600.0,
            "run_convention": "north_america", "activities": "downhill", "status": "operating"}
    return [
        {**base, "feature_id": fid["A"], "name": "Alpha Mountain", "sources": osm("way/100"),
         "wikidata_id": "Q42", "websites": '["https://alpha.example/"]', "geom": point(0, 0, A_ORIGIN)},
        {**base, "feature_id": fid["B"], "name": "Beta Hill", "sources": osm("relation/200"),
         "run_convention": "europe", "geom": point(0, 0, B_ORIGIN)},
        {**base, "feature_id": fid["C1"], "name": None, "sources": "[]", "geom": point(0, 0, C_ORIGIN)},
        {**base, "feature_id": fid["C2"], "name": None, "sources": "[]", "geom": point(2000, 0, C_ORIGIN)},
        {**base, "feature_id": fid["H"], "name": "Hotel Ridge", "sources": osm("way/400"), "geom": point(0, 0, H_ORIGIN)},
        {**base, "feature_id": fid["D"], "name": "Delta Bowl", "sources": json.dumps([{"id": 77, "type": "skimap.org"}]),
         "run_convention": "japan", "activities": "downhill,nordic", "geom": point(0, 0, D_ORIGIN)},
        {**base, "feature_id": fid["I"], "name": "India Basin", "sources": osm("way/500"), "geom": point(0, 0, X_ORIGIN)},
        # Excluded: abandoned, nordic-only, and an operating area with nothing in it.
        {**base, "feature_id": fid["E"], "name": "Echo Abandoned", "sources": osm("way/600"), "status": "abandoned",
         "geom": point(5000, 0, X_ORIGIN)},
        {**base, "feature_id": fid["F"], "name": "Foxtrot Nordic", "sources": osm("way/700"), "activities": "nordic",
         "geom": point(10000, 0, X_ORIGIN)},
        {**base, "feature_id": fid["G"], "name": "Golf Empty", "sources": osm("way/800"), "geom": point(15000, 0, X_ORIGIN)},
    ]


def polygon_rows(fid: dict[str, str]) -> list[dict[str, Any]]:
    return [
        # Alpha: 3 km square around everything it has.
        {"feature_id": fid["A"], "geom": box(-1500, -1500, 1500, 1500, A_ORIGIN)},
        # Hotel: a 200 m base-area square; its runs go 2 km north.
        {"feature_id": fid["H"], "geom": box(-100, -100, 100, 100, H_ORIGIN)},
        # India: a polygon with no runs or lifts (still bundled: resort detection).
        {"feature_id": fid["I"], "geom": box(-500, -500, 500, 500, X_ORIGIN)},
    ]


def run_rows(fid: dict[str, str]) -> list[dict[str, Any]]:
    base = {"status": "operating", "uses": "downhill", "ref": None, "difficulty": None,
            "difficulty_convention": "north_america", "oneway": False}
    return [
        # Alpha: a named intermediate run with collinear points the 5 m simplification drops,
        # built from two merged OSM ways (the lower id is kept).
        {**base, "name": "Payday", "difficulty": "intermediate", "sources": osm("way/1002", "way/1001"),
         "ski_area_ids": fid["A"],
         "geom": line((0, 1000), (0, 750), (1, 500), (0, 250), (0, 0), (300, -400), origin=A_ORIGIN)},
        {**base, "name": None, "sources": osm("way/1003"), "ski_area_ids": fid["A"], "oneway": True,
         "geom": line((-500, 800), (-600, 0), origin=A_ORIGIN)},
        # Alpha: nordic and connection runs are left out.
        {**base, "name": "Classic Loop", "uses": "nordic", "sources": osm("way/1004"), "ski_area_ids": fid["A"],
         "geom": line((-1000, 0), (-1000, 500), origin=A_ORIGIN)},
        {**base, "name": "Skier Bridge", "uses": "connection", "sources": osm("way/1005"), "ski_area_ids": fid["A"],
         "geom": line((800, 0), (900, 0), origin=A_ORIGIN)},
        # Beta (europe): a run 1 km long.
        {**base, "name": "Piste 3", "ref": "3", "difficulty": "easy", "difficulty_convention": "europe",
         "sources": osm("way/2001"), "ski_area_ids": fid["B"], "geom": line((0, 500), (0, -500), origin=B_ORIGIN)},
        # Generated areas: way/250 is in both; each also has a run of its own.
        {**base, "name": "Shared Trail", "difficulty": "easy", "sources": osm("way/250"),
         "ski_area_ids": f"{fid['C1']},{fid['C2']}", "geom": line((0, 200), (2000, 200), origin=C_ORIGIN)},
        {**base, "name": "C One", "difficulty": "advanced", "sources": osm("way/300"), "ski_area_ids": fid["C1"],
         "geom": line((0, 0), (0, -500), origin=C_ORIGIN)},
        {**base, "name": "C Two", "difficulty": "expert", "sources": osm("way/310"), "ski_area_ids": fid["C2"],
         "geom": line((2000, 0), (2000, -500), origin=C_ORIGIN)},
        # Hotel: a run 2 km north of its tiny polygon.
        {**base, "name": "Long Way", "difficulty": "advanced", "sources": osm("way/4001"), "ski_area_ids": fid["H"],
         "geom": line((0, 2000), (0, 200), origin=H_ORIGIN)},
        # Delta (japan).
        {**base, "name": "Kita", "difficulty": "intermediate", "difficulty_convention": "japan",
         "sources": osm("way/7001"), "ski_area_ids": fid["D"], "geom": line((0, 300), (0, -300), origin=D_ORIGIN)},
        # Abandoned area's run, and a run in no area at all.
        {**base, "name": "Ghost", "sources": osm("way/6001"), "ski_area_ids": fid["E"],
         "geom": line((5000, 0), (5000, 300), origin=X_ORIGIN)},
        {**base, "name": "Orphan", "sources": osm("way/6002"), "ski_area_ids": "",
         "geom": line((20000, 0), (20000, 300), origin=X_ORIGIN)},
    ]


def lift_rows(fid: dict[str, str]) -> list[dict[str, Any]]:
    base = {"status": "operating", "lift_type": "chair_lift"}
    return [
        # Alpha: drawn top -> bottom in OSM; the bundle flips it to bottom -> top.
        {**base, "name": "Payday Lift", "sources": osm("way/1501"), "ski_area_ids": fid["A"],
         "geom": line((300, 1000), (300, -300), origin=A_ORIGIN, z=[2600.0, 2100.0])},
        {**base, "name": "Future Chair", "status": "proposed", "sources": osm("way/1502"), "ski_area_ids": fid["A"],
         "geom": line((600, 1000), (600, -300), origin=A_ORIGIN)},
        # Beta: a T-bar beside its run.
        {**base, "name": None, "lift_type": "t-bar", "sources": osm("way/2501"), "ski_area_ids": fid["B"],
         "geom": line((100, -500), (100, 500), origin=B_ORIGIN, z=[1500.0, 1700.0])},
    ]


def _write_layer(path: Path, layer: str, rows: list[dict[str, Any]], geometry_type: str,
                 columns: list[str] | None = None) -> None:
    columns = columns or [k for k in rows[0] if k != "geom"]
    data = []
    for c in columns:
        values = [r.get(c) for r in rows]
        if all(isinstance(v, bool) for v in values):
            data.append(np.array(values, dtype=bool))
        elif all(isinstance(v, (int, float)) or v is None for v in values) and any(isinstance(v, float) for v in values):
            data.append(np.array([np.nan if v is None else v for v in values], dtype=float))
        else:
            data.append(np.array(values, dtype=object))
    geoms = np.array([r["geom"] for r in rows], dtype=object)
    ogr_write(str(path), geoms, data, columns, layer=layer, driver="GPKG",
              geometry_type=geometry_type, crs="EPSG:4326")


def write_fixture_gpkg(path: Path, fid: dict[str, str] | None = None,
                       drop_columns: dict[str, list[str]] | None = None,
                       drop_layers: tuple[str, ...] = ()) -> Path:
    """Write the fixture GeoPackage. ``fid`` sets the (hash-like) feature ids, which
    OpenSkiData changes every build; ``drop_columns`` / ``drop_layers`` break the schema."""
    fid = fid or default_fids()
    drop_columns = drop_columns or {}
    layers = [
        ("ski_areas_point", area_rows(fid), "Point"),
        ("ski_areas_multipolygon", polygon_rows(fid), "MultiPolygon"),
        ("runs_linestring", run_rows(fid), "LineString Z"),
        ("lifts_linestring", lift_rows(fid), "LineString Z"),
    ]
    for name, rows, gtype in layers:
        if name in drop_layers:
            continue
        cols = [k for k in rows[0] if k != "geom" and k not in drop_columns.get(name, [])]
        _write_layer(path, name, rows, gtype, cols)
    return path


def default_fids(salt: str = "build1") -> dict[str, str]:
    import hashlib

    names = ["A", "B", "C1", "C2", "H", "D", "I", "E", "F", "G"]
    return {n: hashlib.sha1(f"{salt}:{n}".encode()).hexdigest() for n in names}


METADATA = {
    "version": 1,
    "formatVersion": "16.0.0",
    "source": {"openStreetMap": {"dataTimestamp": "2026-11-12T01:00:00.000Z"}},
    "run": {"startedAt": "2026-11-12T23:37:31.888Z", "finishedAt": "2026-11-13T01:41:53.171Z"},
    "files": {"geoPackage": 1},
}

EXPECTED_KEYS = {"wd-Q42", "osm-r200", "gen-w300", "gen-w310", "osm-w400", "sm-77", "osm-w500"}


@pytest.fixture(autouse=True)
def _small_min_areas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bb, "MIN_AREAS", 1)


@pytest.fixture
def gpkg(tmp_path: Path) -> Path:
    return write_fixture_gpkg(tmp_path / "fixture.gpkg")


@pytest.fixture
def metadata_file(tmp_path: Path) -> Path:
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps(METADATA))
    return p
