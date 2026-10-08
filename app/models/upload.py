"""Persisted upload metadata."""
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Enum as SQLEnum, String, JSON, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FileStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','PROCESSING','COMPLETED','FAILED')", name="upload_status"),
        CheckConstraint("size_bytes >= 0 AND feature_count >= 0 AND discovered_feature_count >= 0 AND measured_count >= 0 AND skipped_count >= 0 AND failed_count >= 0", name="upload_nonnegative"),
        CheckConstraint("feature_count = measured_count + skipped_count + failed_count", name="upload_counts"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int]
    storage_key: Mapped[str] = mapped_column(String(100))
    status: Mapped[FileStatus] = mapped_column(SQLEnum(FileStatus), default=FileStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_feature_count: Mapped[int] = mapped_column(default=0, server_default="0")
    feature_count: Mapped[int] = mapped_column(default=0, server_default="0")
    measured_count: Mapped[int] = mapped_column(default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    source_crs: Mapped[str | None] = mapped_column(nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    error_code: Mapped[str | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(nullable=True)
