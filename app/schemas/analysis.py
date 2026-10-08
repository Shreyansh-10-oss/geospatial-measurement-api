"""Optional persisted dataset-analysis response."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel
from app.models.upload import FileStatus


class DatasetAnalysis(BaseModel):
    file_id: UUID
    status: FileStatus
    feature_count: int
    count_by_geometry_type: dict[str, int]
    total_measured_polygon_area: float
    area_unit: Literal["square_meters"]
    total_measured_line_length: float
    length_unit: Literal["meters"]
    source_crs: str | None
    measurement_crs: list[str]
    projection_strategies: list[str]
    warnings: list[str]
