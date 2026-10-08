"""Horizontal geometry measurements; independent of HTTP, files and persistence."""
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import partial
import math

import numpy as np
from pyproj import Transformer
from pyproj.exceptions import ProjError
from shapely import get_coordinates, transform
from shapely.errors import GEOSException
from shapely.geometry.base import BaseGeometry

from app.services.crs import CRSResolution
from app.utils.geometry import coordinate_error


class MeasurementStatus(str, Enum):
    MEASURED = "MEASURED"
    NOT_REQUIRED = "NOT_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    EMPTY_GEOMETRY = "EMPTY_GEOMETRY"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"
    FAILED = "FAILED"


@dataclass
class MeasurementResult:
    feature_index: int
    geometry_type: str | None
    status: MeasurementStatus
    measurement_type: str | None = None
    value: float | None = None
    unit: str | None = None
    source_crs: str | None = None
    measurement_crs: str | None = None
    projection_strategy: str | None = None
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["status"] = self.status.value
        return result


def measure_geometry(
    geometry: BaseGeometry | None,
    resolution: CRSResolution | None,
    *,
    feature_index: int = 0,
) -> MeasurementResult:
    """Measure a Polygon or LineString in the resolved projected metre CRS.

    Point/unsupported inputs need no resolution. Expected feature-level failures
    return explicit results; callers can continue the remaining dataset. Projection
    safety is the resolver's responsibility: use a resolution for this same geometry.
    """
    if isinstance(feature_index, bool) or not isinstance(feature_index, int) or feature_index < 0:
        raise ValueError("feature_index must be a nonnegative integer")
    metadata = resolution.to_metadata() if resolution is not None else {}
    result = MeasurementResult(
        feature_index=feature_index,
        geometry_type=geometry.geom_type if geometry is not None else None,
        status=MeasurementStatus.FAILED,
        source_crs=metadata.get("source_crs"),
        measurement_crs=metadata.get("measurement_crs"),
        projection_strategy=metadata.get("strategy"),
        warnings=list(metadata.get("warnings", [])),
    )

    def reject(status, code):
        result.status = status
        result.error_code = code
        if code not in result.warnings:
            result.warnings.append(code)
        return result

    if geometry is None or geometry.is_empty:
        return reject(MeasurementStatus.EMPTY_GEOMETRY, "EMPTY_GEOMETRY")
    if geometry.geom_type not in {"Polygon", "LineString", "Point"}:
        result.status = MeasurementStatus.UNSUPPORTED
        result.warnings.append("UNSUPPORTED_GEOMETRY_TYPE")
        return result
    if not np.isfinite(get_coordinates(geometry)).all():
        return reject(MeasurementStatus.INVALID_GEOMETRY, "NONFINITE_COORDINATES")
    if not geometry.is_valid:
        return reject(MeasurementStatus.INVALID_GEOMETRY, "INVALID_GEOMETRY")
    error = coordinate_error(geometry, resolution.source_crs if resolution else None)
    if error:
        return reject(MeasurementStatus.INVALID_GEOMETRY, error)
    if geometry.geom_type == "Point":
        result.status = MeasurementStatus.NOT_REQUIRED
        return result
    if resolution is None:
        return reject(MeasurementStatus.FAILED, "MISSING_CRS_RESOLUTION")
    target = resolution.measurement_crs
    if (
        not target.is_projected or len(target.axis_info) != 2
        or not all(math.isclose(axis.unit_conversion_factor, 1.0, rel_tol=0, abs_tol=1e-12)
                   for axis in target.axis_info)
    ):
        return reject(MeasurementStatus.FAILED, "MEASUREMENT_CRS_NOT_PROJECTED_METRES")
    source = resolution.source_crs
    if not (source.is_geographic or source.is_projected) or len(source.axis_info) != 2:
        return reject(MeasurementStatus.FAILED, "UNSUPPORTED_SOURCE_CRS")
    coordinates = get_coordinates(geometry)
    if source.is_geographic and all(axis.unit_name == "degree" for axis in source.axis_info):
        if (np.abs(coordinates[:, 0]) > 180).any() or (np.abs(coordinates[:, 1]) > 90).any():
            return reject(MeasurementStatus.INVALID_GEOMETRY, "INVALID_GEOGRAPHIC_COORDINATES")
    if geometry.has_z and "Z_IGNORED_FOR_MEASUREMENT" not in result.warnings:
        result.warnings.append("Z_IGNORED_FOR_MEASUREMENT")
    try:
        transformer = Transformer.from_crs(
            source, target, always_xy=True, allow_ballpark=False, only_best=True
        )
        projected = transform(geometry, partial(transformer.transform, errcheck=True),
                              include_z=False, interleaved=False)
        if not np.isfinite(get_coordinates(projected)).all():
            return reject(MeasurementStatus.FAILED, "NONFINITE_PROJECTED_COORDINATES")
        if projected.is_empty or not projected.is_valid:
            return reject(MeasurementStatus.FAILED, "INVALID_PROJECTED_GEOMETRY")
        if not source.equals(target):
            sequences = [projected.coords] if projected.geom_type == "LineString" else [projected.exterior.coords, *[ring.coords for ring in projected.interiors]]
            if any(np.linalg.norm(np.diff(np.asarray(sequence), axis=0), axis=1).max(initial=0) > 10000 for sequence in sequences):
                result.warnings.append("LONG_SEGMENTS_APPROXIMATED")
        value = float(projected.area if geometry.geom_type == "Polygon" else projected.length)
    except (ProjError, GEOSException, ValueError, OverflowError):
        return reject(MeasurementStatus.FAILED, "MEASUREMENT_TRANSFORMATION_FAILED")
    if not math.isfinite(value) or value < 0:
        return reject(MeasurementStatus.FAILED, "INVALID_MEASUREMENT_VALUE")
    result.status = MeasurementStatus.MEASURED
    result.measurement_type = "area" if geometry.geom_type == "Polygon" else "length"
    result.value = value
    result.unit = "square_meters" if geometry.geom_type == "Polygon" else "meters"
    return result
