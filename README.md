# Geospatial File Measurement API

## Overview

A FastAPI backend for uploading KML files and zipped Shapefiles, extracting feature
metadata, and measuring Polygon area and LineString length in a suitable projected
CRS. Processing is synchronous: an upload response is returned after processing.
Results are persisted in SQLite and retrieved without rereading the original file.

## Features

### Assignment Requirements

- Accept `.kml` and `.zip` containing one Shapefile dataset.
- Extract zero-based feature index, available source ID, geometry/type, source CRS,
  and properties. Source geometry and available KML IDs are stored internally;
  the measurements endpoint exposes index, type, properties, and measurement metadata.
- Calculate Polygon area, including holes, in `square_meters`; calculate LineString
  length in `meters`. Points require no measurement.
- Represent unsupported, empty, invalid, and failed features explicitly.
- Reproject geographic coordinates before measurement; never treat degrees as meters.
- Persist upload metadata, processing status, feature results, and safe errors.
- Provide file-information and measurement retrieval endpoints, tests, and documentation.

### Additional Enhancement — Smart CRS Resolution

- Preserve suitable projected CRSs when metric units, area of use, and sampled
  distortion satisfy the resolver's policy.
- Otherwise select a local UTM zone and hemisphere automatically for each measured feature.
- Expose `source_crs`, `measurement_crs`, `projection_strategy`, and warnings.
- Reject unknown source CRS instead of guessing it.

**Optional dataset analysis** is another enhancement beyond the assignment:
`GET /api/files/{id}/analysis/` aggregates stored results without reprocessing.

## Technology Stack

| Component | Technology |
|---|---|
| Runtime and API | Python 3.11+, FastAPI, Uvicorn |
| Validation and configuration | Pydantic, pydantic-settings |
| Persistence | SQLAlchemy, SQLite, Alembic |
| Shapefile reading | GeoPandas, Pyogrio/GDAL |
| Geometry and reprojection | Shapely/GEOS, PyProj/PROJ, NumPy |
| Safe XML and ownership | defusedxml, filelock |
| Tests and packaging | pytest, HTTPX, pyproject.toml, uv.lock |
| Container | Python 3.12 Debian slim, Docker |

## Architecture

Routes handle HTTP contracts and delegate to services. Parsing, CRS selection,
measurement, and persistence orchestration have separate responsibilities; reader,
CRS, and measurement logic do not depend on FastAPI.

```text
Upload
  -> Validation
  -> Parsing
  -> CRS Resolution
  -> Reprojection
  -> Measurement
  -> Persistence
  -> API Response
```

Validation creates an isolated UUID directory and a `PENDING` record. Processing
commits `PROCESSING`, reads the dataset, records discovered metadata, then resolves
and measures eligible features. Results, counters, and `COMPLETED` status are
committed together. A file-level failure rolls back partial feature results and
records `FAILED`; previously discovered metadata is retained.

The database has two main tables: `UploadedFile` owns metadata and counters;
`FeatureResult` stores one normalized outcome per feature. Foreign keys, unique
feature indexes per upload, and check constraints enforce result consistency.
Versioned migrations run automatically during startup.

## Project Structure

```text
app/
  main.py                 # Application factory, lifespan, route registration
  config.py               # Environment-backed settings and resource limits
  api/                    # Thin routes and raw upload admission middleware
  models/                 # Upload and feature SQLAlchemy models
  schemas/                # Public Pydantic response contracts
  services/
    uploads.py            # Filename, storage, ZIP validation and extraction
    reader.py             # KML/Shapefile parsing and normalized features
    crs.py                # Independent projection selection
    measurements.py       # Reprojection and horizontal measurements
    processing.py         # Lifecycle, transactions, recovery and cleanup
    files.py              # Persisted file/measurement retrieval
    analysis.py           # Optional persisted dataset aggregation
    errors.py             # Upload-domain errors
  utils/                  # Coordinate validation and exclusive ownership locks
  db/                     # Sessions, declarative base and migration runner
migrations/               # Alembic environment and schema revisions
tests/
  fixtures/               # Polygon, line, point, Shapefile and malformed samples
  test_*.py               # Unit, integration and security regression tests
uploads/                  # Local upload storage; contents excluded from version control
.env.example
alembic.ini
pyproject.toml
uv.lock
Dockerfile
.dockerignore
README.md
```

## Setup

