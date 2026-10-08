"""Optional analysis endpoint aggregation and no-reprocessing tests."""
from uuid import uuid4

import pytest
from sqlalchemy import select
from app.models.feature import FeatureResult
from app.services import processing
from tests.test_uploads import client
from tests.test_processing import MIXED_KML


def test_mixed_dataset_analysis(client):
    uploaded = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)}).json()["id"]
    response = client.get(f"/api/files/{uploaded}/analysis/")
    assert response.status_code == 200
    analysis = response.json()
    assert analysis["feature_count"] == 4
    assert analysis["count_by_geometry_type"] == {"Polygon": 1, "LineString": 1, "Point": 1, "GeometryCollection": 1}
    assert analysis["source_crs"] == "EPSG:4326"
    assert analysis["measurement_crs"] == ["EPSG:32643"]
    assert analysis["projection_strategies"] == ["auto_utm"]
    assert analysis["area_unit"] == "square_meters"
    assert analysis["length_unit"] == "meters"
    features = client.get(f"/api/files/{uploaded}/measurements/").json()
    assert analysis["total_measured_polygon_area"] == pytest.approx(features[0]["measurement_value"])
    assert analysis["total_measured_line_length"] == pytest.approx(features[1]["measurement_value"])
    assert "UNSUPPORTED_GEOMETRY_TYPE" in analysis["warnings"]
    assert "storage_key" not in response.text
    assert str(client.app.state.settings.uploads_dir) not in response.text


def test_unknown_file(client):
    assert client.get(f"/api/files/{uuid4()}/analysis/").status_code == 404


def test_empty_dataset(client):
    uploaded = client.post("/api/files/", files={"file": ("empty.kml", b'<kml xmlns="http://www.opengis.net/kml/2.2"/>')}).json()["id"]
    analysis = client.get(f"/api/files/{uploaded}/analysis/").json()
    assert analysis["feature_count"] == 0
    assert analysis["count_by_geometry_type"] == {}
    assert analysis["total_measured_polygon_area"] == 0
    assert analysis["total_measured_line_length"] == 0
    assert analysis["measurement_crs"] == []
    assert analysis["warnings"] == ["EMPTY_DATASET"]


def test_no_reprocessing_and_dataset_isolation(client, monkeypatch):
    uploaded = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)}).json()["id"]
    client.post("/api/files/", files={"file": ("another.kml", MIXED_KML)})
    def fail(*args):
        raise AssertionError("Analysis must only read persisted data")
    monkeypatch.setattr(processing, "read_geospatial_file", fail)
    monkeypatch.setattr(processing, "resolve_measurement_crs", fail)
    monkeypatch.setattr(processing, "measure_geometry", fail)
    response = client.get(f"/api/files/{uploaded}/analysis/")
    assert response.status_code == 200
    assert response.json()["feature_count"] == 4


def test_multiple_crs_and_deduplicated_warnings(client):
    data = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString><coordinates>77,28 77,28.001</coordinates></LineString></Placemark><Placemark><LineString><coordinates>151,-34 151,-34.001</coordinates></LineString></Placemark></kml>'
    uploaded = client.post("/api/files/", files={"file": ("regions.kml", data)}).json()["id"]
    analysis = client.get(f"/api/files/{uploaded}/analysis/").json()
    assert analysis["measurement_crs"] == ["EPSG:32643", "EPSG:32756"]
    assert analysis["projection_strategies"] == ["auto_utm"]
    assert "MULTIPLE_MEASUREMENT_CRSS" in analysis["warnings"]
    assert analysis["warnings"] == sorted(set(analysis["warnings"]))
    assert analysis["total_measured_line_length"] > 200


def test_nonmeasured_results_excluded_from_totals(client):
    uploaded = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)}).json()["id"]
    with client.app.state.session_factory() as session:
        polygon = session.scalar(select(FeatureResult).where(FeatureResult.file_id == uploaded, FeatureResult.feature_index == 0))
        polygon.status = "FAILED"
        polygon.value = None
        polygon.measurement_type = None
        polygon.unit = None
        session.commit()
    analysis = client.get(f"/api/files/{uploaded}/analysis/").json()
    assert analysis["feature_count"] == 4
    assert analysis["total_measured_polygon_area"] == 0
    assert analysis["total_measured_line_length"] > 0


def test_failed_file_returns_explicit_status(client, monkeypatch):
    def fail(path, **kwargs):
        raise RuntimeError("internal failure")
    monkeypatch.setattr(processing, "read_geospatial_file", fail)
    uploaded = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)}).json()["detail"]["id"]
    analysis = client.get(f"/api/files/{uploaded}/analysis/").json()
    assert analysis["status"] == "FAILED"
    assert analysis["feature_count"] == 0
    assert "FILE_FAILED" in analysis["warnings"]
