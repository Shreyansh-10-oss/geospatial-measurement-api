"""End-to-end upload processing and durable failure transitions."""
import io
import zipfile

import pytest
from sqlalchemy import select, create_engine, text
from sqlalchemy.orm import Session

from app.models.feature import FeatureResult
from app.models.upload import UploadedFile, FileStatus
from app.services import processing
from app.services.reader import ReaderError
from app.db.session import initialize_database
from tests.test_uploads import client, shapefile_zip, archive_bytes

MIXED_KML = b'''<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark id="parcel"><name>Parcel</name><Polygon><outerBoundaryIs><LinearRing><coordinates>77,28 77.001,28 77.001,28.001 77,28.001 77,28</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><LineString><coordinates>77,28 77,28.001</coordinates></LineString></Placemark>
<Placemark><Point><coordinates>77,28</coordinates></Point></Placemark>
<Placemark><MultiGeometry><Point><coordinates>77,28</coordinates></Point></MultiGeometry></Placemark>
</Document></kml>'''


def rows(client, upload_id):
    with client.app.state.session_factory() as session:
        return session.get(UploadedFile, upload_id), session.scalars(
            select(FeatureResult).where(FeatureResult.file_id == upload_id).order_by(FeatureResult.feature_index)
        ).all()


def test_full_kml_pipeline(client):
    response = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "COMPLETED"
    assert (data["feature_count"], data["measured_count"], data["skipped_count"], data["failed_count"]) == (4, 2, 2, 0)
    record, features = rows(client, data["id"])
    assert record.completed_at is not None
    assert [f.status for f in features] == ["MEASURED", "MEASURED", "NOT_REQUIRED", "UNSUPPORTED"]
    assert features[0].source_feature_id == "parcel"
    assert features[0].properties["name"] == "Parcel"
    assert features[0].geometry["coordinates"][0][0] == [77, 28]
    assert features[0].value > 10000
    assert features[0].unit == "square_meters"
    assert features[1].value == pytest.approx(110.82, rel=0.01)
    assert features[1].unit == "meters"
    assert features[0].source_crs == "EPSG:4326"
    assert features[0].measurement_crs == "EPSG:32643"
    assert features[0].projection_strategy == "auto_utm"


def test_zip_pipeline(client, shapefile_zip):
    response = client.post("/api/files/", files={"file": ("survey.zip", shapefile_zip)})
    assert response.status_code == 201
    record, features = rows(client, response.json()["id"])
    assert record.status == FileStatus.COMPLETED
    assert record.feature_count == 1
    assert features[0].status == "NOT_REQUIRED"
    assert features[0].properties["name"] == "sample"


def test_processing_visible_before_read(client, monkeypatch):
    original = processing.read_geospatial_file
    def inspect_status(path, **kwargs):
        with client.app.state.session_factory() as session:
            assert session.scalar(select(UploadedFile.status)) == FileStatus.PROCESSING
        return original(path, **kwargs)
    monkeypatch.setattr(processing, "read_geospatial_file", inspect_status)
    assert client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)}).status_code == 201


@pytest.mark.parametrize("known,expected_status", [(True, 422), (False, 500)])
def test_file_failure_persisted_safely(client, monkeypatch, known, expected_status):
    def fail(path, **kwargs):
        if known:
            raise ReaderError("PARSE_ERROR", "SECRET C:/private/input.kml")
        raise RuntimeError("SECRET C:/private/input.kml")
    monkeypatch.setattr(processing, "read_geospatial_file", fail)
    response = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)})
    assert response.status_code == expected_status
    assert "SECRET" not in response.text
    record, features = rows(client, response.json()["detail"]["id"])
    assert record.status == FileStatus.FAILED
    assert record.error_message and "SECRET" not in record.error_message
    assert features == []
    assert (client.app.state.settings.uploads_dir / record.id / "source.kml").is_file()


def test_missing_crs_fails_file(client, shapefile_zip):
    with zipfile.ZipFile(io.BytesIO(shapefile_zip)) as archive:
        data = archive_bytes({name: archive.read(name) for name in archive.namelist() if not name.endswith(".prj")})
    response = client.post("/api/files/", files={"file": ("missing-crs.zip", data)})
    assert response.status_code == 422
    record, features = rows(client, response.json()["detail"]["id"])
    assert record.status == FileStatus.FAILED
    assert record.error_code == "MISSING_CRS"
    assert features == []


