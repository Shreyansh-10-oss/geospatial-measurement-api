"""Normalized source features and persisted measurement outcomes."""
from sqlalchemy import ForeignKey, JSON, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class FeatureResult(Base):
    __tablename__ = "feature_results"
    __table_args__ = (
        UniqueConstraint("file_id", "feature_index"),
        CheckConstraint("feature_index >= 0", name="feature_index_nonnegative"),
        CheckConstraint("status IN ('MEASURED','NOT_REQUIRED','UNSUPPORTED','EMPTY_GEOMETRY','INVALID_GEOMETRY','FAILED')", name="feature_status"),
        CheckConstraint("(status = 'MEASURED' AND geometry_type IS NOT NULL AND measurement_type IS NOT NULL AND unit IS NOT NULL AND value IS NOT NULL AND value >= 0 AND value <= 1.7976931348623157e308 AND measurement_crs IS NOT NULL AND projection_strategy IS NOT NULL AND ((geometry_type = 'Polygon' AND measurement_type = 'area' AND unit = 'square_meters') OR (geometry_type = 'LineString' AND measurement_type = 'length' AND unit = 'meters'))) OR (status != 'MEASURED' AND value IS NULL AND measurement_type IS NULL AND unit IS NULL)", name="feature_measurement"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("uploaded_files.id", ondelete="CASCADE"), index=True)
    feature_index: Mapped[int]
    source_feature_id: Mapped[str | None] = mapped_column(nullable=True)
    geometry_type: Mapped[str | None] = mapped_column(nullable=True)
    geometry: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    properties: Mapped[dict] = mapped_column(JSON)
    source_crs: Mapped[str | None] = mapped_column(nullable=True)
    measurement_crs: Mapped[str | None] = mapped_column(nullable=True)
    projection_strategy: Mapped[str | None] = mapped_column(nullable=True)
    status: Mapped[str]
    measurement_type: Mapped[str | None] = mapped_column(nullable=True)
    value: Mapped[float | None] = mapped_column(nullable=True)
    unit: Mapped[str | None] = mapped_column(nullable=True)
    warnings: Mapped[list] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(nullable=True)
