"""CRS policy tests using independent expected EPSG codes and known locations."""
import pytest
from pyproj import CRS, Transformer
from shapely.geometry import Point, LineString, Polygon, box
from shapely import transform as transform_geometry


def transform(function, geometry):
    return transform_geometry(geometry, function, interleaved=False)

from app.services.crs import CRSResolutionError, resolve_measurement_crs, utm_zone


@pytest.mark.parametrize("longitude,latitude,expected", [
    (77.2, 28.6, 32643), (151.2, -33.9, 32756),
    (-74, 40.7, 32618), (-58.4, -34.6, 32721),
    (3, 45, 32631), (9, 45, 32632),
    (-180, 10, 32601), (180, -10, 32760), (3, 0, 32631),
])
def test_geographic_utm_selection(longitude, latitude, expected):
    geometry = Point(longitude, latitude)
    result = resolve_measurement_crs(geometry, "EPSG:4326")
    assert result.source_crs.to_epsg() == 4326
    assert result.measurement_crs.to_epsg() == expected
    assert result.strategy == "auto_utm"
    assert result.measurement_crs.is_projected
    assert geometry.coords[0] == (longitude, latitude)


def test_structured_metadata():
    assert resolve_measurement_crs(Point(77, 28), 4326).to_metadata() == {
        "source_crs": "EPSG:4326", "measurement_crs": "EPSG:32643",
        "strategy": "auto_utm", "warnings": [],
    }


@pytest.mark.parametrize("epsg,location", [(32643, Point(77, 28)), (32756, Point(151, -34))])
def test_preserve_projected_utm(epsg, location):
    projected = transform(Transformer.from_crs(4326, epsg, always_xy=True).transform, location)
    result = resolve_measurement_crs(projected, epsg)
    assert result.strategy == "preserve_projected"
    assert result.measurement_crs == CRS.from_epsg(epsg)


def test_preserve_suitable_non_utm():
    location = Point(10, 52)
    projected = transform(Transformer.from_crs(4326, 3035, always_xy=True).transform, location)
    result = resolve_measurement_crs(projected, 3035)
    assert result.strategy == "preserve_projected"
    assert result.measurement_crs.to_epsg() == 3035


def test_web_mercator_replaced():
    projected = transform(Transformer.from_crs(4326, 3857, always_xy=True).transform, Point(77, 28))
    result = resolve_measurement_crs(projected, 3857)
    assert result.measurement_crs.to_epsg() == 32643
    assert "WEB_MERCATOR_NOT_SUITABLE" in result.warnings


def test_wrong_projected_zone_replaced():
    projected = transform(Transformer.from_crs(4326, 32632, always_xy=True).transform, Point(20, 40))
    result = resolve_measurement_crs(projected, 32632)
    assert result.strategy == "auto_utm"
    assert result.measurement_crs.to_epsg() == 32634
    assert "SOURCE_PROJECTED_AREA_UNVERIFIED" in result.warnings


def test_nonmetric_projected_crs_replaced():
    feet_crs = CRS.from_proj4("+proj=tmerc +lat_0=0 +lon_0=-75 +k=0.9996 +x_0=500000 +y_0=0 +datum=WGS84 +units=us-ft +no_defs")
    projected = transform(Transformer.from_crs(4326, feet_crs, always_xy=True).transform, Point(-74, 40.7))
    result = resolve_measurement_crs(projected, feet_crs)
    assert result.measurement_crs.to_epsg() == 32618
    assert "SOURCE_PROJECTED_UNITS_NOT_METRES" in result.warnings


def test_missing_crs():
    with pytest.raises(CRSResolutionError) as error:
        resolve_measurement_crs(Point(1, 2), None)
    assert error.value.code == "MISSING_CRS"
    assert error.value.warnings == ["MISSING_CRS"]


def test_zone_boundary():
    result = resolve_measurement_crs(Point(6, 40), 4326)
    assert result.measurement_crs.to_epsg() == 32632
    assert "UTM_ZONE_BOUNDARY" in result.warnings


