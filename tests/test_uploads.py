"""Upload acceptance, persistence, and hostile archive regression tests."""
import io
import zipfile
from uuid import UUID

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models.upload import FileStatus, UploadedFile

KML = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Point><coordinates>77,28</coordinates></Point></Placemark></kml>'


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}", uploads_dir=tmp_path / "uploads")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def archive_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def shapefile_zip(tmp_path):
    directory = tmp_path / "fixture"
    directory.mkdir()
    gpd.GeoDataFrame({"name": ["sample"]}, geometry=[Point(77, 28)], crs="EPSG:4326").to_file(directory / "survey.shp", engine="pyogrio")
    return archive_bytes({f"nested/{p.name}": p.read_bytes() for p in directory.iterdir()})


def assert_no_uploads(client):
    assert [path for path in client.app.state.settings.uploads_dir.iterdir() if path.name != ".owner.lock"] == []
    with client.app.state.session_factory() as session:
        assert session.scalars(select(UploadedFile)).all() == []


def test_valid_kml(client):
    response = client.post("/api/files/", files={"file": ("survey.KML", KML)})
    assert response.status_code == 201, response.text
    result = response.json()
    UUID(result["id"])
    assert result["status"] == "COMPLETED"
    assert result["size_bytes"] == len(KML)
    directory = client.app.state.settings.uploads_dir / result["id"]
    assert (directory / "source.kml").read_bytes() == KML
    with client.app.state.session_factory() as session:
        record = session.get(UploadedFile, result["id"])
        assert record.filename == "survey.KML"
        assert record.status == FileStatus.COMPLETED
    second = client.post("/api/files/", files={"file": ("survey.KML", KML)})
    assert second.json()["id"] != result["id"]


def test_valid_zip(client, shapefile_zip):
    response = client.post("/api/files/", files={"file": ("survey.zip", shapefile_zip)})
    assert response.status_code == 201, response.text
    directory = client.app.state.settings.uploads_dir / response.json()["id"]
    assert (directory / "source.zip").is_file()
    assert not (directory / "extracted").exists()


@pytest.mark.parametrize("filename,data,status", [
    ("data.txt", b"text", 415),
    ("survey.zip", b"not a ZIP", 422),
    ("survey.zip", archive_bytes({"survey.shp": b"fake"}), 422),
    ("survey.kml", b"bad XML", 422),
    ("empty.kml", b"", 422),
    ("../survey.kml", KML, 422),
    ("CON.kml", KML, 422),
    ("survey.zip", archive_bytes({"CON.shp": b"bad"}), 422),
    ("survey.zip", archive_bytes({"../escape.shp": b"bad"}), 422),
    ("survey.zip", archive_bytes({"C:/escape.shp": b"bad"}), 422),
    ("survey.zip", archive_bytes({"folder\\escape.shp": b"bad"}), 422),
    ("survey.zip", archive_bytes({"survey.shp": b"fake", "survey.shx": b"fake", "survey.dbf": b"fake"}), 422),
    ("survey.kml", b'<!DOCTYPE kml [<!ENTITY x "unsafe">]><kml xmlns="http://www.opengis.net/kml/2.2">&x;</kml>', 422),
])
def test_rejected_uploads_leave_no_files_or_rows(client, filename, data, status):
    response = client.post("/api/files/", files={"file": (filename, data)})
    assert response.status_code == status, response.text
    assert_no_uploads(client)


def test_upload_limit(client):
    client.app.state.settings.max_upload_bytes = 10
    response = client.post("/api/files/", files={"file": ("survey.kml", KML)})
    assert response.status_code == 413
    assert_no_uploads(client)


def test_expanded_zip_limit(client, shapefile_zip):
    client.app.state.settings.max_zip_uncompressed_bytes = 10
    response = client.post("/api/files/", files={"file": ("survey.zip", shapefile_zip)})
    assert response.status_code == 413
    assert_no_uploads(client)


def test_all_statuses_persist(client):
    with client.app.state.session_factory() as session:
        for index, status in enumerate(FileStatus):
            session.add(UploadedFile(id=str(index), filename="test.kml", format="kml", size_bytes=1, storage_key=f"{index}/source.kml", status=status))
        session.commit()
        assert set(session.scalars(select(UploadedFile.status))) == set(FileStatus)


def test_symlink_archive_rejected(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entry = zipfile.ZipInfo("survey.shp")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, "../outside")
    response = client.post("/api/files/", files={"file": ("survey.zip", buffer.getvalue())})
    assert response.status_code == 422
    assert_no_uploads(client)


def test_zip_entry_limit(client, shapefile_zip):
    client.app.state.settings.max_zip_entries = 1
    response = client.post("/api/files/", files={"file": ("survey.zip", shapefile_zip)})
    assert response.status_code == 422
    assert_no_uploads(client)


def test_database_failure_cleans_storage(client, monkeypatch):
    from sqlalchemy.orm import Session

    def fail_commit(self):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="simulated database failure"):
        client.post("/api/files/", files={"file": ("survey.kml", KML)})
    assert_no_uploads(client)
