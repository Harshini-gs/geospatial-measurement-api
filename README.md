# Geospatial File Measurement API

A backend service that accepts a **Shapefile** (as a `.zip`) or a **KML/KMZ**
file, extracts every feature, and returns area and length measurements in
metres — never in degrees.

Built with **FastAPI**, **Shapely** and **pyproj**. No GDAL, no system
dependencies: `pip install -r requirements.txt` works on a clean machine.

---

## Contents

1. [Quick start](#quick-start)
2. [API reference](#api-reference)
3. [Architecture](#architecture)
4. [CRS handling](#crs-handling)
5. [Design decisions](#design-decisions)
6. [Testing](#testing)
7. [What I learned](#what-i-learned)
8. [Known limitations](#known-limitations)
9. [Future scope](#future-scope)

---

## Quick start

Requires Python 3.10+.

```bash
git clone <this-repo>
cd geospatial-measurement-api

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open:

| URL | What it is |
|-----|------------|
| <http://localhost:8000/> | Upload UI — drag a file in and see measurements |
| <http://localhost:8000/docs> | Interactive Swagger docs, generated from the code |
| <http://localhost:8000/redoc> | Alternative API reference |

No database setup is needed. SQLite is created on first run.

### Generate sample files

```bash
python scripts/make_sample_data.py
```

Writes `survey_plots.zip`, `pipelines.zip` and `no_projection.zip` into
`sample_data/`, alongside the committed `survey_site.kml`.

### Try it in 30 seconds

```bash
# 1. Upload
curl -s -F "file=@sample_data/survey_site.kml" http://localhost:8000/api/files/

# 2. Check status (copy the id from above)
curl -s http://localhost:8000/api/files/<id>/

# 3. Get measurements
curl -s http://localhost:8000/api/files/<id>/measurements/
```

### With Docker

```bash
docker compose up --build
```

---

## API reference

All responses are JSON. Every error uses the same envelope:

```json
{ "error": { "code": "invalid_archive", "message": "...", "detail": { } } }
```

### `POST /api/files/`

Uploads and queues a file. Accepts `multipart/form-data` with a `file` field.

Accepted: `.zip` (Shapefile), `.kml`, `.kmz`

Returns **202 Accepted** — processing runs in the background.

```json
{
  "id": "3f2a9c8e4b1d4e7a9c0f5b2d8e6a1c3f",
  "filename": "survey_site.kml",
  "file_type": "KML",
  "size_bytes": 2847,
  "status": "PENDING",
  "created_at": "2026-10-08T09:14:22.104Z",
  "message": "File accepted. Poll the status endpoint until status is COMPLETED.",
  "links": {
    "self": "http://localhost:8000/api/files/3f2a.../",
    "measurements": "http://localhost:8000/api/files/3f2a.../measurements/",
    "features": "http://localhost:8000/api/files/3f2a.../features/"
  }
}
```

| Status | Meaning |
|--------|---------|
| 202 | Accepted and queued |
| 400 | Empty file |
| 413 | Exceeds `MAX_UPLOAD_BYTES` |
| 415 | Unsupported extension |

### `GET /api/files/{id}/`

File information and processing state.

```json
{
  "id": "3f2a9c8e4b1d4e7a9c0f5b2d8e6a1c3f",
  "filename": "survey_site.kml",
  "file_type": "KML",
  "size_bytes": 2847,
  "status": "COMPLETED",
  "crs": "EPSG:4326",
  "crs_name": "WGS 84",
  "layer_name": "Sample Survey Site",
  "feature_count": 5,
  "error_message": null,
  "warnings": [],
  "created_at": "2026-10-08T09:14:22.104Z",
  "updated_at": "2026-10-08T09:14:22.331Z",
  "processing_duration_seconds": 0.0271
}
```

`status` moves `PENDING → PROCESSING → COMPLETED | FAILED`. On `FAILED`,
`error_message` explains why.

### `GET /api/files/{id}/measurements/`

Per-feature measurements plus whole-file totals.

Query parameters: `limit`, `offset`, `include_geometry` (default `false`).

```json
{
  "file_id": "3f2a9c8e4b1d4e7a9c0f5b2d8e6a1c3f",
  "filename": "survey_site.kml",
  "status": "COMPLETED",
  "crs": "EPSG:4326",
  "summary": {
    "feature_count": 5,
    "measured_count": 5,
    "unsupported_count": 0,
    "geometry_type_counts": {
      "Polygon": 1, "LineString": 1, "MultiLineString": 1,
      "Point": 1, "MultiPolygon": 1
    },
    "total_area_sqm": 1221458.6342,
    "total_area_hectares": 122.145863,
    "total_area_sqkm": 1.22145863,
    "total_length_m": 2419.8871,
    "total_length_km": 2.4198871,
    "measurement_methods_used": ["utm_projection"]
  },
  "pagination": { "total": 5, "limit": 100, "offset": 0, "has_more": false },
  "measurements": [
    {
      "feature_index": 0,
      "feature_id": "plot-A1",
      "geometry_type": "Polygon",
      "supported": true,
      "skip_reason": null,
      "measurement": {
        "measurement_type": "area",
        "area_sqm": 1194856.2471,
        "area_hectares": 119.485625,
        "area_sqkm": 1.19485625,
        "perimeter_m": 4381.9043,
        "length_m": null,
        "length_km": null,
        "measurement_crs": "EPSG:32643",
        "measurement_method": "utm_projection"
      },
      "properties": {
        "name": "Plot A1",
        "owner": "Site Operations",
        "land_use": "industrial",
        "survey_date": "2026-03-14"
      },
      "bbox": [77.59, 12.97, 77.6, 12.98],
      "geometry": null
    }
  ]
}
```

Points return `"measurement_type": "none"` with null measurements and
`"supported": true` — per the spec, a point needs no measurement, so this is a
success, not an error.

### `GET /api/files/{id}/features/`

The same features as a valid **GeoJSON FeatureCollection**, so the output drops
straight into Leaflet, Mapbox, QGIS or geopandas. Measurements ride along inside
each feature's `properties` under the namespaced `_measurement` key.

### `GET /api/files/` · `DELETE /api/files/{id}/` · `GET /api/health`

List uploads (paginated, filterable by `status`), delete a file and its
features, and check service and database health.

---

## Architecture

### Application structure

```
app/
├── main.py                 FastAPI factory, exception handlers, frontend mount
├── config.py               Pydantic settings from environment
├── database.py             SQLAlchemy engine and session
├── models.py               ORM: UploadedFile, Feature
├── schemas.py              Pydantic request/response contracts
├── core/
│   ├── exceptions.py       AppError hierarchy, each with a status + code
│   └── logging.py          Logging setup
├── api/routes/
│   ├── files.py            Upload, info, measurements, features, delete
│   └── health.py           Health check
└── services/
    ├── parsers/
    │   ├── base.py             Abstract parser + ParsedFeature/ParsedDataset
    │   ├── archive.py          Safe ZIP extraction (zip-slip, zip-bomb guards)
    │   ├── shapefile_parser.py pyshp-based reader
    │   ├── kml_parser.py       Hand-written KML/KMZ XML reader
    │   └── registry.py         Filename → parser dispatch
    ├── crs.py              CRS strategy selection
    ├── measurement.py      Area/length computation
    └── processor.py        Pipeline orchestration
```

Layering is strict and one-directional:

```
routes → services → parsers
   ↓         ↓
 schemas   models
```

Routes never touch Shapely or pyproj. Parsers never touch the database. The
measurement engine knows nothing about HTTP. Each layer is testable alone.

### File-processing flow

```
POST /api/files/
      │
      ├─ detect format from extension ──► 415 if unknown
      ├─ stream to disk in 1 MB chunks ─► 413 if over the limit
      ├─ INSERT UploadedFile (PENDING)
      └─ return 202 + id                  ← client is released here
            │
            └─► BackgroundTask
                  │
                  ├─ status = PROCESSING
                  ├─ parser.parse(path)
                  │     ├─ zip: validate every member, then extract
                  │     ├─ read geometry + attributes + CRS
                  │     └─ per-feature errors recorded, not raised
                  │
                  ├─ for each feature: measure(geometry, source_crs)
                  ├─ bulk INSERT features
                  └─ status = COMPLETED | FAILED
```

Uploads return immediately because parsing a large survey file can take
seconds. Returning `202` with a pollable status is the honest representation of
work that has not finished yet.

Two failure boundaries keep one bad feature from sinking a file:

* **Per-feature** — a corrupt record is stored with a `parse_error` and
  `supported: false`. Its neighbours are measured normally.
* **Per-file** — an unreadable archive sets `status = FAILED` with a reason.
  The API still returns 200 for the status query; the *file* failed, not the
  *request*.

### Measurement calculation flow

```
measure(geometry, source_crs)
      │
      ├─ None / empty         → supported: false, with a reason
      ├─ Point / MultiPoint   → supported: true, measurement_type: "none"
      ├─ GeometryCollection   → recurse into members, aggregate totals
      │
      └─ Polygon / LineString (and Multi* variants)
            │
            ├─ source CRS projected? ──► measure in place, scale units → m
            │
            └─ source CRS geographic? ─► pick a strategy (below)
                   ├─ UTM      → reproject, then planar area/length
                   └─ geodesic → compute on the WGS84 ellipsoid
```

`measure()` never raises. Every failure path returns a `Measurement` with
`supported: false` and a human-readable `skip_reason`, because a single
malformed geometry in a 50 000-feature file should not produce a 500.

---

## CRS handling

This is the heart of the assignment, so it gets its own section.

**The problem.** A degree of longitude is about 111 km at the equator and 0 km
at the poles. Computing `polygon.area` on EPSG:4326 coordinates yields "square
degrees", a unit that does not correspond to any physical area. The sample plot
in `survey_site.kml` measures `0.0001` in square degrees and `1 194 856 m²` in
reality — a factor of twelve billion.

**The solution**, chosen **per feature**:

| Condition | Strategy | Reported as |
|---|---|---|
| Source CRS is projected | Measure directly in it; scale the CRS's linear unit to metres | `source_projected_crs` |
| Geographic, feature fits one UTM zone, \|lat\| ≤ 84° | Reproject to `EPSG:326xx`/`327xx`, then planar maths | `utm_projection` |
| Geographic, spans > 6° longitude, or polar | Geodesic computation on the WGS84 ellipsoid | `geodesic` |

Every response names the method and CRS used, so the number is auditable rather
than magic.

**Why per feature, not per file.** A single upload can hold a plot in Karnataka
and a plot in Queensland. Picking one CRS for the whole file would measure one
of them through heavy distortion. Choosing per feature costs a cached
`Transformer` lookup and makes both correct.

**Why UTM for local data.** UTM is conformal with scale error below roughly
1 part in 1000 inside its 6° zone. Drone and site survey data — AEREO's actual
domain — is almost always well inside one zone, so this is both accurate and
fast.

**Why geodesic as the fallback rather than an equal-area projection.** An
equal-area projection such as EPSG:6933 would give correct areas but wrong
lengths, so lines would need a different CRS from polygons. Geodesic
computation via `pyproj.Geod` (Karney's algorithms) is exact for both, at any
size, with no zone limits. It is slower, which is why it is the fallback and
not the default.

**Non-metric projected CRSs are handled.** US State Plane zones use US survey
feet. The code reads `CRS.axis_info[0].unit_conversion_factor` and converts, so
responses are always in metres. There is a test for EPSG:2229 proving it.

**Axis order.** Every `Transformer` is built with `always_xy=True`. EPSG:4326's
authority-defined axis order is *(latitude, longitude)*, while virtually every
file format stores *(longitude, latitude)*. Omitting this flag silently swaps
coordinates, which is the single most common bug in geospatial code. It would
have put the sample data in the Indian Ocean.

---

## Design decisions

### FastAPI over Django + DRF

Django's strengths — the ORM, admin, auth, migrations — were not what this
problem needed. This is a stateless processing service with three endpoints.
FastAPI gave async file streaming, Pydantic validation, and an OpenAPI schema
generated from the code rather than maintained by hand.

*Trade-off:* Django would be the better call once this grows multi-tenant auth,
per-user quotas and an admin console. That is a real argument, not a dismissal.

### pyshp + Shapely + pyproj, instead of GeoPandas or GDAL

The obvious choice is `geopandas.read_file()`, which reads both formats in one
line. I deliberately avoided it.

GeoPandas pulls in GDAL. GDAL is the single most common reason a Python
geospatial project fails to install — it needs system libraries, the wheels are
large and platform-sensitive, and the usual fix is "use conda" or "use Docker".
For a service that reads exactly two well-documented formats, I was paying a
very large dependency cost for a very small slice of GDAL's capability.

Instead: `pyshp` (pure Python) for shapefiles, a hand-written XML reader for
KML, `shapely` and `pyproj` for geometry and projection — both of which ship
self-contained wheels with GEOS and PROJ bundled.

The result is that `pip install -r requirements.txt` succeeds on a clean
Windows, macOS or Linux machine with no system packages, and the Docker image
needs no `apt-get` layer at all.

*Trade-off:* adding GeoJSON, GPX or GeoPackage support now means writing a
parser rather than getting it free. Given the registry pattern, each is roughly
one small class — and I would rather pay that than make installation fragile.

### Writing the KML parser by hand

KML files in the wild declare KML 2.0, 2.1, 2.2, Google's `gx` extensions, or
no namespace at all. Matching on the *local* tag name handles every variant
uniformly. Writing it directly also meant correct handling of nested
`<Folder>` hierarchies, `<innerBoundaryIs>` holes, both `<ExtendedData>`
dialects, and dropping altitude from `lon,lat,alt` tuples. It is about 250
lines and has no dependencies beyond the standard library.

### Background processing with a status state machine

The spec's example response includes `"status": "COMPLETED"`, which implies the
work is not synchronous. `BackgroundTasks` gives that without adding Redis and
Celery to a take-home.

*Trade-off:* the task dies with the process, and there is no retry or
visibility. That is the correct next step, not something to fake now — see
[Future scope](#future-scope).

### Persisting measurements instead of recomputing

Parsing and projecting is the expensive part, and the result is immutable once
the source file is stored. Measurements are computed once and written to the
`features` table, so repeat reads are plain indexed queries. Summary totals are
aggregated **in SQL**, not by loading every feature into Python, which keeps
`/measurements/` cheap for files with hundreds of thousands of features.

### Treating uploads as hostile

A `.zip` from the internet is attacker-controlled input. `archive.py` validates
every member *before* writing a byte:

* **Zip-slip** — members with `..` or absolute paths are refused, so nothing
  can be written outside the extraction directory.
* **Zip-bomb** — total uncompressed size and per-member compression ratio are
  both capped.

Uploads are also streamed to disk in 1 MB chunks with the size limit enforced
mid-stream, so a large upload never lands in memory and a rejected one never
leaves a partial file behind. All four guards have tests.

### Geometry excluded from `/measurements/` by default

A survey file can carry millions of coordinates. A client asking for
measurements usually does not want the geometry too, so it is opt-in via
`include_geometry=true`, and `/features/` exists for when the geometry *is* the
point.

### Alternatives I considered and rejected

| Considered | Rejected because |
|---|---|
| GeoPandas / Fiona | GDAL dependency; the install fragility outweighed the convenience |
| PostGIS `ST_Area(geography)` | Correct and fast, but makes Postgres mandatory for a reviewer who should be able to run `uvicorn` and be done |
| Synchronous processing | Simpler, but a large file would hold the HTTP connection open for seconds |
| One CRS per file | Cheaper, but wrong for datasets spanning zones |
| Equal-area projection as fallback | Correct for area, wrong for length; geodesic handles both |
| Storing geometry as WKT | GeoJSON is directly usable by web clients without a translation step |

---

## Testing

```bash
pip install -r requirements.txt
pytest -v
```

Roughly 70 tests across four files:

| File | Covers |
|---|---|
| `tests/test_crs.py` | UTM zone derivation, strategy selection, unit conversion |
| `tests/test_measurement.py` | Area/length correctness, every geometry type, failure paths |
| `tests/test_parsers.py` | Shapefile and KML parsing, archive safety |
| `tests/test_api.py` | Full upload → poll → measure flow, error envelopes |

Two tests matter more than the rest:

**`test_area_is_not_computed_in_square_degrees`** asserts directly against the
failure mode the spec calls out — that the returned area differs from the naive
degree area by a factor of more than 10⁹.

**`test_utm_and_geodesic_agree_for_a_local_polygon`** cross-validates the
projected path against the ellipsoidal one. Two mathematically independent
methods agreeing to within 0.1% is far stronger evidence of correctness than
either matching a number I wrote down myself.

The UTM assertions use real cities (Bengaluru → 43N, London → 30N, Sydney →
56S) whose zones are independently documented, so a regression is caught
against ground truth rather than against whatever the code happened to return
when the test was written.

---

## What I learned

**Degrees are not a unit of distance.** I knew this abstractly. Seeing the same
polygon return `0.0001` and `1 194 856` depending on one projection step made
it concrete, and made me realise the CRS decision is the actual engineering
problem here — the rest is plumbing.

**Axis order is a silent failure.** `always_xy=True` is four characters that
decide whether your data is in Bengaluru or in the Indian Ocean. Nothing throws
if you get it wrong; you just get confident, wrong numbers. I now treat any
coordinate transform without an explicit axis convention as a bug.

**Choosing boring dependencies is an engineering decision.** My first instinct
was `geopandas.read_file()` — one line, both formats. Working through what GDAL
would cost a reviewer, a CI pipeline and a Docker image changed my mind. The
hand-written KML parser is more code, but the project installs anywhere.

**"Handle it gracefully" means deciding what failure *is*.** The spec says
unsupported geometry should not crash the API. The interesting question was
which outcomes are failures at all: a Point has no area, but that is a correct
answer, not an error. So points return `supported: true` with null
measurements, while genuinely unreadable geometry returns `supported: false`
with a reason. That distinction shaped the whole response schema.

**Cross-validation beats assertion.** I initially wrote tests asserting
hardcoded expected areas, then realised I was only testing that the code still
did what it did when I wrote it. Checking UTM against geodesic tests the maths
instead of the memory.

---

## Known limitations

Stated plainly rather than hidden:

* **Measurements are 2D.** Z coordinates are parsed and dropped. "Area" here
  means planimetric area, which is what cadastral and survey work means by it —
  not surface area draped over terrain. True 3D surface area would need a DEM.
* **UTM zone exceptions are not implemented.** The widened zones around Norway
  (32V) and Svalbard are not special-cased. An affected feature is measured in
  the neighbouring zone, which adds minor distortion rather than breaking.
* **Background tasks are in-process.** A restart mid-processing leaves a file
  stuck in `PROCESSING`. There is no sweeper yet.
* **Self-intersecting polygons** are measured as-is rather than repaired. The
  area is returned and finite, but the number is only as meaningful as the
  input geometry.
* **One layer per shapefile archive.** If a `.zip` holds several `.shp` files,
  the first is used and the rest are logged.
* **No authentication.** Out of scope for the assignment, and the first thing
  I would add.

---

## Future scope

Ordered by what I would actually do first.

1. **Celery + Redis for processing.** Replace `BackgroundTasks` with a real
   queue: retries, progress reporting, a dead-letter queue, and tasks that
   survive a restart. The status state machine is already shaped for this —
   only the dispatch changes.
2. **Authentication and multi-tenancy.** API keys or JWT, with files scoped to
   an owner. Currently every upload is globally visible.
3. **Alembic migrations.** `create_all()` is fine for a take-home; a service
   that ships needs versioned schema changes.
4. **PostGIS.** Store geometry in a real spatial column and get indexed spatial
   queries — "which features intersect this bounding box" — for free. The
   SQLAlchemy layer is already database-agnostic; this is an env-var change
   plus a geometry column type.
5. **Object storage.** Uploads go to S3 or GCS rather than local disk, so the
   service can scale horizontally.
6. **More formats.** GeoJSON, GPX, GeoPackage, DXF. The registry pattern means
   each is one class and one line.
7. **Coordinate simplification for large geometries.** Douglas-Peucker on the
   stored GeoJSON for display, keeping full precision for measurement.
8. **Prometheus metrics and structured JSON logging** — processing duration by
   format, failure rates by error code, measurement method distribution.
9. **Rate limiting** on the upload endpoint.
10. **3D measurement** against an uploaded DEM, for genuine surface area — the
    natural extension for drone-derived data.

---

## Tech stack

| Layer | Choice |
|---|---|
| API | FastAPI, Pydantic v2 |
| Server | Uvicorn |
| ORM | SQLAlchemy 2.0 |
| Database | SQLite (default), Postgres-ready |
| Geometry | Shapely 2.x |
| Projection / geodesy | pyproj (PROJ bundled) |
| Shapefile | pyshp |
| KML | Hand-written, stdlib `xml.etree` |
| Frontend | Single-file HTML/CSS/JS, no build step |
| Tests | pytest |
| Container | Docker, `python:3.12-slim` |