def test_multizone_geometry_warns():
    result = resolve_measurement_crs(box(5.9, 40, 6.1, 40.1), 4326)
    assert result.measurement_crs.to_epsg() == 32632
    assert "MULTIPLE_UTM_ZONES" in result.warnings
    assert "OUTSIDE_SELECTED_UTM_AREA" in result.warnings


def test_large_but_acceptable_region_warns():
    result = resolve_measurement_crs(box(1, 45, 8, 46), 4326)
    assert "LARGE_REGION" in result.warnings
    assert "MULTIPLE_UTM_ZONES" in result.warnings


@pytest.mark.parametrize("geometry,crs,code", [
    (Point(1, 2), "invalid-crs", "INVALID_CRS"),
    (Point(), 4326, "EMPTY_GEOMETRY"),
    (None, 4326, "EMPTY_GEOMETRY"),
    (Point(200, 10), 4326, "INVALID_COORDINATES"),
    (Point(1, 91), 4326, "INVALID_COORDINATES"),
    (Point(float("inf"), 10), 4326, "INVALID_COORDINATES"),
    (Point(3, 85), 4326, "UTM_LATITUDE_UNSUPPORTED"),
    (Point(3, -81), 4326, "UTM_LATITUDE_UNSUPPORTED"),
    (box(2, -1, 3, 1), 4326, "EQUATOR_CROSSING"),
    (LineString([(179, 10), (-179, 10)]), 4326, "ANTIMERIDIAN_UNSUPPORTED"),
    (box(-10, 20, 20, 30), 4326, "EXTENT_TOO_LARGE"),
    (Point(1, 2), 4978, "UNSUPPORTED_CRS"),
    (Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)]), 4326, "INVALID_GEOMETRY"),
])
def test_controlled_errors(geometry, crs, code):
    with pytest.raises(CRSResolutionError) as error:
        resolve_measurement_crs(geometry, crs)
    assert error.value.code == code


def test_custom_crs_without_area_uses_utm_and_preserves_wkt():
    custom = CRS.from_proj4("+proj=tmerc +lat_0=0 +lon_0=77 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
    projected = transform(Transformer.from_crs(4326, custom, always_xy=True).transform, Point(77, 28))
    result = resolve_measurement_crs(projected, custom)
    assert result.strategy == "auto_utm"
    assert result.to_metadata()["source_crs"].startswith("PROJCRS[")


def test_z_does_not_change_selection():
    result = resolve_measurement_crs(Point(77, 28, 100), 4326)
    assert result.measurement_crs.to_epsg() == 32643
    assert "Z_IGNORED_FOR_CRS_SELECTION" in result.warnings


@pytest.mark.parametrize("longitude,expected", [(-180, 1), (-174, 2), (0, 31), (6, 32), (179.99, 60), (180, 60)])
def test_zone_formula(longitude, expected):
    assert utm_zone(longitude) == expected


def test_unavailable_transformation_is_controlled(monkeypatch):
    from pyproj.exceptions import ProjError
    from app.services import crs

    def unavailable(*args):
        raise ProjError("required datum grid missing")

    monkeypatch.setattr(crs, "_transformer", unavailable)
    with pytest.raises(CRSResolutionError) as error:
        resolve_measurement_crs(Point(77, 28), 4326)
    assert error.value.code == "TRANSFORMATION_FAILED"


def test_distortion_failure_is_controlled(monkeypatch):
    from app.services import crs
    monkeypatch.setattr(crs, "_scale_is_acceptable", lambda *args: False)
    with pytest.raises(CRSResolutionError) as error:
        resolve_measurement_crs(Point(77, 28), 4326)
    assert error.value.code == "PROJECTION_DISTORTION_TOO_HIGH"


def test_high_distortion_projected_crs_replaced():
    projected = transform(Transformer.from_crs(4326, 3395, always_xy=True).transform, Point(10, 60))
    result = resolve_measurement_crs(projected, 3395)
    assert result.strategy == "auto_utm"
    assert "SOURCE_PROJECTED_DISTORTION_TOO_HIGH" in result.warnings


@pytest.mark.parametrize("latitude", [-80, 84])
def test_utm_latitude_limits_inclusive(latitude):
    assert resolve_measurement_crs(Point(3, latitude), 4326).strategy == "auto_utm"
