"""Tests for skidata/build_bundles.py, run on a fixture GeoPackage the tests write themselves.

    pip install -r skidata/requirements.txt pytest
    pytest skidata/tests -q
"""
from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
from typing import Any

import pytest
import shapely
from shapely.geometry import Point, Polygon

import build_bundles as bb
from conftest import (
    A_ORIGIN, B_ORIGIN, EXPECTED_KEYS, H_ORIGIN, METADATA, default_fids, ll, write_fixture_gpkg,
)


# OpenSkiData's required credit, word for word (its licence lists every source).
FULL_CREDIT = ("Data from OpenSkiData / OpenSkiMap.org, © OpenStreetMap contributors (ODbL), "
               "Skimap.org, Who's On First, © Mapterhorn")


def build_fixture(gpkg: Path, only: str | None = None) -> bb.BuildResult:
    return bb.build(bb.load_source(gpkg), only=only)


def bundle(result: bb.BuildResult, key: str) -> dict[str, Any]:
    return json.loads(result.areas[key].raw)


def hull_shape(rings: list[str]) -> shapely.Geometry:
    """Even-odd union of the encoded rings, as a shapely shape in (lon, lat)."""
    shape = shapely.Polygon()
    for ring in rings:
        poly = Polygon([(lon, lat) for lat, lon in bb.decode_line(ring)])
        shape = shapely.symmetric_difference(shape, poly)
    return shape


def contains(shape: shapely.Geometry, lonlat: tuple[float, float]) -> bool:
    return bool(shape.contains(Point(*lonlat)))


# --- polyline: identical to the backend's route_stats.encode_line -----------------------

# Produced by homebound/backend/src/services/route_stats.py encode_line (2026-11-13).
# The first is Google's own documented example.
BACKEND_POLYLINES = {
    "google_reference": ([(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)], "_p~iF~ps|U_ulLnnqC_mqNvxq`@"),
    "park_city": ([(40.651234, -111.508765), (40.649871, -111.510432), (40.647002, -111.512999),
                   (40.645551, -111.514004)], "eubwFv`rgTnGlI|P`O`HfE"),
    "half_up_rounding": ([(0.000005, -0.000005), (0.000015, -0.000015), (-0.000025, 0.000025)], "A?A@FG"),
    "southern_east": ([(-45.0312, 168.6626), (-45.0329, 168.6501)], "~czqGg|ld_@rIbmA"),
    "single_point": ([(46.0, 7.0)], "_kwwG_evi@"),
    "empty": ([], ""),
}


@pytest.mark.parametrize("name", sorted(BACKEND_POLYLINES))
def test_polyline_matches_backend_encoder(name: str) -> None:
    points, expected = BACKEND_POLYLINES[name]
    assert bb.encode_line(points) == expected
    decoded = bb.decode_line(expected)
    assert len(decoded) == len(points)
    for (lat, lon), (dlat, dlon) in zip(points, decoded):
        assert dlat == pytest.approx(lat, abs=1e-5)
        assert dlon == pytest.approx(lon, abs=1e-5)


def test_bundle_geometry_decodes_and_is_simplified(gpkg: Path) -> None:
    b = bundle(build_fixture(gpkg), "wd-Q42")
    payday = next(r for r in b["runs"] if r["name"] == "Payday")
    pts = bb.decode_line(payday["geom"])
    # (0,1000) (0,750) (1,500) (0,250) (0,0) (300,-400): the four collinear-ish points go.
    assert len(pts) == 3
    lon, lat = ll(0, 1000, A_ORIGIN)
    assert pts[0] == pytest.approx((lat, lon), abs=1e-5)
    lon, lat = ll(300, -400, A_ORIGIN)
    assert pts[-1] == pytest.approx((lat, lon), abs=1e-5)


# --- bundle contents (the documented schema) --------------------------------------------

