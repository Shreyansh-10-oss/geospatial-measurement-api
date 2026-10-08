"""Reader unit tests: parsing and normalization, never measurements."""
import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon, MultiPoint

from app.services.reader import GeometryStatus, ReaderError, read_geospatial_file


def kml(tmp_path, body):
    path = tmp_path / "data.kml"
    path.write_text(f'<kml xmlns="http://www.opengis.net/kml/2.2">{body}</kml>')
    return path


def shapefile(tmp_path, geometry, crs="EPSG:4326"):
    path = tmp_path / "data.shp"
    gpd.GeoDataFrame({"name": ["sample"] * len(geometry)}, geometry=geometry, crs=crs).to_file(path, engine="pyogrio")
    return path


def test_kml_normalization(tmp_path):
    path = kml(tmp_path, '<Document><Folder><Placemark id="a"><name>Example</name><ExtendedData><Data name="category"><value>test</value></Data></ExtendedData><Point><coordinates>77,28,5</coordinates></Point></Placemark><Placemark><LineString><coordinates>77,28 78,29</coordinates></LineString></Placemark></Folder></Document>')
    result = read_geospatial_file(path)
    assert result.source_crs.to_epsg() == 4326
    assert [f.feature_index for f in result.features] == [0, 1]
    feature = result.features[0]
    assert feature.source_feature_id == "a"
    assert feature.geometry_type == "Point"
    assert feature.geometry.coords[0] == (77, 28, 5)
    assert feature.properties == {"name": "Example", "attributes": {"category": "test"}}
    assert feature.source_crs == result.source_crs
    assert all(f.geometry_status == GeometryStatus.VALID for f in result.features)


def test_shapefile_normalization(tmp_path):
    result = read_geospatial_file(shapefile(tmp_path, [Point(77, 28)]))
    assert result.source_crs.to_epsg() == 4326
    assert result.features[0].properties == {"name": "sample"}
    assert result.features[0].geometry.equals(Point(77, 28))
    assert result.features[0].feature_index == 0
    assert result.features[0].geometry_type == "Point"


def test_missing_crs_preserved(tmp_path):
    path = shapefile(tmp_path, [Point(1, 2)])
    path.with_suffix(".prj").unlink()
    result = read_geospatial_file(path)
    assert result.source_crs is None
    assert "MISSING_CRS" in result.warnings
    assert "MISSING_CRS" in result.features[0].warnings
    assert result.features[0].geometry.equals(Point(1, 2))


def test_invalid_polygon_not_repaired(tmp_path):
    polygon = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    result = read_geospatial_file(shapefile(tmp_path, [polygon]))
    feature = result.features[0]
    assert feature.geometry_status == GeometryStatus.INVALID
    assert not feature.geometry.is_valid
    assert feature.warnings[0].startswith("INVALID_GEOMETRY")


def test_unsupported_geometry_preserved(tmp_path):
    geometry = MultiPoint([(1, 2), (3, 4)])
    feature = read_geospatial_file(shapefile(tmp_path, [geometry])).features[0]
    assert feature.geometry_status == GeometryStatus.UNSUPPORTED
    assert feature.geometry.equals(geometry)
    assert feature.geometry_type == "MultiPoint"


def test_empty_shapefile(tmp_path):
    result = read_geospatial_file(shapefile(tmp_path, []))
    assert result.features == []
    assert "EMPTY_DATASET" in result.warnings


def test_empty_kml(tmp_path):
    result = read_geospatial_file(kml(tmp_path, "<Document/>"))
    assert result.features == []
    assert result.warnings == ["EMPTY_DATASET"]


def test_feature_error_does_not_abort_dataset(tmp_path):
    path = kml(tmp_path, '<Placemark><Point><coordinates>bad,28</coordinates></Point></Placemark><Placemark><Point><coordinates>77,28</coordinates></Point></Placemark>')
    result = read_geospatial_file(path)
    assert len(result.features) == 2
    assert result.features[0].geometry_status == GeometryStatus.PARSE_ERROR
    assert result.features[1].geometry_status == GeometryStatus.VALID


