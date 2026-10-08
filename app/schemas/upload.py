"""Public upload metadata; internal storage paths are omitted."""
from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict
from app.schemas.common import UTCResponse

from app.models.upload import FileStatus


class UploadResponse(UTCResponse):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    format: str
    size_bytes: int
    status: FileStatus
    created_at: datetime

    completed_at: datetime | None
    discovered_feature_count: int
    feature_count: int
    measured_count: int
    skipped_count: int
    failed_count: int
    source_crs: str | None
    warnings: list[str]
    error_code: str | None
    error_message: str | None