def test_bundle_schema_and_filters(gpkg: Path) -> None:
    result = build_fixture(gpkg)
    b = bundle(result, "wd-Q42")
    assert list(b) == ["v", "key", "name", "website", "convention", "bbox", "hull", "min_alt",
                       "max_alt", "runs", "lifts", "license", "attribution"]
    assert b["v"] == 1
    assert b["key"] == "wd-Q42"
    assert b["name"] == "Alpha Mountain"
    assert b["website"] == "https://alpha.example/"
    assert b["convention"] == "north_america"
    assert b["license"] == "ODbL-1.0"
    assert b["attribution"] == FULL_CREDIT
    assert (b["min_alt"], b["max_alt"]) == (2100.0, 2600.0)
    s, w, n, e = b["bbox"]
    assert s < n and w < e

    # Downhill runs only (no nordic, no connection), sorted by OSM id.
    assert [r["osm_id"] for r in b["runs"]] == ["w1001", "w1003"]
    payday, unnamed = b["runs"]
    assert list(payday) == ["osm_id", "name", "ref", "difficulty", "color", "oneway", "geom"]
    assert payday == {**payday, "name": "Payday", "ref": None, "difficulty": "intermediate",
                      "color": "blue", "oneway": False}
    assert unnamed == {**unnamed, "name": None, "difficulty": None, "color": "grey", "oneway": True}

    # Operating lifts only; drawn downhill in OSM, flipped to run bottom -> top.
    assert [lf["osm_id"] for lf in b["lifts"]] == ["w1501"]
    lift = b["lifts"][0]
    assert list(lift) == ["osm_id", "name", "type", "geom", "bottom_alt", "top_alt"]
    assert lift["name"] == "Payday Lift" and lift["type"] == "chair_lift"
    assert (lift["bottom_alt"], lift["top_alt"]) == (2100.0, 2600.0)
    pts = bb.decode_line(lift["geom"])
    lon, lat = ll(300, -300, A_ORIGIN)
    assert pts[0] == pytest.approx((lat, lon), abs=1e-5)

    # A lift already drawn bottom -> top keeps its direction.
    beta = bundle(result, "osm-r200")
    tbar = beta["lifts"][0]
    assert (tbar["name"], tbar["type"], tbar["bottom_alt"], tbar["top_alt"]) == (None, "t-bar", 1500.0, 1700.0)
    assert beta["runs"][0]["ref"] == "3" and beta["runs"][0]["color"] == "blue"  # europe: easy = blue
    assert beta["convention"] == "europe"

    # The abandoned, nordic-only and empty areas are not bundled; the polygon-only one is.
    assert set(result.areas) == EXPECTED_KEYS
    india = bundle(result, "osm-w500")
    assert india["runs"] == [] and india["lifts"] == [] and india["hull"]


def test_index_entries_describe_each_bundle(gpkg: Path) -> None:
    result = build_fixture(gpkg)
    index = json.loads(bb.build_index(result, "osd-build-1"))
    assert list(index) == ["v", "osd_build", "areas", "license", "attribution"]
    assert index["osd_build"] == "osd-build-1"
    assert [a["key"] for a in index["areas"]] == sorted(EXPECTED_KEYS)
    for entry in index["areas"]:
        assert list(entry) == ["key", "name", "centroid", "bbox", "hull", "bytes", "hash"]
        out = result.areas[entry["key"]]
        assert entry["hash"] == bb.content_hash(out.raw) and len(entry["hash"]) == 16
        assert entry["bytes"] == len(gzip.compress(out.raw, compresslevel=9, mtime=0))
        assert entry["bbox"] == json.loads(out.raw)["bbox"]
        assert len(entry["hull"]) >= 1


def test_every_file_carries_the_full_credit(gpkg: Path, tmp_path: Path) -> None:
    assert bb.ATTRIBUTION == FULL_CREDIT
    result = build_fixture(gpkg)
    assert {bundle(result, k)["attribution"] for k in result.areas} == {FULL_CREDIT}
    assert json.loads(bb.build_index(result, "osd-build-1"))["attribution"] == FULL_CREDIT
    readme = (Path(bb.__file__).parent / "README.md").read_text()
    page = (Path(bb.__file__).parent / "index.html").read_text()
    assert FULL_CREDIT in readme
    assert FULL_CREDIT.replace("'", "&rsquo;").replace("©", "&copy;") in page or FULL_CREDIT in page
    # The old short line is gone from what we publish.
    assert "© OpenStreetMap contributors, OpenSkiData\"" not in readme