def test_null_geometry(tmp_path):
    feature = read_geospatial_file(kml(tmp_path, '<Placemark><name>No geometry</name></Placemark>')).features[0]
    assert feature.geometry is None
    assert feature.geometry_status == GeometryStatus.EMPTY
    assert feature.properties["name"] == "No geometry"


def test_kml_multigeometry(tmp_path):
    path = kml(tmp_path, '<Placemark><MultiGeometry><Point><coordinates>1,2</coordinates></Point><Point><coordinates>3,4</coordinates></Point></MultiGeometry></Placemark>')
    feature = read_geospatial_file(path).features[0]
    assert feature.geometry_type == "GeometryCollection"
    assert len(feature.geometry.geoms) == 2
    assert feature.geometry_status == GeometryStatus.UNSUPPORTED


@pytest.mark.parametrize("content", ["bad XML", "<other/>", '<!DOCTYPE kml [<!ENTITY x "bad">]><kml>&x;</kml>'])
def test_kml_parse_errors(tmp_path, content):
    path = tmp_path / "bad.kml"
    path.write_text(content)
    with pytest.raises(ReaderError) as error:
        read_geospatial_file(path)
    assert error.value.code == "PARSE_ERROR"


def test_corrupt_shapefile(tmp_path):
    path = tmp_path / "bad.shp"
    for suffix in (".shp", ".shx", ".dbf"):
        path.with_suffix(suffix).write_bytes(b"bad")
    with pytest.raises(ReaderError) as error:
        read_geospatial_file(path)
    assert error.value.code == "PARSE_ERROR"


def test_missing_components(tmp_path):
    path = tmp_path / "bad.shp"
    path.write_bytes(b"bad")
    with pytest.raises(ReaderError) as error:
        read_geospatial_file(path)
    assert error.value.code == "MISSING_COMPONENTS"


def test_extracted_directory(tmp_path):
    shapefile(tmp_path, [Point(1, 2)])
    assert len(read_geospatial_file(tmp_path).features) == 1


def test_missing_file(tmp_path):
    with pytest.raises(ReaderError) as error:
        read_geospatial_file(tmp_path / "missing.kml")
    assert error.value.code == "FILE_NOT_FOUND"


def test_unsupported_format(tmp_path):
    path = tmp_path / "data.zip"
    path.write_bytes(b"zip")
    with pytest.raises(ReaderError) as error:
        read_geospatial_file(path)
    assert error.value.code == "UNSUPPORTED_FORMAT"


def test_kml_polygon_holes(tmp_path):
    path = kml(tmp_path, '<Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 4,0 4,4 0,4 0,0</coordinates></LinearRing></outerBoundaryIs><innerBoundaryIs><LinearRing><coordinates>1,1 1,2 2,2 2,1 1,1</coordinates></LinearRing></innerBoundaryIs></Polygon></Placemark>')
    feature = read_geospatial_file(path).features[0]
    assert feature.geometry_status == GeometryStatus.VALID
    assert feature.geometry_type == "Polygon"
    assert len(feature.geometry.interiors) == 1


def test_unsupported_kml_geometry(tmp_path):
    feature = read_geospatial_file(kml(tmp_path, '<Placemark><Model/></Placemark>')).features[0]
    assert feature.geometry_status == GeometryStatus.UNSUPPORTED
    assert feature.geometry_type == "Model"
    assert feature.geometry is None


def test_invalid_kml_polygon(tmp_path):
    path = kml(tmp_path, '<Placemark><Polygon><outerBoundaryIs><LinearRing><coordinates>0,0 2,2 0,2 2,0 0,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>')
    assert read_geospatial_file(path).features[0].geometry_status == GeometryStatus.INVALID
