"""Optional dataset summaries derived exclusively from persisted results."""
from collections import Counter
import math
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.feature import FeatureResult
from app.models.upload import FileStatus, UploadedFile
from app.services.files import FileNotFoundError


def analyze_dataset(file_id: UUID, session: Session) -> dict:
    record = session.get(UploadedFile, str(file_id))
    if record is None:
        raise FileNotFoundError("Uploaded file not found")
    # Read only columns needed for aggregation; omit geometry and attribute payloads.
    rows = session.execute(select(
        FeatureResult.geometry_type, FeatureResult.status, FeatureResult.measurement_type,
        FeatureResult.value, FeatureResult.unit, FeatureResult.measurement_crs,
        FeatureResult.projection_strategy, FeatureResult.warnings,
    ).where(FeatureResult.file_id == str(file_id))).all()
    warnings = set(record.warnings)
    for row in rows:
        warnings.update(row.warnings)
    if record.status != FileStatus.COMPLETED:
        warnings.add("FILE_FAILED" if record.status == FileStatus.FAILED else "FILE_NOT_COMPLETED")
    crss = sorted({row.measurement_crs for row in rows if row.measurement_crs is not None})
    strategies = sorted({row.projection_strategy for row in rows if row.projection_strategy is not None})
    if len(crss) > 1:
        warnings.add("MULTIPLE_MEASUREMENT_CRSS")
    valid_values = [row for row in rows if row.status == "MEASURED" and row.value is not None
                    and math.isfinite(row.value) and row.value >= 0]
    area = math.fsum(row.value for row in valid_values if row.geometry_type == "Polygon"
                     and row.measurement_type == "area" and row.unit == "square_meters")
    length = math.fsum(row.value for row in valid_values if row.geometry_type == "LineString"
                       and row.measurement_type == "length" and row.unit == "meters")
    return {
        "file_id": record.id,
        "status": record.status,
        "feature_count": len(rows),
        "count_by_geometry_type": dict(sorted(Counter(row.geometry_type or "Unknown" for row in rows).items())),
        "total_measured_polygon_area": area,
        "area_unit": "square_meters",
        "total_measured_line_length": length,
        "length_unit": "meters",
        "source_crs": record.source_crs,
        "measurement_crs": crss,
        "projection_strategies": strategies,
        "warnings": sorted(warnings),
    }