def test_feature_crs_failure_keeps_other_results(client):
    data = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString><coordinates>3,85 3.01,85</coordinates></LineString></Placemark><Placemark><Point><coordinates>77,28</coordinates></Point></Placemark></kml>'
    response = client.post("/api/files/", files={"file": ("polar.kml", data)})
    assert response.status_code == 201
    record, features = rows(client, response.json()["id"])
    assert record.status == FileStatus.COMPLETED
    assert record.failed_count == 1
    assert "FEATURE_FAILURES" in record.warnings
    assert features[0].error_code == "UTM_LATITUDE_UNSUPPORTED"
    assert features[1].status == "NOT_REQUIRED"


def test_empty_dataset_completes(client):
    response = client.post("/api/files/", files={"file": ("empty.kml", b'<kml xmlns="http://www.opengis.net/kml/2.2"/>')})
    assert response.status_code == 201
    assert response.json()["feature_count"] == 0
    assert response.json()["warnings"] == ["EMPTY_DATASET"]


def test_final_commit_failure_rolls_back_features_and_marks_failed(client, monkeypatch):
    original = Session.commit
    def fail_final(self):
        if any(isinstance(value, FeatureResult) for value in self.new):
            self.flush()
            raise RuntimeError("simulated final commit failure")
        return original(self)
    monkeypatch.setattr(Session, "commit", fail_final)
    response = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)})
    assert response.status_code == 500
    record, features = rows(client, response.json()["detail"]["id"])
    assert record.status == FileStatus.FAILED
    assert record.feature_count == 0
    assert features == []


def test_upgrade_existing_upload_schema(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE uploaded_files (id VARCHAR(36) PRIMARY KEY, filename VARCHAR(255), format VARCHAR(10), size_bytes INTEGER, storage_key VARCHAR(100), status VARCHAR(10), created_at DATETIME)")
        connection.exec_driver_sql("INSERT INTO uploaded_files VALUES ('old','old.kml','kml',1,'old/source.kml','PENDING','2026-10-08 00:00:00')")
    initialize_database(engine)
    initialize_database(engine)
    with Session(engine) as session:
        record = session.get(UploadedFile, "old")
        assert record.filename == "old.kml"
        assert record.feature_count == 0
        assert record.warnings == []
    engine.dispose()


def test_projected_shapefile_area_end_to_end(client, tmp_path):
    import geopandas as gpd
    from shapely.geometry import box
    directory = tmp_path / "rectangle"
    directory.mkdir()
    gpd.GeoDataFrame({"name": ["rectangle"]}, geometry=[box(500000, 3000000, 500100, 3000050)], crs=32643).to_file(directory / "rectangle.shp", engine="pyogrio")
    data = archive_bytes({path.name: path.read_bytes() for path in directory.iterdir()})
    response = client.post("/api/files/", files={"file": ("rectangle.zip", data)})
    assert response.status_code == 201
    record, features = rows(client, response.json()["id"])
    assert record.measured_count == 1
    assert features[0].value == pytest.approx(5000, abs=1e-6)
    assert features[0].projection_strategy == "preserve_projected"


def test_malformed_feature_is_persisted_without_aborting(client):
    data = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Point><coordinates>bad,28</coordinates></Point></Placemark><Placemark><Point><coordinates>77,28</coordinates></Point></Placemark></kml>'
    response = client.post("/api/files/", files={"file": ("mixed.kml", data)})
    assert response.status_code == 201
    record, features = rows(client, response.json()["id"])
    assert record.failed_count == 1
    assert features[0].error_code == "GEOMETRY_PARSE_ERROR"
    assert features[1].status == "NOT_REQUIRED"


def test_interrupted_processing_recovered_on_startup(tmp_path):
    from app.config import Settings
    from app.main import create_app
    from fastapi.testclient import TestClient
    settings = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}", uploads_dir=tmp_path / "uploads")
    engine = create_engine(settings.database_url)
    initialize_database(engine)
    with Session(engine) as session:
        session.add(UploadedFile(id="interrupted", filename="old.kml", format="kml", size_bytes=1, storage_key="interrupted/source.kml", status=FileStatus.PROCESSING))
        session.commit()
    with TestClient(create_app(settings)) as test_client:
        record, features = rows(test_client, "interrupted")
        assert record.status == FileStatus.FAILED
        assert record.error_code == "PROCESSING_INTERRUPTED"
    engine.dispose()
