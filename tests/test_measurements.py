"""Numerical and failure-isolation tests for the measurement service."""
import pytest
from pyproj import CRS, Geod, Transformer
from shapely import transform
from shapely.geometry import Point, Polygon, LineString, MultiPolygon, MultiLineString, GeometryCollection, box

from app.services.crs import CRSResolution, resolve_measurement_crs
from app.services.measurements import MeasurementStatus, measure_geometry


def projected_resolution():
    return CRSResolution(CRS.from_epsg(32643), CRS.from_epsg(32643), "preserve_projected")


def test_known_projected_area():
    geometry = box(500000, 3000000, 500100, 3000050)
    result = measure_geometry(geometry, resolve_measurement_crs(geometry, 32643), feature_index=1)
    assert result.status == MeasurementStatus.MEASURED
    assert result.value == pytest.approx(5000, abs=1e-6)
    assert result.measurement_type == "area"
    assert result.unit == "square_meters"
    assert result.to_dict()["feature_index"] == 1
    assert result.to_dict()["status"] == "MEASURED"


def test_polygon_holes_subtracted():
    geometry = Polygon([(500000, 3000000), (500100, 3000000), (500100, 3000100), (500000, 3000100)],
                       holes=[[(500010, 3000010), (500030, 3000010), (500030, 3000030), (500010, 3000030)]])
    result = measure_geometry(geometry, projected_resolution())
    assert result.value == pytest.approx(9600, abs=1e-6)


def test_known_projected_line_length():
    geometry = LineString([(500000, 3000000), (500003, 3000004), (500006, 3000008)])
    result = measure_geometry(geometry, projected_resolution())
    assert result.value == pytest.approx(10, abs=1e-9)
    assert result.measurement_type == "length"
    assert result.unit == "meters"


@pytest.mark.parametrize("epsg,northing", [(32643, 3000000), (32743, 6000000)])
def test_geographic_polygon_reprojected_to_known_area(epsg, northing):
    square = box(500000, northing, 500100, northing + 100)
    geographic = transform(square, Transformer.from_crs(epsg, 4326, always_xy=True).transform, interleaved=False)
    original = geographic.wkb
    resolution = resolve_measurement_crs(geographic, 4326)
    result = measure_geometry(geographic, resolution)
    assert resolution.measurement_crs.to_epsg() == epsg
    assert result.value == pytest.approx(10000, abs=0.01)
    assert result.source_crs == "EPSG:4326"
    assert result.measurement_crs == f"EPSG:{epsg}"
    assert result.projection_strategy == "auto_utm"
    assert geographic.wkb == original


def test_geographic_line_against_geodesic_reference():
    geometry = LineString([(75, 28), (75, 28.001)])
    result = measure_geometry(geometry, resolve_measurement_crs(geometry, 4326))
    _, _, geodesic_length = Geod(ellps="WGS84").inv(75, 28, 75, 28.001)
    # UTM central-meridian scale is 0.9996; 0.1% allows expected projection distortion.
    assert result.value == pytest.approx(geodesic_length, rel=0.001)
    assert result.value > 100


def test_point_needs_no_projection():
    result = measure_geometry(Point(77, 28), None)
    assert result.status == MeasurementStatus.NOT_REQUIRED
    assert result.value is None
    assert result.measurement_type is None
    assert result.unit is None


@pytest.mark.parametrize("geometry", [MultiPolygon([box(0, 0, 1, 1)]), MultiLineString([[(0, 0), (1, 1)]]), GeometryCollection([Point(1, 2)])])
def test_unsupported_geometry(geometry):
    result = measure_geometry(geometry, None)
    assert result.status == MeasurementStatus.UNSUPPORTED
    assert result.value is None
    assert "UNSUPPORTED_GEOMETRY_TYPE" in result.warnings


@pytest.mark.parametrize("geometry", [None, Polygon(), LineString(), Point()])
def test_empty_geometry(geometry):
    result = measure_geometry(geometry, None)
    assert result.status == MeasurementStatus.EMPTY_GEOMETRY
    assert result.value is None


def test_invalid_polygon_is_not_repaired():
    geometry = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    result = measure_geometry(geometry, projected_resolution())
    assert result.status == MeasurementStatus.INVALID_GEOMETRY
    assert result.value is None


def test_missing_resolution():
    result = measure_geometry(box(0, 0, 1, 1), None)
    assert result.error_code == "MISSING_CRS_RESOLUTION"


@pytest.mark.parametrize("target", [4326, 2263])
def test_target_must_be_projected_in_metres(target):
    resolution = CRSResolution(CRS.from_epsg(4326), CRS.from_epsg(target), "test")
    result = measure_geometry(LineString([(77, 28), (77.01, 28)]), resolution)
    assert result.status == MeasurementStatus.FAILED
    assert result.error_code == "MEASUREMENT_CRS_NOT_PROJECTED_METRES"


def test_z_ignored_and_warnings_not_mutated():
    resolution = projected_resolution()
    resolution.warnings.append("EXISTING_WARNING")
    geometry = LineString([(500000, 3000000, 0), (500003, 3000004, 100)])
    result = measure_geometry(geometry, resolution)
    assert result.value == pytest.approx(5)
    assert "Z_IGNORED_FOR_MEASUREMENT" in result.warnings
    assert result.warnings[0] == "EXISTING_WARNING"
    assert resolution.warnings == ["EXISTING_WARNING"]
    assert geometry.has_z


def test_transform_failure_is_result(monkeypatch):
    from app.services import measurements
    from pyproj.exceptions import ProjError

    def fail(*args, **kwargs):
        raise ProjError("required grid unavailable")

    monkeypatch.setattr(measurements.Transformer, "from_crs", fail)
    result = measure_geometry(box(500000, 3000000, 500010, 3000010), projected_resolution())
    assert result.status == MeasurementStatus.FAILED
    assert result.error_code == "MEASUREMENT_TRANSFORMATION_FAILED"
    assert result.value is None


def test_nonfinite_geometry():
    result = measure_geometry(LineString([(0, 0), (float("inf"), 1)]), projected_resolution())
    assert result.error_code == "NONFINITE_COORDINATES"


@pytest.mark.parametrize("index", [-1, True, 1.5])
def test_invalid_index_is_programmer_error(index):
    with pytest.raises(ValueError):
        measure_geometry(Point(1, 2), None, feature_index=index)
