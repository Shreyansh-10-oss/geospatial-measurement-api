"""Exercise reusable sample files through the complete upload pipeline."""
from pathlib import Path

import pytest

from tests.test_uploads import client

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("filename,geometry_type,kind", [
    ("polygon.kml", "Polygon", "area"),
    ("linestring.kml", "LineString", "length"),
    ("point.kml", "Point", None),
    ("shapefile.zip", "Polygon", "area"),
])
def test_valid_fixture(client, filename, geometry_type, kind):
    response = client.post("/api/files/", files={"file": (filename, (FIXTURES / filename).read_bytes())})
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["feature_count"] == 1
    measurements = client.get(f"/api/files/{response.json()['id']}/measurements/")
    assert measurements.status_code == 200
    feature, = measurements.json()
    assert feature["geometry_type"] == geometry_type
    assert feature["measurement_type"] == kind
    if kind is None:
        assert feature["measurement_value"] is None
    else:
        assert feature["measurement_value"] > 0
    if filename == "shapefile.zip":
        assert feature["measurement_value"] == pytest.approx(5000, abs=0.01)
        assert feature["source_crs"] == "EPSG:32643"


def test_invalid_fixture(client):
    response = client.post("/api/files/", files={"file": ("invalid.kml", (FIXTURES / "invalid.kml").read_bytes())})
    assert response.status_code == 422
