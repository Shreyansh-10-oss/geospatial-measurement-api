"""Persisted measurement retrieval integration tests."""
from uuid import uuid4
import pytest
from app.services import processing
from tests.test_uploads import client
from tests.test_processing import MIXED_KML


@pytest.fixture
def uploaded(client):
    response = client.post("/api/files/", files={"file": ("mixed.kml", MIXED_KML)})
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.parametrize("index,geometry_type,status,kind,unit", [
    (0, "Polygon", "MEASURED", "area", "square_meters"),
    (1, "LineString", "MEASURED", "length", "meters"),
    (2, "Point", "NOT_REQUIRED", None, None),
    (3, "GeometryCollection", "UNSUPPORTED", None, None),
])
def test_feature_types(client, uploaded, index, geometry_type, status, kind, unit):
    response = client.get(f"/api/files/{uploaded}/measurements/")
    assert response.status_code == 200
    features = response.json()
    assert len(features) == 4
    feature = features[index]
    assert feature["feature_index"] == index
    assert feature["geometry_type"] == geometry_type
    assert feature["status"] == status
    assert feature["measurement_type"] == kind
    assert feature["measurement_unit"] == unit
    assert feature["source_crs"] == "EPSG:4326"
    assert isinstance(feature["properties"], dict)
    assert isinstance(feature["warnings"], list)
    if status == "MEASURED":
        assert feature["measurement_value"] > 0
        assert feature["measurement_crs"] == "EPSG:32643"
        assert feature["projection_strategy"] == "auto_utm"
    else:
        assert feature["measurement_value"] is None
        assert feature["measurement_crs"] is None
        assert feature["projection_strategy"] is None
    if index == 0:
        assert feature["properties"] == {"name": "Parcel"}
    if index == 3:
        assert "UNSUPPORTED_GEOMETRY_TYPE" in feature["warnings"]


def test_unknown_file(client):
    response = client.get(f"/api/files/{uuid4()}/measurements/")
    assert response.status_code == 404
    assert response.json() == {"detail": "Uploaded file not found"}


def test_malformed_id(client):
    assert client.get("/api/files/invalid/measurements/").status_code == 422


def test_only_public_fields_and_stable_order(client, uploaded):
    response = client.get(f"/api/files/{uploaded}/measurements/")
    features = response.json()
    assert [f["feature_index"] for f in features] == [0, 1, 2, 3]
    public_fields = {"feature_index", "geometry_type", "properties", "status", "measurement_type", "measurement_value", "measurement_unit", "source_crs", "measurement_crs", "projection_strategy", "warnings", "error_code"}
    assert all(set(feature) == public_fields for feature in features)
    assert str(client.app.state.settings.uploads_dir) not in response.text
    assert "storage_key" not in response.text


def test_does_not_reprocess_and_is_scoped_to_file(client, uploaded, monkeypatch):
    other = client.post("/api/files/", files={"file": ("empty.kml", b'<kml xmlns="http://www.opengis.net/kml/2.2"/>')}).json()["id"]
    def fail(*args):
        raise AssertionError("GET must not process files")
    monkeypatch.setattr(processing, "read_geospatial_file", fail)
    monkeypatch.setattr(processing, "measure_geometry", fail)
    assert len(client.get(f"/api/files/{uploaded}/measurements/").json()) == 4
    assert client.get(f"/api/files/{other}/measurements/").json() == []


def test_feature_failure_is_exposed(client):
    data = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><LineString><coordinates>3,85 3.01,85</coordinates></LineString></Placemark></kml>'
    uploaded = client.post("/api/files/", files={"file": ("polar.kml", data)}).json()["id"]
    feature = client.get(f"/api/files/{uploaded}/measurements/").json()[0]
    assert feature["status"] == "FAILED"
    assert feature["error_code"] == "UTM_LATITUDE_UNSUPPORTED"
    assert "OUTSIDE_UTM_LATITUDE_RANGE" in feature["warnings"]
    assert feature["measurement_value"] is None