def test_display_names_override_bundle_and_index(gpkg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert bb.DISPLAY_NAMES["wd-Q3821979"] == "Park City Mountain"
    assert bb.display_name("wd-Q3821979", "Park City Mountain Resort") == "Park City Mountain"
    assert bb.display_name("osm-r200", "Beta Hill") == "Beta Hill"
    assert bb.display_name("gen-w1", None) is None
    monkeypatch.setitem(bb.DISPLAY_NAMES, "wd-Q42", "Alpha")
    result = build_fixture(gpkg)
    assert bundle(result, "wd-Q42")["name"] == "Alpha"
    assert result.areas["wd-Q42"].name == "Alpha"
    entry = next(a for a in json.loads(bb.build_index(result, "b"))["areas"] if a["key"] == "wd-Q42")
    assert entry["name"] == "Alpha"
    assert bundle(result, "osm-r200")["name"] == "Beta Hill"   # no entry: the source name
    # Every override is documented on the published page and in the README.
    readme = (Path(bb.__file__).parent / "README.md").read_text()
    page = (Path(bb.__file__).parent / "index.html").read_text()
    for key, name in bb.DISPLAY_NAMES.items():
        if key == "wd-Q42":
            continue
        assert key in readme and name in readme and key in page and name in page


@pytest.mark.parametrize(("convention", "difficulty", "color"), [
    ("north_america", "novice", "green"), ("north_america", "easy", "green"),
    ("north_america", "intermediate", "blue"), ("north_america", "advanced", "black"),
    ("north_america", "expert", "black"), ("north_america", "freeride", "orange"),
    ("europe", "novice", "green"), ("europe", "easy", "blue"), ("europe", "intermediate", "red"),
    ("europe", "expert", "black"), ("europe", "extreme", "orange"),
    ("japan", "easy", "green"), ("japan", "intermediate", "red"), ("japan", "advanced", "black"),
    ("europe", None, "grey"), ("north_america", "something_new", "grey"),
])
def test_run_color_matches_openskidata(convention: str, difficulty: str | None, color: str) -> None:
    assert bb.run_color(convention, difficulty) == color


# --- hull ------------------------------------------------------------------------------------

def test_hull_is_area_polygon_when_it_covers_the_runs(gpkg: Path) -> None:
    b = bundle(build_fixture(gpkg), "wd-Q42")
    hull = hull_shape(b["hull"])
    # The polygon is a 3 km square: a point 1.4 km from any run is inside it ...
    assert contains(hull, ll(-1400, -1400, A_ORIGIN))
    # ... and a point just outside the square is not, even though it's near a run end.
    assert not contains(hull, ll(0, 1600, A_ORIGIN))


def test_hull_falls_back_to_buffered_convex_hull(gpkg: Path) -> None:
    b = bundle(build_fixture(gpkg), "osm-r200")
    hull = hull_shape(b["hull"])
    assert len(b["hull"]) == 1
    # Run along x=0 and T-bar along x=100, both from -500 to +500 m north.
    for e, n in [(0, 0), (0, 500), (100, -500), (50, 0)]:
        assert contains(hull, ll(e, n, B_ORIGIN))
    # Buffered 300 m: 250 m past the run end is in, 400 m is out.
    assert contains(hull, ll(0, 750, B_ORIGIN))
    assert contains(hull, ll(-250, 0, B_ORIGIN))
    assert not contains(hull, ll(0, 900, B_ORIGIN))
    assert not contains(hull, ll(-400, 0, B_ORIGIN))


def test_hull_joins_a_polygon_that_misses_its_runs(gpkg: Path) -> None:
    b = bundle(build_fixture(gpkg), "osm-w400")
    hull = hull_shape(b["hull"])
    # The 200 m base-area polygon is still in, and so is the run 2 km north of it.
    assert contains(hull, ll(-90, -90, H_ORIGIN))
    assert contains(hull, ll(0, 1900, H_ORIGIN))
    assert contains(hull, ll(0, 2250, H_ORIGIN))


def test_index_hull_is_coarser_but_still_holds_the_area(gpkg: Path) -> None:
    result = build_fixture(gpkg)
    index = json.loads(bb.build_index(result, "x"))
    entry = next(a for a in index["areas"] if a["key"] == "osm-r200")
    hull = hull_shape(entry["hull"])
    assert contains(hull, ll(0, 0, B_ORIGIN))
    assert sum(len(bb.decode_line(r)) for r in entry["hull"]) <= sum(
        len(bb.decode_line(r)) for r in bundle(result, "osm-r200")["hull"])


# --- stable keys ----------------------------------------------------------------------------

def test_keys_are_stable_across_rebuilds(tmp_path: Path) -> None:
    first = build_fixture(write_fixture_gpkg(tmp_path / "one.gpkg", default_fids("build1")))
    second = build_fixture(write_fixture_gpkg(tmp_path / "two.gpkg", default_fids("build2")))
    assert set(first.areas) == set(second.areas) == EXPECTED_KEYS
    # OpenSkiData's feature ids changed; nothing we publish did.
    for key in EXPECTED_KEYS:
        assert first.areas[key].raw == second.areas[key].raw
    fids = set(default_fids("build1").values()) | set(default_fids("build2").values())
    for out in first.areas.values():
        text = out.raw.decode()
        assert not any(f in text for f in fids)


def test_area_key_rules() -> None:
    osm_src = json.dumps([{"id": "way/918681867", "type": "openstreetmap"}, {"id": 229, "type": "skimap.org"}])
    assert bb.area_key("Q3821979", osm_src) == "wd-Q3821979"
    assert bb.area_key(None, osm_src) == "osm-w918681867"
    assert bb.area_key("not-a-qid", osm_src) == "osm-w918681867"
    rel = json.dumps([{"id": "way/5", "type": "openstreetmap"}, {"id": "relation/9", "type": "openstreetmap"}])
    assert bb.area_key(None, rel) == "osm-r9"  # a relation outranks a way
    assert bb.area_key(None, json.dumps([{"id": 229, "type": "skimap.org"}])) == "sm-229"
    assert bb.area_key(None, "[]", ["w30", "w4", "n1"]) == "gen-w4"
    assert bb.area_key(None, "[]", ["w30", "w4"], {"w4"}) == "gen-w30"
    assert bb.area_key(None, "[]", ["w4"], {"w4"}) == "gen-w4"
    assert bb.area_key(None, "[]", []) is None


def test_generated_areas_sharing_a_run_get_distinct_keys(gpkg: Path) -> None:
    result = build_fixture(gpkg)
    assert {"gen-w300", "gen-w310"} <= set(result.areas)
    assert "gen-w250" not in result.areas
    # The shared run is in both bundles.
    for key in ("gen-w300", "gen-w310"):
        assert "w250" in [r["osm_id"] for r in bundle(result, key)["runs"]]


# --- failing loudly ---------------------------------------------------------------------------

def test_missing_column_fails_loudly(tmp_path: Path, metadata_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = write_fixture_gpkg(tmp_path / "bad.gpkg", drop_columns={"runs_linestring": ["difficulty"]})
    with pytest.raises(bb.SchemaError, match=r"runs_linestring.*difficulty"):
        bb.load_source(bad)
    out = tmp_path / "out"
    code = bb.cli(["--gpkg", str(bad), "--metadata", str(metadata_file), "--out", str(out)])
    assert code == 2
    assert "::error::" in capsys.readouterr().err
    assert not (out / bb.INDEX_KEY).exists() and not (out / bb.BUILD_KEY).exists()


def test_missing_layer_fails_loudly(tmp_path: Path) -> None:
    bad = write_fixture_gpkg(tmp_path / "bad.gpkg", drop_layers=("lifts_linestring",))
    with pytest.raises(bb.SchemaError, match="lifts_linestring"):
        bb.check_schema(bad)


def test_unexpected_source_ids_fail_loudly() -> None:
    with pytest.raises(bb.SchemaError):
        bb.osm_ref('[{"id": "way:12", "type": "openstreetmap"}]')
    with pytest.raises(bb.SchemaError):
        bb.osm_ref("not json")


def test_too_few_areas_is_refused(gpkg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bb, "MIN_AREAS", 1000)
    with pytest.raises(bb.SchemaError, match="refusing to publish"):
        build_fixture(gpkg)


# --- skip when unchanged --------------------------------------------------------------------------

def test_skip_when_unchanged_locally(gpkg: Path, metadata_file: Path, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out"
    args = ["--gpkg", str(gpkg), "--metadata", str(metadata_file), "--out", str(out)]
    assert bb.cli(args) == 0
    build_info = json.loads((out / bb.BUILD_KEY).read_text())
    assert build_info["osd_build"] == METADATA["run"]["finishedAt"]
    assert build_info["builder"] == bb.BUILDER_VERSION
    assert build_info["areas"] == len(EXPECTED_KEYS)
    assert len(list((out / bb.AREAS_PREFIX).glob("*.json"))) == len(EXPECTED_KEYS)

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("an unchanged build must not read the GeoPackage")

    monkeypatch.setattr(bb, "load_source", boom)
    assert bb.cli(args) == 0  # same OpenSkiData build: nothing read, nothing written

    # A new OpenSkiData build, or a new builder version, rebuilds.
    monkeypatch.undo()
    monkeypatch.setattr(bb, "MIN_AREAS", 1)
    newer = dict(METADATA, run={"finishedAt": "2026-11-20T01:00:00.000Z"})
    metadata_file.write_text(json.dumps(newer))
    assert bb.cli(args) == 0
    assert json.loads((out / bb.BUILD_KEY).read_text())["osd_build"] == "2026-11-20T01:00:00.000Z"
    assert bb.is_unchanged({"osd_build": "b", "builder": bb.BUILDER_VERSION}, "b")
    assert not bb.is_unchanged({"osd_build": "b", "builder": bb.BUILDER_VERSION - 1}, "b")
    assert not bb.is_unchanged(None, "b")


def test_skip_when_unchanged_in_r2_never_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3()
    fake.objects[bb.BUILD_KEY] = {"Body": gzip.compress(json.dumps(
        {"osd_build": METADATA["run"]["finishedAt"], "builder": bb.BUILDER_VERSION}).encode())}
    install_fake_boto3(monkeypatch, fake)
    monkeypatch.setattr(bb, "fetch_metadata", lambda url=bb.METADATA_URL: METADATA)

    def no_download(*_a: Any, **_k: Any) -> None:
        raise AssertionError("must not download when the build is unchanged")

    monkeypatch.setattr(bb, "download", no_download)
    assert bb.cli(["--upload"]) == 0
    assert fake.puts == []


def test_build_id_comes_from_metadata() -> None:
    assert bb.osd_build_id(METADATA) == "2026-11-13T01:41:53.171Z"
    fallback = bb.osd_build_id({"formatVersion": "16.0.0"})
    assert fallback.startswith("sha-") and fallback == bb.osd_build_id({"formatVersion": "16.0.0"})


# --- upload: gzip, headers, only what changed ---------------------------------------------------------

class FakeS3:
    """Just enough of boto3's S3 client for R2Store."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kw: Any) -> dict[str, Any]:
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 (boto3 names)
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "nope"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}

    def get_paginator(self, name: str) -> Any:
        assert name == "list_objects_v2"
        fake = self

        class Paginator:
            def paginate(self, Bucket: str, Prefix: str) -> list[dict[str, Any]]:  # noqa: N803
                keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                return [{"Contents": [{"Key": k} for k in keys[:2]]}, {"Contents": [{"Key": k} for k in keys[2:]]}]

        return Paginator()


def install_fake_boto3(monkeypatch: pytest.MonkeyPatch, fake: FakeS3) -> dict[str, Any]:
    import boto3

    seen: dict[str, Any] = {}

    def client(service: str, **kw: Any) -> FakeS3:
        seen.update(kw, service=service)
        return fake

    monkeypatch.setattr(boto3, "client", client)
    for name, value in {"R2_ACCOUNT_ID": "acct123", "R2_SKI_ACCESS_KEY_ID": "AKID",
                        "R2_SKI_SECRET_ACCESS_KEY": "SECRET", "R2_SKI_BUCKET": "homebound-ski"}.items():
        monkeypatch.setenv(name, value)
    return seen


def test_upload_is_gzip_with_headers_and_skips_unchanged(gpkg: Path, metadata_file: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3()
    seen = install_fake_boto3(monkeypatch, fake)
    assert bb.cli(["--gpkg", str(gpkg), "--metadata", str(metadata_file), "--upload"]) == 0

    assert seen["service"] == "s3"
    assert seen["endpoint_url"] == "https://acct123.r2.cloudflarestorage.com"
    assert seen["aws_access_key_id"] == "AKID" and seen["aws_secret_access_key"] == "SECRET"
    assert seen["region_name"] == "auto"

    keys = [p["Key"] for p in fake.puts]
    assert sorted(keys[:-2]) == sorted(f"v1/areas/{k}.json" for k in EXPECTED_KEYS)
    assert keys[-2:] == ["v1/index.json", "v1/build.json"]  # build.json strictly last
    for put in fake.puts:
        assert put["Bucket"] == "homebound-ski"
        assert put["ContentType"] == "application/json"
        assert put["ContentEncoding"] == "gzip"
        assert put["CacheControl"] == "public, max-age=3600"
        assert put["Body"][:2] == b"\x1f\x8b"
        json.loads(gzip.decompress(put["Body"]))  # valid JSON inside
    alpha = json.loads(gzip.decompress(fake.objects["v1/areas/wd-Q42.json"]["Body"]))
    assert alpha["name"] == "Alpha Mountain"

    # Same data, new OpenSkiData build id: bundles are unchanged, so only the index and
    # build.json go up again.
    fake.puts.clear()
    metadata_file.write_text(json.dumps(dict(METADATA, run={"finishedAt": "2026-11-20T01:00:00.000Z"})))
    assert bb.cli(["--gpkg", str(gpkg), "--metadata", str(metadata_file), "--upload"]) == 0
    assert [p["Key"] for p in fake.puts] == ["v1/index.json", "v1/build.json"]


def test_upload_rewrites_changed_and_missing_bundles(gpkg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3()
    result = build_fixture(gpkg)
    store = bb.R2Store(fake, "b")
    bb.publish(store, result, METADATA, "build-1", workers=1)
    fake.puts.clear()

    # Someone deleted one object, and one area's content changed.
    del fake.objects["v1/areas/sm-77.json"]
    old = result.areas["osm-r200"]
    changed = old.raw.replace(b"Beta Hill", b"Beta Hill East")
    result.areas["osm-r200"] = bb.AreaOut(**{**old.__dict__, "raw": changed, "hash": bb.content_hash(changed)})
    result.areas["osm-r200"].index_entry["hash"] = bb.content_hash(changed)
    bb.publish(store, result, METADATA, "build-2", workers=4)
    assert sorted(p["Key"] for p in fake.puts[:-2]) == ["v1/areas/osm-r200.json", "v1/areas/sm-77.json"]


def test_shrinking_index_is_refused(gpkg: Path) -> None:
    fake = FakeS3()
    store = bb.R2Store(fake, "b")
    previous = {"areas": [{"key": f"k{i}", "hash": "x"} for i in range(20)]}
    fake.objects[bb.INDEX_KEY] = {"Body": gzip.compress(json.dumps(previous).encode())}
    result = build_fixture(gpkg)
    with pytest.raises(bb.PublishRefused):
        bb.publish(store, result, METADATA, "b1")
    assert fake.puts == []
    bb.publish(store, result, METADATA, "b1", allow_shrink=True)
    assert fake.puts[-1]["Key"] == bb.BUILD_KEY


def test_upload_needs_the_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("R2_ACCOUNT_ID", "R2_SKI_ACCESS_KEY_ID", "R2_SKI_SECRET_ACCESS_KEY", "R2_SKI_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="R2_ACCOUNT_ID"):
        bb.R2Store.from_env()


# --- CLI ------------------------------------------------------------------------------------------------

def test_only_builds_one_area(gpkg: Path, metadata_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    assert bb.cli(["--gpkg", str(gpkg), "--metadata", str(metadata_file), "--only", "wd-Q42", "--out", str(out)]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["key"] == "wd-Q42"
    assert (stats["runs"], stats["named_runs"], stats["graded_runs"], stats["lifts"], stats["named_lifts"]) == (2, 1, 1, 1, 1)
    assert [p.name for p in (out / bb.AREAS_PREFIX).iterdir()] == ["wd-Q42.json"]
    assert not (out / bb.INDEX_KEY).exists()


def test_only_cannot_upload() -> None:
    with pytest.raises(SystemExit):
        bb.parse_args(["--only", "wd-Q42", "--upload"])
    with pytest.raises(SystemExit):
        bb.parse_args([])
