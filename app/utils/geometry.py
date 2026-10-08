"""Coordinate validation shared by readers and measurement logic."""
import numpy as np
from shapely import get_coordinates


def coordinate_error(geometry, crs):
    coordinates = get_coordinates(geometry)
    if not np.isfinite(coordinates).all():
        return "NONFINITE_COORDINATES"
    if crs is not None and crs.is_geographic and all(axis.unit_name == "degree" for axis in crs.axis_info):
        if (np.abs(coordinates[:, 0]) > 180).any() or (np.abs(coordinates[:, 1]) > 90).any():
            return "INVALID_GEOGRAPHIC_COORDINATES"
    return None
