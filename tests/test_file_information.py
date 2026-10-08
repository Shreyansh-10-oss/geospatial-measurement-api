"""File metadata retrieval integration tests."""
from datetime import datetime
from uuid import uuid4

import pytest
from app.models.upload import FileStatus
from app.services import processing
from tests.test_uploads import client, shapefile_zip, KML
from tests.test_processing import rows


def test_get_completed_file(client):
    upload = client.post("/api/files/", files={"file": ("survey.kml", KML)}).json()
    response = client.get(f"/api/files/{upload['id']}/")
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"id", "filename", "file_type", "feature_count", "original_crs", "status", "created_at", "processing_error"}
    assert result["id"] == upload["id"]
    assert result["filename"] == "survey.kml"
    assert result["file_type"] == "kml"
    assert result["feature_count"] == 1
    assert result["original_crs"] == "EPSG:4326"
    assert result["status"] == "COMPLETED"
    assert result["processing_error"] is None
    assert datetime.fromisoformat(result["created_at"]).utcoffset().total_seconds() == 0
    assert "storage_key" not in result


def test_get_shapefile(client, shapefile_zip):
    upload = client.post("/api/files/", files={"file": ("survey.zip", shapefile_zip)}).json()
    response = client.get(f"/api/files/{upload['id']}/")
    assert response.status_code == 200
    assert response.json()["file_type"] == "shapefile"


def test_unknown_id(client):
    response = client.get(f"/api/files/{uuid4()}/")
    assert response.status_code == 404
    assert response.json() == {"detail": "Uploaded file not found"}


def test_malformed_uuid(client):
    assert client.get("/api/files/not-a-uuid/").status_code == 422


def test_failed_upload_information(client, monkeypatch):
    def fail(path, **kwargs):
        raise RuntimeError("SECRET internal path")
    monkeypatch.setattr(processing, "read_geospatial_file", fail)
    upload = client.post("/api/files/", files={"file": ("survey.kml", KML)})
    upload_id = upload.json()["detail"]["id"]
    response = client.get(f"/api/files/{upload_id}/")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "FAILED"
    assert result["processing_error"]["code"] == "PROCESSING_FAILED"
    assert result["processing_error"]["message"]
    assert "SECRET" not in response.text


@pytest.mark.parametrize("status", [FileStatus.PENDING, FileStatus.PROCESSING])
def test_intermediate_status_information(client, status):
    upload = client.post("/api/files/", files={"file": ("survey.kml", KML)}).json()
    from app.models.upload import UploadedFile
    with client.app.state.session_factory() as session:
        record = session.get(UploadedFile, upload["id"])
        record.status = status
        record.source_crs = None
        record.error_message = "stale error"
        session.commit()
    response = client.get(f"/api/files/{upload['id']}/")
    assert response.status_code == 200
    assert response.json()["status"] == status.value
    assert response.json()["original_crs"] is None
    assert response.json()["processing_error"] is None


def test_retrieval_does_not_process_file(client, monkeypatch):
    upload = client.post("/api/files/", files={"file": ("survey.kml", KML)}).json()
    def fail(path, **kwargs):
        raise AssertionError("GET must not read files")
    monkeypatch.setattr(processing, "read_geospatial_file", fail)
    assert client.get(f"/api/files/{upload['id']}/").status_code == 200