Use Python 3.11 or newer and run commands from the project root. With
[uv](https://docs.astral.sh/uv/), on Windows PowerShell:

```powershell
uv sync --extra dev --locked
Copy-Item .env.example .env
```

Alternatively:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Settings load `.env` and `GEOSPATIAL_` environment variables. Defaults include:

| Setting | Default |
|---|---|
| Database / upload directory | `sqlite:///./geospatial.db` / `uploads/` |
| File / total multipart request | 20 MiB / 21 MiB |
| Expanded ZIP / entries / compression ratio | 100 MiB / 100 / 200:1 |
| Concurrent upload requests | 2 |
| Features / coordinates per dataset | 10,000 / 250,000 |
| Properties / attribute fields | 16 KiB per feature / 128 |
| KML nesting depth | 32 |
| Upload storage quota | 512 MiB |
| Failed-upload retention | 30 days; cleanup at startup |

See `.env.example` for exact variable names. Binary geospatial wheels are preferred;
platforms without compatible wheels need native GDAL, GEOS, and PROJ build setup.

## Running Locally

```powershell
uv run --locked uvicorn app.main:app --reload
```

Without uv:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open [Swagger UI](http://127.0.0.1:8000/docs),
[ReDoc](http://127.0.0.1:8000/redoc), or [health](http://127.0.0.1:8000/health).
`GET /health` returns `{"status":"ok"}` and is a liveness check.

Use one application process. Startup holds OS-backed locks on the database and
upload directory before migration/recovery; another process sharing either resource
fails startup. Reload is supported; multiple workers are not. Relative database and
configured upload paths resolve from the working directory.

For an explicit schema upgrade, stop the server first:

```powershell
uv run --locked alembic upgrade head
```

## Running with Docker

```powershell
docker build -t geospatial-api .
docker run --rm --name geospatial-api -p 8000:8000 -v geospatial-data:/data geospatial-api
```

The runtime runs as a non-root user. The named volume stores SQLite and uploads
under `/data`; use `-p 8001:8000` if host port 8000 is occupied. Compatible Linux
wheels bundle GDAL, GEOS, and PROJ. The build requires binary dependency wheels and
fails rather than silently compiling native libraries. Docker installs the dependency
ranges in `pyproject.toml`; it does not use `uv.lock`.

The separate test target includes development dependencies and fixtures:

```powershell
docker build --target test -t geospatial-api-tests .
docker run --rm geospatial-api-tests
docker run --rm geospatial-api python -c "import geopandas, pyogrio, shapely, pyproj; print('Geospatial imports OK')"
```

## API Documentation

Examples below use `curl`; on Windows PowerShell use `curl.exe`. Replace the example
UUID with the ID returned by your upload. UUIDs, timestamps, and byte sizes below
are illustrative; the polygon measurement is from the supplied fixture.
Public responses do not expose internal filesystem paths.

### POST /api/files/

Upload multipart field `file`. A valid processed upload returns **201 Created**.

```bash
curl -F "file=@tests/fixtures/polygon.kml" http://127.0.0.1:8000/api/files/
curl -F "file=@tests/fixtures/shapefile.zip" http://127.0.0.1:8000/api/files/
```

```json
{
  "id": "4f6040d5-da4f-4fcd-96bc-4f62bc9a7097",
  "filename": "polygon.kml",
  "format": "kml",
  "size_bytes": 400,
  "status": "COMPLETED",
  "created_at": "2026-10-09T10:00:00Z",
  "completed_at": "2026-10-09T10:00:01Z",
  "discovered_feature_count": 1,
  "feature_count": 1,
  "measured_count": 1,
  "skipped_count": 0,
  "failed_count": 0,
  "source_crs": "EPSG:4326",
  "warnings": [],
  "error_code": null,
  "error_message": null
}
```

ZIP upload metadata uses `format: "zip"`. A completed file can contain failed or
unsupported features: inspect counters and individual outcomes, not status alone.

### GET /api/files/{id}/

```bash
curl http://127.0.0.1:8000/api/files/4f6040d5-da4f-4fcd-96bc-4f62bc9a7097/
```

```json
{
  "id": "4f6040d5-da4f-4fcd-96bc-4f62bc9a7097",
  "filename": "polygon.kml",
  "file_type": "kml",
  "feature_count": 1,
  "original_crs": "EPSG:4326",
  "status": "COMPLETED",
  "created_at": "2026-10-09T10:00:00Z",
  "processing_error": null
}
```

ZIP datasets use `file_type: "shapefile"`. For `FAILED` uploads,
`processing_error` contains `code` and a safe `message`.

### GET /api/files/{id}/measurements/

Returns every persisted feature in ingestion order, without reprocessing.

```bash
curl http://127.0.0.1:8000/api/files/4f6040d5-da4f-4fcd-96bc-4f62bc9a7097/measurements/
```

```json
[
  {
    "feature_index": 0,
    "geometry_type": "Polygon",
    "properties": {"name": "Polygon sample"},
    "status": "MEASURED",
    "measurement_type": "area",
    "measurement_value": 10902.05138908191,
    "measurement_unit": "square_meters",
    "source_crs": "EPSG:4326",
    "measurement_crs": "EPSG:32643",
    "projection_strategy": "auto_utm",
    "warnings": [],
    "error_code": null
  }
]
```

Other statuses are `NOT_REQUIRED` (Point), `UNSUPPORTED`, `EMPTY_GEOMETRY`,
`INVALID_GEOMETRY`, and `FAILED`. Unmeasured type/value/unit fields are `null`;
projection metadata is `null` when selection was not attempted. Existing records
without persisted results return `[]`; inspect file status separately.

### GET /api/files/{id}/analysis/

Optional enhancement: sums existing results, not spatial unions. Overlapping
polygons and repeated lines are counted independently.

```bash
curl http://127.0.0.1:8000/api/files/4f6040d5-da4f-4fcd-96bc-4f62bc9a7097/analysis/
```

```json
{
  "file_id": "4f6040d5-da4f-4fcd-96bc-4f62bc9a7097",
  "status": "COMPLETED",
  "feature_count": 1,
  "count_by_geometry_type": {"Polygon": 1},
  "total_measured_polygon_area": 10902.05138908191,
  "area_unit": "square_meters",
  "total_measured_line_length": 0.0,
  "length_unit": "meters",
  "source_crs": "EPSG:4326",
  "measurement_crs": ["EPSG:32643"],
  "projection_strategies": ["auto_utm"],
  "warnings": []
}
```

CRSs/strategies are distinct sorted lists. Multiple measurement CRSs add
`MULTIPLE_MEASUREMENT_CRSS`; failed/incomplete uploads carry status and warnings.

## CRS & Measurement Strategy

A geographic CRS, such as EPSG:4326, describes longitude/latitude in angular degrees.
A degree covers different distances at different latitudes, so direct Shapely area
or length on those coordinates is not a metric measurement. A projected CRS uses
planar coordinates, but units and distortion still need checking.

1. KML has source CRS EPSG:4326; Shapefile CRS comes from its projection metadata.
   Nonempty datasets with missing CRS fail, including point-only datasets.
2. For a supported measurable feature, transform its location/envelope to WGS84.
   Preserve a projected source only if it has meter units, covers the envelope in
   its authority area of use, and passes distortion sampling. Web Mercator,
   nonmetric, or unverified projected CRSs fall back to UTM selection.
3. Choose the standard six-degree UTM zone from the geographic envelope midpoint:
   `floor((longitude + 180) / 6) + 1`, capped at 60. Use EPSG:326xx in the north
   and EPSG:327xx in the south. An equator midpoint chooses north; +180 chooses
   zone 60. Norway/Svalbard special zone conventions are not applied.
4. Reproject a working geometry with explicit x/y ordering and strict
   best-available transformations; no ballpark datum fallback. Compute horizontal
   area/length and retain source geometry unchanged. Values are not display-rounded.

Warnings flag multi-zone or large envelopes. Width above 6 degrees or height above
10 degrees warns; above 12/20 degrees fails. A 5x5 envelope sample accepts principal
scale errors within 1% and area scale error within 2%; these are policy thresholds,
not accuracy guarantees. UTM fallback supports latitudes -80 to +84 degrees and
rejects equator crossings, antimeridian/global extents, and excessive distortion.

Z is ignored for measurement with a warning. Reprojection connects transformed
vertices without geodesic densification; transformed segments over 10 km warn with
`LONG_SEGMENTS_APPROXIMATED`. These measurements are not survey-grade guarantees.

## Error Handling

| HTTP status | Situation |
|---|---|
| 400 | Invalid request Content-Length |
| 404 | Unknown file UUID |
| 413 | Upload/request size or processing resource budget exceeded |
| 415 | Unsupported filename extension |
| 422 | Invalid filename/file/archive, missing source CRS, malformed UUID or request |
| 500 | Unexpected processing failure |
| 503 | Concurrent upload capacity exhausted |
| 507 | Upload storage quota exhausted |

For example, uploading `invalid.kml` returns 422:

```json
{
  "detail": {
    "code": "INVALID_UPLOAD",
    "message": "Unable to parse safe KML XML"
  }
}
```

Processing errors after record creation additionally include `id` and `status:
"FAILED"` in `detail`, allowing later inspection. FastAPI request-validation errors
use its standard validation envelope. Feature-level errors are stored individually;
file completion does not imply every feature was measured. Persisting a failure
state itself requires a functioning database.

Security controls include generated UUID storage directories, filename/path
validation, ZIP traversal/symlink/encryption/duplicate rejection, expansion limits,
and safe XML without DTDs or external entities. A ZIP must contain exactly one
matching `.shp`, `.shx`, and `.dbf` dataset; `.prj` is needed for nonempty dataset
processing. Other accepted sidecars are `.cpg`, `.sbn`, `.sbx`, and `.qix`.

Request bytes are bounded before multipart parsing, and Shapefile headers are
inspected before native geometry decoding. Extraction is temporary. Initial
validation failures remove stored files; startup recovers interrupted statuses,
removes UUID orphan directories, and expires old failed uploads. Completed originals
are retained. Cleanup failures are logged without replacing the original error.

## Testing

```powershell
uv run --locked pytest -q
```

Or run the Docker test target documented above. Tests use temporary databases and
storage. Coverage includes reader normalization, known geometry values, CRS zones
and hemispheres, API persistence, malformed inputs, ZIP/XML attacks, quotas,
concurrency admission, ownership locks, migrations, recovery, and cleanup.

The small fixtures are synthetic and require no third-party dataset permission.
Live API checks compared geographic measurements with independent WGS84 geodesic
references and the projected Shapefile with its known 100 x 50 m rectangle:

| Fixture | API result | Reference |
|---|---|---|
| `polygon.kml` | 10,902.0514 m² | 10,900.3587 m² geodesic area |
| `linestring.kml` | 110.8280 m | 110.8195 m geodesic distance |
| `point.kml` | `NOT_REQUIRED`, null measurement | No measurement expected |
| `shapefile.zip` | 5,000 m² | 5,000 m² planar area |
| `invalid.kml` | 422, controlled error | Malformed XML rejected |

Projection differences are small and expected, rather than assumed to be zero.
Latest verified suite: **184 passed** locally and in Linux Docker; one dependency
TestClient deprecation warning. Runtime container startup, geospatial imports, and
KML/Shapefile measurements were also verified.

## Design Decisions

| Decision | Rationale and alternative considered |
|---|---|
| Layered services | Keeps HTTP, parsing, CRS policy, and measurement independently testable; putting everything in routes would couple these concerns. |
| SQLite and two tables | Sufficient for a single-process assignment; PostGIS would add infrastructure without current spatial-query requirements. A separate measurement table would duplicate the one-outcome-per-feature relationship. |
| Synchronous processing | Simple lifecycle and immediately available results for bounded inputs; a queue/worker adds operational complexity and is deferred for large files. |
| GeoPandas/Pyogrio plus hardened KML XML | Uses native Shapefile support and explicit KML attribute handling; a GDAL-only KML reader can depend on driver availability and attribute behavior. |
| Conservative local UTM policy | Provides explainable metric results; global equal-area or geodesic calculations require a separate strategy appropriate to the measurement. |
| No automatic geometry repair | Keeps source intent visible; silent repair can alter topology and area. Invalid features have explicit outcomes. |
| Transactions plus startup recovery | Database transactions cannot atomically cover filesystem writes; exclusive ownership and reconciliation address ordinary interruptions. |

## Learning

The project illustrates why CRS selection and reprojection are separate steps, why
projected units alone do not establish accuracy, and why unknown CRS must remain
explicit. It also shows how upload security extends beyond extension checks to
archive contents, pre-parser budgets, and cleanup. Dataset completion and feature
success are distinct outcomes, and filesystem/database consistency needs recovery
as well as transactions.

## Limitations

- One application process and SQLite; no multiworker/shared-storage deployment.
- Only Polygon and LineString are measured. MultiPolygon, MultiLineString,
  GeometryCollection, and other types have unsupported outcomes.
- KML parsing supports a bounded subset, including polygon holes and MultiGeometry
  recognition. NetworkLinks, styles, external fetching, and gx tracks are not interpreted.
- Local projection policy is conservative; polar, crossing, or very large features
  may require a different strategy. No geodesic measurement or densification is implemented.
- Count/byte limits do not provide hard CPU/memory isolation for native parsers.
- Retrieval is unpaginated; there is no delete/export endpoint or object storage.
- Completed originals remain until external management; failed-upload expiry runs
  at startup, not on a background schedule.
- Authentication, rate limiting, and deployment proxy timeouts are not implemented.

## Future Scope

- PostgreSQL/PostGIS for spatial queries and greater write concurrency.
- Asynchronous large-file processing and native parser process isolation.
- Object storage with explicit retention and deletion policies.
- Additional geometry types, pagination, and result export.
- Geodesic measurement strategies, densification, and task-specific projection policies.
