"""Read persisted file information without processing files again."""
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.feature import FeatureResult
from app.models.upload import FileStatus, UploadedFile


class FileNotFoundError(Exception):
    """No upload exists for the requested ID."""


def get_file_information(file_id: UUID, session: Session) -> dict:
    record = session.get(UploadedFile, str(file_id))
    if record is None:
        raise FileNotFoundError("Uploaded file not found")
    error = None
    if record.status == FileStatus.FAILED:
        error = {
            "code": record.error_code or "PROCESSING_FAILED",
            "message": record.error_message or "Unable to process uploaded geospatial dataset",
        }
    return {
        "id": record.id,
        "filename": record.filename,
        "file_type": "shapefile" if record.format == "zip" else record.format,
        "feature_count": record.feature_count,
        "original_crs": record.source_crs,
        "status": record.status,
        "created_at": record.created_at,
        "processing_error": error,
    }


def get_file_measurements(file_id: UUID, session: Session) -> list:
    """Return every persisted feature in ingestion order, with no recalculation."""
    if session.get(UploadedFile, str(file_id)) is None:
        raise FileNotFoundError("Uploaded file not found")
    columns = [getattr(FeatureResult, name) for name in (
        "feature_index", "geometry_type", "properties", "status", "measurement_type", "value", "unit",
        "source_crs", "measurement_crs", "projection_strategy", "warnings", "error_code",
    )]
    return list(session.execute(
        select(*columns).where(FeatureResult.file_id == str(file_id))
        .order_by(FeatureResult.feature_index)
    ))
