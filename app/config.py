"""Application settings loaded from environment variables or .env."""
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GEOSPATIAL_", env_file=PROJECT_ROOT / ".env", extra="ignore"
    )

    app_name: str = "Geospatial File Measurement API"
    database_url: str = "sqlite:///./geospatial.db"
    uploads_dir: Path = PROJECT_ROOT / "uploads"

    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_zip_uncompressed_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_zip_entries: int = Field(default=100, gt=0)
    max_zip_compression_ratio: int = Field(default=200, gt=0)

    max_request_bytes: int = Field(default=21 * 1024 * 1024, gt=0)
    max_concurrent_uploads: int = Field(default=2, gt=0)
    max_features: int = Field(default=10000, gt=0)
    max_coordinates: int = Field(default=250000, gt=0)
    max_attribute_fields: int = Field(default=128, gt=0)
    max_property_bytes: int = Field(default=16384, gt=0)
    max_kml_depth: int = Field(default=32, gt=0)
    max_storage_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    failed_upload_retention_days: int = Field(default=30, gt=0)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("This scaffold supports SQLite database URLs only")
        return value
