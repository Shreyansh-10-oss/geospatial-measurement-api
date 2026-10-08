"""Deterministic, conservative CRS selection, independent of HTTP and persistence."""
from dataclasses import dataclass, field
import math
from typing import Any

import numpy as np
from pyproj import CRS, Proj, Transformer
from pyproj.exceptions import CRSError, ProjError
from shapely import get_coordinates
from shapely.geometry.base import BaseGeometry


class CRSResolutionError(Exception):
    def __init__(self, code: str, message: str, warnings: list[str] | None = None):
        self.code = code
        self.warnings = list(warnings or [])
        super().__init__(message)


@dataclass(frozen=True)
class CRSResolution:
    source_crs: CRS
    measurement_crs: CRS
    strategy: str
    warnings: list[str] = field(default_factory=list)

    def to_metadata(self) -> dict[str, Any]:
        # WKT preserves custom CRSs with no authority identifier.
        return {
            "source_crs": _identifier(self.source_crs),
            "measurement_crs": _identifier(self.measurement_crs),
            "strategy": self.strategy,
            "warnings": list(self.warnings),
        }


def _identifier(crs: CRS) -> str:
    authority = crs.to_authority()
    return ":".join(authority) if authority else crs.to_wkt()


def utm_zone(longitude: float) -> int:
    """Standard six-degree UTM zones; +180 belongs to zone 60."""
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise CRSResolutionError("INVALID_COORDINATES", "Longitude is outside [-180, 180]")
    return min(60, int(math.floor((longitude + 180) / 6)) + 1)


def _transformer(source: CRS, target: CRS) -> Transformer:
    return Transformer.from_crs(source, target, always_xy=True, allow_ballpark=False, only_best=True)


def _scale_is_acceptable(crs: CRS, bounds: tuple[float, float, float, float]) -> bool:
    """Sample a 5x5 geographic envelope, including corners; not an accuracy guarantee."""
    west, south, east, north = bounds
    try:
        longitudes, latitudes = np.meshgrid(np.linspace(west, east, 5), np.linspace(south, north, 5))
        lon, lat = _transformer(CRS.from_epsg(4326), crs.geodetic_crs).transform(
            longitudes.ravel(), latitudes.ravel(), errcheck=True
        )
        factors = Proj(crs).get_factors(lon, lat, errcheck=True)
        major = np.asarray(factors.tissot_semimajor)
        minor = np.asarray(factors.tissot_semiminor)
        area = np.asarray(factors.areal_scale)
        return bool(
            np.isfinite(major).all() and np.isfinite(minor).all() and np.isfinite(area).all()
            and np.max(np.abs(major - 1)) <= 0.01
            and np.max(np.abs(minor - 1)) <= 0.01
            and np.max(np.abs(area - 1)) <= 0.02
        )
    except (ProjError, ValueError):
        return False


def _within_area(crs: CRS, bounds) -> bool:
    area = crs.area_of_use
    if area is None:
        return False
    west, south, east, north = bounds
    # Wrapped authority extents are conservatively declined for preservation.
    return area.west <= west <= east <= area.east and area.south <= south <= north <= area.north


