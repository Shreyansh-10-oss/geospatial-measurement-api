"""Regression tests for review findings: budgets, ownership and data correctness."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pyproj import CRS
from shapely.geometry import Point, LineString
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.api.limits import UploadLimitsMiddleware
from app.config import Settings
from app.main import create_app
from app.models.feature import FeatureResult
from app.models.upload import FileStatus, UploadedFile
from app.schemas.measurement import FeatureMeasurement
from app.services import processing, reader, uploads
from app.services.crs import CRSResolution, resolve_measurement_crs
from app.services.measurements import measure_geometry
from tests.test_uploads import client, KML, archive_bytes
from tests.test_processing import MIXED_KML, rows


def test_raw_body_limit_without_content_length():
    async def scenario():
        invoked = False
        async def application(scope, receive, send):
            nonlocal invoked
            invoked = True
        middleware = UploadLimitsMiddleware(application, SimpleNamespace(max_request_bytes=12, max_concurrent_uploads=1))
        chunks = iter([{"type": "http.request", "body": b"12345678", "more_body": True},
                       {"type": "http.request", "body": b"12345678", "more_body": False}])
        async def receive():
            return next(chunks)
        messages = []
        async def send(message):
            messages.append(message)
        await middleware({"type": "http", "method": "POST", "path": "/api/files/", "headers": []}, receive, send)
        assert messages[0]["status"] == 413
        assert not invoked
        assert middleware.active_uploads == 0
    asyncio.run(scenario())


def test_concurrency_gate_before_ingestion():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def application(scope, receive, send):
            await receive()
        middleware = UploadLimitsMiddleware(application, SimpleNamespace(max_request_bytes=100, max_concurrent_uploads=1))
        async def first_receive():
            entered.set()
            await release.wait()
            return {"type": "http.request", "body": b"small", "more_body": False}
        messages = []
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "method": "POST", "path": "/api/files/", "headers": []}
        first = asyncio.create_task(middleware(scope, first_receive, send))
        await entered.wait()
        async def forbidden_receive():
            raise AssertionError("Busy uploads must be rejected before reading")
        await middleware(scope, forbidden_receive, send)
        assert messages[0]["status"] == 503
        release.set()
        await first
        assert middleware.active_uploads == 0
    asyncio.run(scenario())


def test_http_body_limit(client):
    client.app.state.settings.max_request_bytes = 32
    response = client.post("/api/files/", files={"file": ("survey.kml", KML)})
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "REQUEST_TOO_LARGE"
    with client.app.state.session_factory() as session:
        assert session.scalars(select(UploadedFile)).all() == []


def test_second_owner_cannot_recover_active_work(client):
    active_id = str(uuid4())
    with client.app.state.session_factory() as session:
        session.add(UploadedFile(id=active_id, filename="active.kml", format="kml", size_bytes=1,
                                 storage_key=f"{active_id}/source.kml", status=FileStatus.PROCESSING))
        session.commit()
    with pytest.raises(RuntimeError, match="already owned"):
        with TestClient(create_app(client.app.state.settings)):
            pass
    record, _ = rows(client, active_id)
    assert record.status == FileStatus.PROCESSING


@pytest.mark.parametrize("setting,value,body", [
    ("max_features", 1, '<Placemark/><Placemark/>'),
    ("max_coordinates", 1, '<Placemark><LineString><coordinates>77,28 77,29</coordinates></LineString></Placemark>'),
    ("max_property_bytes", 3, '<Placemark><name>oversized</name></Placemark>'),
    ("max_kml_depth", 2, '<Document><Folder><Placemark/></Folder></Document>'),
])
def test_kml_budgets(client, setting, value, body):
    setattr(client.app.state.settings, setting, value)
    data = f'<kml xmlns="http://www.opengis.net/kml/2.2">{body}</kml>'.encode()
    response = client.post("/api/files/", files={"file": ("large.kml", data)})
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "RESOURCE_LIMIT"


def test_shapefile_coordinate_limit_before_native_decode(client, tmp_path, monkeypatch):
    directory = tmp_path / "shape"
    directory.mkdir()
    gpd.GeoDataFrame(geometry=[LineString([(77, 28), (77, 28.01), (77, 28.02)])], crs=4326).to_file(directory / "line.shp", engine="pyogrio")
    data = archive_bytes({p.name: p.read_bytes() for p in directory.iterdir()})
    client.app.state.settings.max_coordinates = 2
    def forbidden(*args, **kwargs):
        raise AssertionError("Coordinate budget must be checked before native geometry decoding")
    monkeypatch.setattr(reader.gpd, "read_file", forbidden)
    response = client.post("/api/files/", files={"file": ("line.zip", data)})
    assert response.status_code == 413


def test_shapefile_is_decoded_once(client, tmp_path, monkeypatch):
    directory = tmp_path / "shape"
    directory.mkdir()
    gpd.GeoDataFrame(geometry=[Point(77, 28)], crs=4326).to_file(directory / "point.shp", engine="pyogrio")
    data = archive_bytes({p.name: p.read_bytes() for p in directory.iterdir()})
    original, calls = reader.gpd.read_file, []
    def track(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(reader.gpd, "read_file", track)
    response = client.post("/api/files/", files={"file": ("point.zip", data)})
    assert response.status_code == 201
    assert len(calls) == 1


def test_invalid_geographic_point(client):
    data = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Point><coordinates>200,100</coordinates></Point></Placemark></kml>'
    response = client.post("/api/files/", files={"file": ("bad-point.kml", data)})
    assert response.status_code == 201
    record, features = rows(client, response.json()["id"])
    assert record.failed_count == 1
    assert features[0].status == "INVALID_GEOMETRY"
    assert "INVALID_GEOGRAPHIC_COORDINATES" in features[0].warnings


def test_measurement_service_validates_point_with_known_crs():
    resolution = CRSResolution(CRS.from_epsg(4326), CRS.from_epsg(32643), "auto_utm")
    assert measure_geometry(Point(200, 100), resolution).error_code == "INVALID_GEOGRAPHIC_COORDINATES"


def test_unclosed_kml_ring_not_repaired(client):
    data = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>77,28 77.001,28 77.001,28.001 77,28.001</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>'
    response = client.post("/api/files/", files={"file": ("ring.kml", data)})
    _, features = rows(client, response.json()["id"])
    assert features[0].status == "INVALID_GEOMETRY"
    assert features[0].value is None
    assert "INVALID_GEOMETRY_COORDINATES" in features[0].warnings


def test_mixed_unsupported_kml_collection_preserved(client):
    data = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><MultiGeometry><Point><coordinates>77,28</coordinates></Point><Model/></MultiGeometry></Placemark></kml>'
    response = client.post("/api/files/", files={"file": ("mixed.kml", data)})
    _, features = rows(client, response.json()["id"])
    assert features[0].status == "UNSUPPORTED"
    assert features[0].geometry["format"] == "kml"
    assert "Model" in features[0].geometry["xml"]
    assert "Point" in features[0].geometry["xml"]


def test_long_reprojected_segments_warn():
    geometry = LineString([(75, 28), (75, 28.2)])
    result = measure_geometry(geometry, resolve_measurement_crs(geometry, 4326))
    assert result.value > 10000
    assert "LONG_SEGMENTS_APPROXIMATED" in result.warnings


def test_discovered_metadata_survives_measurement_failure(client, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("measurement failure")
    monkeypatch.setattr(processing, "measure_geometry", fail)
    response = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)})
    assert response.status_code == 500
    record, features = rows(client, response.json()["detail"]["id"])
    assert record.source_crs == "EPSG:4326"
    assert record.discovered_feature_count == 4
    assert record.feature_count == 0
    assert features == []


def test_database_rejects_inconsistent_measurement(client):
    uploaded = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)}).json()["id"]
    with client.app.state.session_factory() as session:
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE feature_results SET unit=NULL WHERE file_id=:id AND status='MEASURED'"), {"id": uploaded})
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE feature_results SET value=-1 WHERE file_id=:id AND status='MEASURED'"), {"id": uploaded})
            session.commit()
        session.rollback()
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0003"


def test_schema_rejects_inconsistent_measurement():
    data = dict(feature_index=0, geometry_type="Polygon", properties={}, status="MEASURED",
                measurement_type="area", value=-1, unit="meters", source_crs="EPSG:4326",
                measurement_crs="EPSG:32643", projection_strategy="auto_utm", warnings=[], error_code=None)
    with pytest.raises(ValidationError):
        FeatureMeasurement.model_validate(data)


def test_storage_quota(client):
    client.app.state.settings.max_storage_bytes = 10
    response = client.post("/api/files/", files={"file": ("survey.kml", KML)})
    assert response.status_code == 507
    assert response.json()["detail"]["code"] == "STORAGE_FULL"


def test_cleanup_error_does_not_mask_original_failure(client, monkeypatch):
    def fail_cleanup(*args, **kwargs):
        raise OSError("cleanup failed")
    monkeypatch.setattr(uploads.shutil, "rmtree", fail_cleanup)
    response = client.post("/api/files/", files={"file": ("bad.kml", b"bad XML")})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_UPLOAD"


def test_recovery_cleans_orphans_pending_and_expired_failed(tmp_path):
    from app.db.session import create_database_engine, initialize_database
    from sqlalchemy.orm import Session
    root = tmp_path / "uploads"
    root.mkdir()
    settings = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}", uploads_dir=root)
    pending, expired, orphan = [str(uuid4()) for _ in range(3)]
    for upload_id in (pending, expired, orphan):
        directory = root / upload_id
        directory.mkdir()
        (directory / "source.kml").write_bytes(KML)
        (directory / "extracted").mkdir()
    unrelated = root / "do-not-touch"
    unrelated.mkdir()
    engine = create_database_engine(settings.database_url)
    initialize_database(engine)
    with Session(engine) as session:
        session.add(UploadedFile(id=pending, filename="pending.kml", format="kml", size_bytes=len(KML), storage_key=f"{pending}/source.kml", status=FileStatus.PENDING))
        session.add(UploadedFile(id=expired, filename="expired.kml", format="kml", size_bytes=len(KML), storage_key=f"{expired}/source.kml", status=FileStatus.FAILED, completed_at=datetime.now(timezone.utc) - timedelta(days=31)))
        session.commit()
    engine.dispose()
    with TestClient(create_app(settings)) as test_client:
        record, _ = rows(test_client, pending)
        assert record.status == FileStatus.FAILED
        assert (root / pending / "source.kml").is_file()
        assert not (root / pending / "extracted").exists()
        assert not (root / expired).exists()
        assert not (root / orphan).exists()
        assert unrelated.is_dir()
    # Ownership is released normally and a new instance can start.
    with TestClient(create_app(settings)):
        pass


def test_post_timestamps_are_utc(client):
    response = client.post("/api/files/", files={"file": ("survey.kml", KML)})
    for field in ("created_at", "completed_at"):
        assert datetime.fromisoformat(response.json()[field]).utcoffset().total_seconds() == 0


def test_measurement_query_omits_geometry_column(client):
    from sqlalchemy import event
    uploaded = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)}).json()["id"]
    statements = []
    def track(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(client.app.state.engine, "before_cursor_execute", track)
    try:
        assert client.get(f"/api/files/{uploaded}/measurements/").status_code == 200
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", track)
    feature_queries = [sql for sql in statements if "FROM feature_results" in sql]
    assert feature_queries
    assert all("feature_results.geometry," not in sql for sql in feature_queries)


def test_adopt_processed_schema_preserves_features_and_foreign_keys(tmp_path):
    from app.db.base import Base
    from app.db.session import create_database_engine, initialize_database
    from sqlalchemy.orm import Session
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'processed.db').as_posix()}")
    # An unversioned schema from the earlier implementation has no alembic_version.
    Base.metadata.create_all(engine)
    upload_id = str(uuid4())
    with Session(engine) as session:
        session.add(UploadedFile(id=upload_id, filename="old.kml", format="kml", size_bytes=1,
                                 storage_key=f"{upload_id}/source.kml", status=FileStatus.COMPLETED,
                                 feature_count=1, measured_count=1))
        session.flush()
        session.add(FeatureResult(file_id=upload_id, feature_index=0, geometry_type="Polygon",
                                  geometry={"type": "Polygon", "coordinates": []}, properties={"name": "retained"},
                                  source_crs="EPSG:4326", measurement_crs="EPSG:32643", projection_strategy="auto_utm",
                                  status="MEASURED", measurement_type="area", value=5000, unit="square_meters", warnings=[]))
        session.commit()
    initialize_database(engine)
    initialize_database(engine)
    with Session(engine) as session:
        assert session.get(UploadedFile, upload_id).measured_count == 1
        assert session.get(UploadedFile, upload_id).discovered_feature_count == 1
        feature = session.scalar(select(FeatureResult).where(FeatureResult.file_id == upload_id))
        assert feature.properties == {"name": "retained"}
        assert feature.value == 5000
        assert session.scalar(text("PRAGMA foreign_keys")) == 1
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_existing_database_lock_is_shared_across_different_storage_roots(client, tmp_path):
    settings = client.app.state.settings.model_copy(update={"uploads_dir": tmp_path / "other-storage"})
    with pytest.raises(RuntimeError, match="already owned"):
        with TestClient(create_app(settings)):
            pass


def test_upload_capacity_is_released_after_application_failure():
    async def scenario():
        async def application(scope, receive, send):
            raise RuntimeError("application failure")
        middleware = UploadLimitsMiddleware(application, SimpleNamespace(max_request_bytes=100, max_concurrent_uploads=1))
        async def receive():
            return {"type": "http.request", "body": b"small", "more_body": False}
        async def send(message):
            pass
        with pytest.raises(RuntimeError, match="application failure"):
            await middleware({"type": "http", "method": "POST", "path": "/api/files/", "headers": []}, receive, send)
        assert middleware.active_uploads == 0
    asyncio.run(scenario())


def test_shapefile_attribute_width_bounded_before_decode(client, tmp_path, monkeypatch):
    directory = tmp_path / "wide"
    directory.mkdir()
    gpd.GeoDataFrame({f"field{i}": ["value"] for i in range(5)}, geometry=[Point(77, 28)], crs=4326).to_file(directory / "wide.shp", engine="pyogrio")
    data = archive_bytes({p.name: p.read_bytes() for p in directory.iterdir()})
    client.app.state.settings.max_attribute_fields = 2
    def forbidden(*args, **kwargs):
        raise AssertionError("Attribute width must be bounded before native decoding")
    monkeypatch.setattr(reader.gpd, "read_file", forbidden)
    assert client.post("/api/files/", files={"file": ("wide.zip", data)}).status_code == 413
