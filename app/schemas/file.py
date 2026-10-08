"""Public file-information contract."""
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel
from app.schemas.common import UTCResponse
from app.models.upload import FileStatus


class ProcessingErrorInfo(BaseModel):
    code: str
    message: str


class FileInformation(UTCResponse):
    id: UUID
    filename: str
    file_type: Literal["kml", "shapefile"]
    feature_count: int
    original_crs: str | None
    status: FileStatus
    created_at: datetime
    processing_error: ProcessingErrorInfo | None