def resolve_measurement_crs(geometry: BaseGeometry | None, source_crs: Any) -> CRSResolution:
    """Select a CRS only. Callers must transform geometry before measuring.

    Missing CRS, invalid inputs, excessive extents, equator crossings and unsupported
    UTM latitudes raise controlled errors. Near-zone crossings can return warnings.
    Coordinates follow GIS x/y ordering. Z is ignored for selection, never modified.
    """
    if source_crs is None:
        raise CRSResolutionError("MISSING_CRS", "Source CRS is required; no CRS is inferred", ["MISSING_CRS"])
    try:
        source = CRS.from_user_input(source_crs)
    except CRSError as exc:
        raise CRSResolutionError("INVALID_CRS", "Source CRS cannot be interpreted") from exc
    if not (source.is_geographic or source.is_projected) or len(source.axis_info) != 2:
        raise CRSResolutionError("UNSUPPORTED_CRS", "A two-dimensional geographic or projected CRS is required")
    if geometry is None or geometry.is_empty:
        raise CRSResolutionError("EMPTY_GEOMETRY", "Location cannot be resolved for an empty geometry")
    coordinates = get_coordinates(geometry)
    if not np.isfinite(coordinates).all():
        raise CRSResolutionError("INVALID_COORDINATES", "Geometry has nonfinite coordinates")
    if not geometry.is_valid:
        raise CRSResolutionError("INVALID_GEOMETRY", "CRS selection requires a valid geometry")
    warnings = []
    if geometry.has_z:
        warnings.append("Z_IGNORED_FOR_CRS_SELECTION")
    # Validate native degree coordinates before PROJ can normalize longitudes.
    if source.is_geographic and all(axis.unit_name == "degree" for axis in source.axis_info):
        if (np.abs(coordinates[:, 0]) > 180).any() or (np.abs(coordinates[:, 1]) > 90).any():
            raise CRSResolutionError("INVALID_COORDINATES", "Geographic coordinates are outside valid ranges")
    try:
        transformer = _transformer(source, CRS.from_epsg(4326))
        lon, lat = transformer.transform(coordinates[:, 0], coordinates[:, 1], errcheck=True)
        bounds = transformer.transform_bounds(*geometry.bounds, densify_pts=21, errcheck=True)
    except (ProjError, ValueError) as exc:
        raise CRSResolutionError("TRANSFORMATION_FAILED", "Source CRS cannot be located reliably in WGS84") from exc
    if not np.isfinite(lon).all() or not np.isfinite(lat).all() or not all(math.isfinite(v) for v in bounds):
        raise CRSResolutionError("TRANSFORMATION_FAILED", "Location transformation produced nonfinite coordinates")
    west, south, east, north = bounds
    if east < west or east - west > 180 or float(np.max(lon) - np.min(lon)) > 180:
        raise CRSResolutionError("ANTIMERIDIAN_UNSUPPORTED", "Antimeridian or global extents require a different strategy", ["ANTIMERIDIAN_OR_GLOBAL_EXTENT"])
    if east - west > 6 or north - south > 10:
        warnings.append("LARGE_REGION")
    if east - west > 12 or north - south > 20:
        raise CRSResolutionError("EXTENT_TOO_LARGE", "Extent exceeds the supported local projection policy", warnings)

    if source.is_projected:
        metric = all(math.isclose(axis.unit_conversion_factor, 1.0) for axis in source.axis_info)
        if not metric:
            warnings.append("SOURCE_PROJECTED_UNITS_NOT_METRES")
        elif source.to_epsg() == 3857:
            warnings.append("WEB_MERCATOR_NOT_SUITABLE")
        elif not _within_area(source, bounds):
            warnings.append("SOURCE_PROJECTED_AREA_UNVERIFIED")
        elif _scale_is_acceptable(source, bounds):
            return CRSResolution(source, source, "preserve_projected", warnings)
        else:
            warnings.append("SOURCE_PROJECTED_DISTORTION_TOO_HIGH")

    if south < -80 or north > 84:
        raise CRSResolutionError("UTM_LATITUDE_UNSUPPORTED", "UTM policy supports latitudes from -80 to 84 degrees", warnings + ["OUTSIDE_UTM_LATITUDE_RANGE"])
    if south < 0 < north:
        raise CRSResolutionError("EQUATOR_CROSSING", "Geometry spans both UTM hemispheres", warnings + ["MULTIPLE_HEMISPHERES"])
    longitude = (west + east) / 2
    latitude = (south + north) / 2
    zone = utm_zone(longitude)
    if utm_zone(west) != utm_zone(east):
        warnings.append("MULTIPLE_UTM_ZONES")
    if math.isclose((longitude + 180) % 6, 0, abs_tol=1e-8):
        warnings.append("UTM_ZONE_BOUNDARY")
    target = CRS.from_epsg((32600 if latitude >= 0 else 32700) + zone)
    if not _within_area(target, bounds):
        warnings.append("OUTSIDE_SELECTED_UTM_AREA")
    if not _scale_is_acceptable(target, bounds):
        raise CRSResolutionError("PROJECTION_DISTORTION_TOO_HIGH", "Selected UTM projection exceeds distortion policy", warnings)
    return CRSResolution(source, target, "auto_utm", warnings)
