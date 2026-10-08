"""Verify API contract and actual SQLite startup initialization."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from app.config import Settings
from app.main import create_app


def test_health_and_database_startup(tmp_path):
    database_path = tmp_path / "test.db"
    uploads_path = tmp_path / "uploads"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{database_path.as_posix()}",
        uploads_dir=uploads_path,
    )
    application = create_app(settings)
    with TestClient(application) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert database_path.is_file()
        assert uploads_path.is_dir()
        with application.state.session_factory() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
            assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        schema = client.get("/openapi.json").json()
        assert "/health" in schema["paths"]


def test_settings_use_environment(monkeypatch):
    monkeypatch.setenv("GEOSPATIAL_APP_NAME", "Test API")
    assert Settings(_env_file=None).app_name == "Test API"


def test_non_sqlite_url_is_rejected():
    with pytest.raises(ValidationError, match="SQLite"):
        Settings(_env_file=None, database_url="postgresql://localhost/example")
