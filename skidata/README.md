# Homebound ski trail bundles

Homebound's ski mode names the runs and lifts you ride, and draws the resort's trail map
under your route. It does this with small per-resort files ("bundles") built from
[OpenSkiData](https://openskidata.org), the processed OpenStreetMap ski data behind
[OpenSkiMap.org](https://openskimap.org). This folder is the complete method that builds
them. It is published at <https://www.homeboundapp.com/skidata/>.

- **Bundles:** `https://ski.homeboundapp.com/v1/…` (see [Files](#files))
- **Script:** [`build_bundles.py`](build_bundles.py), run weekly by
  [`.github/workflows/ski-bundles.yml`](../.github/workflows/ski-bundles.yml)
- **Tests:** [`tests/`](tests/)

## Licence and attribution

The bundles are a **Derivative Database** of OpenSkiData, which is derived from
OpenStreetMap. They are made available under the
[Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/) (ODbL-1.0).
Every file says so in its `license` and `attribution` fields:

    © OpenStreetMap contributors, OpenSkiData

This README and `build_bundles.py` are the method used to make the derivative database
(ODbL §4.6). The bundles contain no Homebound user data.

Sources and credits:

- Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) (ODbL)
  and [OpenSkiMap.org](https://openskimap.org) / OpenSkiData.
- Ski area records from [Skimap.org](https://skimap.org), via OpenSkiData (the `sm-` areas,
  and the names and websites of many others).
- Elevations © [Mapterhorn](https://mapterhorn.com/attribution), via OpenSkiData (lift
  `bottom_alt` / `top_alt`, area `min_alt` / `max_alt`).

To fix a run or lift, edit OpenStreetMap: the change reaches OpenSkiData within a few days
and the bundles the following Monday.

## Method

Every Monday at 09:00 UTC (and when run by hand):

1. **Check.** Fetch <https://tiles.openskimap.org/metadata.json>. The OpenSkiData build id
   is its `run.finishedAt`. If it equals `osd_build` in the published `v1/build.json`
   **and** `builder` there equals the script's `BUILDER_VERSION`, stop: nothing changed.
2. **Download** <https://tiles.openskimap.org/openskidata.gpkg> (about 412 MB) once.
   OpenSkiData allows automated downloads at most once a day; this runs once a week.
3. **Check the format.** OpenSkiData says its format is not stable, so the script checks
   every layer, column and geometry type it reads and stops with an error if any is
   missing. It also refuses to publish fewer than 1,000 areas, or an index more than 20%
   smaller than the published one.
4. **Select:**
   - **Ski areas** whose `status` is `operating` and whose `activities` include
     `downhill`. Areas with no runs, no lifts and no polygon are skipped.
   - **Runs** from `runs_linestring` whose `uses` include `downhill` (so `downhill,nordic`
     counts), in a selected area. Nordic, ski touring, sled, connection and snow-park-only
     runs are left out, and so are runs mapped only as areas (`runs_multipolygon`).
   - **Lifts** whose `status` is `operating` (not proposed, planned, under construction,
     disused or abandoned), in a selected area.
   - A run or lift in several areas goes into each of their bundles.
5. **Simplify.** Each run and lift is simplified with Ramer–Douglas–Peucker at **5 m**,
   in a local metric projection, then encoded as a polyline (below). Consecutive points
   that round to the same 0.00001° are dropped.
6. **Orient lifts.** OpenStreetMap draws lifts in the direction of travel. If a lift's
   first point is at least 5 m higher than its last, the line is reversed, so `geom`
   runs bottom → top.
7. **Hull.** Each area gets an outline used to detect which resort a route is in:
   - its own polygon from OpenSkiData, when that polygon holds at least 90% of the area's
     run and lift vertices;
   - that polygon **joined with** the convex hull of its runs and lifts buffered 300 m,
     when the polygon holds less (375 of 2,816 polygons in the 2026-11-13 build: many
     outline only a base area or one sector, and Ylläs's held 1% of its runs);
   - the convex hull of its runs and lifts buffered 300 m, when it has no polygon.

   The bundle's hull is simplified at 5 m and the index's at 50 m.
8. **Key** each area with a stable id (below). OpenSkiData's own `feature_id`s are content
   hashes that change every build, so they never appear in the output.
9. **Write** `v1/areas/{key}.json` for each area whose content hash changed (or that is
   missing), then `v1/index.json`, then `v1/build.json` last. A run that fails halfway
   leaves the old `build.json`, so the next run retries everything.

## Files

| URL | What |
|---|---|
| `https://ski.homeboundapp.com/v1/index.json` | every area: key, name, outline, size, hash |
| `https://ski.homeboundapp.com/v1/areas/{key}.json` | one area's runs and lifts |
| `https://ski.homeboundapp.com/v1/build.json` | when and from what the files were built |

Every object is stored **gzip-compressed** and served with:

    Content-Type: application/json
    Content-Encoding: gzip
    Cache-Control: public, max-age=3600

`URLSession`, `httpx` and `requests` decompress it transparently. Python's `urllib` does
not: if a body starts with the bytes `1f 8b`, gunzip it first. The JSON is UTF-8, compact
(no whitespace), and keys appear in the order documented below.

Size of the 2026-11-13 build: 5,448 areas, 98,396 runs, 27,140 lifts. Bundles total
20.6 MB (6.9 MB gzipped); the median bundle is 0.7 KB gzipped and the largest, Dolomiti
Superski, is 92 KB. The index is 1.28 MB (0.61 MB gzipped). Park City Mountain Resort
(`wd-Q3821979`) is 67 KB (17.5 KB gzipped).

## Format (version 1)

### Conventions

- **Coordinates** are `[lat, lon]` in degrees (WGS 84), rounded to 5 decimals.
- **`bbox`** is `[min_lat, min_lon, max_lat, max_lon]` (the same order as the Homebound
  backend's route `bbox`).
- **Polylines** use the [Google encoded polyline
  algorithm](https://developers.google.com/maps/documentation/utilities/polylinealgorithm)
  with precision 5, latitude first, rounding half up (`floor(v × 1e5 + 0.5)`). The encoder
  is byte-for-byte the Homebound backend's `route_stats.encode_line`, so the backend's
  `route_stats.decode_line` and the app's `RoutePolyline` read them as they are. Example:
  `_p~iF~ps|U_ulLnnqC_mqNvxq`@` is `(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)`.
- **Hulls** are a list of polylines, one per ring. Rings are implicitly closed (the last
  point does not repeat the first) and have at least 3 points. A hull can have several
  rings: separate parts of a multipolygon, and holes. A point is inside the hull when it is
  inside an **odd** number of rings (even–odd rule), which handles parts and holes alike.
- **Nullable** fields are marked `?`. Absent data is `null`, never an empty string.
- **Forward compatibility:** new fields may be added to version 1. Readers must ignore
  fields and enum values they don't know (treat an unknown `difficulty` as unknown, an
  unknown `color` as `grey`, an unknown lift `type` as a generic lift). A breaking change
  gets a new `v2/` prefix while `v1/` stays up.

### Area keys

| Form | From | Example |
|---|---|---|
| `wd-{Qid}` | the area's Wikidata id | `wd-Q3821979` (Park City Mountain Resort) |
| `osm-{r\|w\|n}{id}` | the area's own OpenStreetMap relation, way or node | `osm-r3545276` (Les Trois Vallées) |
| `sm-{id}` | its Skimap.org id, when it has no OSM object | `sm-1024` (Hurricane Ridge) |
| `gen-{r\|w\|n}{id}` | an area OpenSkiData generated around runs that no mapped area claims: the lowest OSM id among its runs and lifts, preferring one that is in no other area. These have no name | `gen-r10978224` |

The first that applies wins, in that order. Keys match `^[a-z]+-[A-Za-z0-9]+$`. A key is
stable across OpenSkiData builds; it changes only if the identity it comes from does (for
example, someone adds a Wikidata tag, so `osm-w…` becomes `wd-Q…`). A reader that stored a
key should handle a later 404 by detecting the area again from the index.

Areas **overlap**. Ski-pass groupings (Dolomiti Superski, Ski amadé, Les Trois Vallées)
contain resorts that also have bundles of their own, and about 9,300 runs are in more
than one bundle. When several hulls hold a route, the reader decides (for example, the
area holding the most points, then the smaller hull).

### `v1/index.json`

```json
{
  "v": 1,
  "osd_build": "2026-11-13T01:41:53.171Z",
  "areas": [
    {
      "key": "wd-Q3821979",
      "name": "Park City Mountain Resort",
      "centroid": [40.6569, -111.53786],
      "bbox": [40.60878, -111.60016, 40.69961, -111.49823],
      "hull": ["<polyline ring>"],
      "bytes": 17466,
      "hash": "604270e2212d17b4"
    }
  ],
  "license": "ODbL-1.0",
  "attribution": "© OpenStreetMap contributors, OpenSkiData"
}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Format version: `1`. |
| `osd_build` | string | The OpenSkiData build these files came from (`metadata.json` `run.finishedAt`). |
| `areas` | array | Every area, sorted by `key`. |
| `areas[].key` | string | The area key (above). |
| `areas[].name` | string? | Its name. About 17% of areas have none (every `gen-` area, and some small ones). |
| `areas[].centroid` | `[lat, lon]` | OpenSkiData's point for the area. Usually, not always, inside the hull. |
| `areas[].bbox` | bbox | Same as the bundle's `bbox`. |
| `areas[].hull` | string[] | The hull, simplified at 50 m. |
| `areas[].bytes` | int | Size of the bundle object as stored and transferred (gzip). |
| `areas[].hash` | string | First 16 hex characters of the SHA-256 of the bundle's **uncompressed** JSON. It changes exactly when the bundle's content does: re-download a cached bundle when this differs. |
| `license`, `attribution` | string | As in every file. |

### `v1/areas/{key}.json`

```json
{
  "v": 1,
  "key": "wd-Q3821979",
  "name": "Park City Mountain Resort",
  "website": "https://parkcitymountain.com/",
  "convention": "north_america",
  "bbox": [40.60878, -111.60016, 40.69961, -111.49823],
  "hull": ["<polyline ring>"],
  "min_alt": 2101.2,
  "max_alt": 3040.9,
  "runs": [
    {"osm_id": "w260786241", "name": "Fright Gully", "ref": null, "difficulty": "expert",
     "color": "black", "oneway": false, "geom": "<polyline>"}
  ],
  "lifts": [
    {"osm_id": "w10159691", "name": "Town Lift", "type": "chair_lift", "geom": "<polyline>",
     "bottom_alt": 2131.2, "top_alt": 2489.9}
  ],
  "license": "ODbL-1.0",
  "attribution": "© OpenStreetMap contributors, OpenSkiData"
}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Format version: `1`. |
| `key` | string | The area key; equals the file name. |
| `name` | string? | The area's name. |
| `website` | string? | Its first listed website. |
| `convention` | string? | The difficulty colour convention: `europe`, `north_america` or `japan`. |
| `bbox` | bbox | Bounds of the hull and of every run and lift. |
| `hull` | string[] | The hull, simplified at 5 m. |
| `min_alt`, `max_alt` | number? | The area's lowest and highest elevation, metres. |
| `runs` | array | Downhill runs, sorted by `osm_id` (relations, then ways, then nodes; then by number). |
| `lifts` | array | Operating lifts, sorted the same way. |
| `license` | string | `"ODbL-1.0"` |
| `attribution` | string | `"© OpenStreetMap contributors, OpenSkiData"` |

**`runs[]`**

| Field | Type | Meaning |
|---|---|---|
| `osm_id` | string | The run's OpenStreetMap id: a type letter (`r`, `w`, `n`) and number, e.g. `w260786241`. OpenSkiData merges ways into one run; this is the lowest of them, so it stays put unless that way is deleted. **Not unique:** OpenSkiData can put one way in two run features (a way that is both its own piste and part of a named route). Features that share an `osm_id` overlap on the ground; treat them as the same piste when comparing runs across days. |
| `name` | string? | The run's name. |
| `ref` | string? | Its piste number or reference, e.g. `"3"` (common in Europe). |
| `difficulty` | string? | `novice`, `easy`, `intermediate`, `advanced`, `expert`, `freeride` or `extreme`, from OSM `piste:difficulty`. |
| `color` | string | `green`, `blue`, `red`, `black`, `orange` or `grey`: the colour OpenSkiMap uses for this difficulty under the area's convention (table below). `grey` means the difficulty is unknown. |
| `oneway` | bool | `true` when OSM marks the run one-way; `false` otherwise, including when it isn't tagged. |
| `geom` | string | The run as a polyline, simplified at 5 m, in the direction OSM draws it (normally downhill). |

`color` is OpenSkiData's `getRunColorName(convention, difficulty)`
([openskidata-format `Run.ts`](https://github.com/russellporter/openskidata-format/blob/master/src/Run.ts)).
The GeoPackage doesn't carry it, so the script computes it the same way:

| `difficulty` | `north_america` | `europe` | `japan` |
|---|---|---|---|
| `novice` | green | green | green |
| `easy` | green | blue | green |
| `intermediate` | blue | red | red |
| `advanced` | black | black | black |
| `expert` | black | black | black |
| `freeride`, `extreme` | orange | orange | orange |
| `null` | grey | grey | grey |

In North America `expert` is the double black diamond; tell it apart from `advanced` by
`difficulty`, not by `color`.

**`lifts[]`**

| Field | Type | Meaning |
|---|---|---|
| `osm_id` | string | The lift's OpenStreetMap id, e.g. `w10159691`. Unique within a bundle. |
| `name` | string? | The lift's name. |
| `type` | string? | `cable_car`, `gondola`, `chair_lift`, `mixed_lift`, `drag_lift`, `t-bar`, `j-bar`, `platter`, `rope_tow`, `magic_carpet`, `funicular` or `railway`. |
| `geom` | string | The lift as a polyline, simplified at 5 m, running **bottom → top** (see Method, step 6). |
| `bottom_alt` | number? | Elevation of `geom`'s first point, metres. |
| `top_alt` | number? | Elevation of `geom`'s last point, metres. On a nearly flat lift (a magic carpet) it can be a little lower than `bottom_alt`, because a difference under 5 m doesn't reverse the line. |

### `v1/build.json`

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Format version: `1`. |
| `osd_build` | string | OpenSkiData's build id (`run.finishedAt`). |
| `osd_format` | string? | OpenSkiData's `formatVersion`, e.g. `"16.0.0"`. |
| `osm_data_at` | string? | The OpenStreetMap data timestamp in that build. |
| `builder` | int | The script's `BUILDER_VERSION`. |
| `built_at` | string | When this build finished, UTC, `YYYY-MM-DDTHH:MM:SSZ`. |
| `areas`, `runs`, `lifts` | int | Counts in this build. |
| `index_hash` | string | 16 hex characters of the SHA-256 of `index.json`'s uncompressed JSON. |
| `index_bytes` | int | `index.json`'s gzip size. |
| `changed` | int | How many bundles this build wrote. |
| `license`, `attribution` | string | As in every file. |

## Running it

Python 3.12:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r skidata/requirements.txt

# Build everything into a folder (uncompressed JSON at the object keys):
python skidata/build_bundles.py --out /tmp/ski-bundles

# Reuse a GeoPackage you already downloaded, and look at one area:
python skidata/build_bundles.py --gpkg openskidata.gpkg --only wd-Q3821979 --out /tmp/ski

# Upload to R2 (what the workflow runs):
R2_ACCOUNT_ID=… R2_SKI_ACCESS_KEY_ID=… R2_SKI_SECRET_ACCESS_KEY=… R2_SKI_BUCKET=homebound-ski \
  python skidata/build_bundles.py --upload
```

| Flag | Does |
|---|---|
| `--out DIR` | Writes the objects under `DIR` as plain JSON (serve it with any static server to test a client). |
| `--upload` | Uploads to R2 at `https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com`, bucket `R2_SKI_BUCKET`, with the keys `R2_SKI_ACCESS_KEY_ID` / `R2_SKI_SECRET_ACCESS_KEY`. |
| `--gpkg PATH` | Uses a local `openskidata.gpkg` instead of downloading one. |
| `--metadata PATH` | Uses a local `metadata.json` instead of fetching it. |
| `--only KEY` | Builds one area and prints its counts; no index. Can't be combined with `--upload`. |
| `--force` | Builds even when OpenSkiData's build hasn't changed. |
| `--allow-shrink` | Publishes even when the index would lose more than 20% of its areas. |

`--out` and `--upload` can be combined. Exit codes: `0` done (or nothing to do), `1` an
unexpected error (including missing R2 variables), `2` the GeoPackage isn't shaped as
expected, `3` a publish check refused.

Tests run on a small GeoPackage the tests write themselves, with no network:

```sh
pip install -r skidata/requirements.txt pytest
python -m pytest skidata/tests -q
```

## Changing the format

- Bump `BUILDER_VERSION` in `build_bundles.py` with any change to what's built. The next
  run then rebuilds even if OpenSkiData hasn't changed.
- Additive changes (a new field) stay in `v1/`. Update this README, and tell the readers:
  the Homebound backend (`src/services/ski_resorts.py`) and app (`SkiResortStore`).
- A breaking change goes to `v2/`, and `v1/` keeps being published until no supported app
  version reads it.
