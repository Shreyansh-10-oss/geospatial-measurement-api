"""Local geospatial parsing and normalization; no measurement or reprojection."""
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import math
import json
import re
import struct
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pyproj import CRS
from pyproj.exceptions import CRSError
from pyogrio import read_info
from app.utils.geometry import coordinate_error
from pyogrio.errors import DataSourceError, DataLayerError, FeatureError, GeometryError
from shapely.errors import GEOSException
from shapely.geometry import Point, LineString, Polygon, GeometryCollection
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

KML_NS = "http://www.opengis.net/kml/2.2"
NS = {"k": KML_NS}
SUPPORTED_TYPES = {"Point", "LineString", "Polygon"}


class ReaderError(Exception):
    """Dataset-level failure with a safe machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class GeometryStatus(str, Enum):
    VALID = "VALID"
    EMPTY = "EMPTY"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"
    PARSE_ERROR = "PARSE_ERROR"


@dataclass
class NormalizedFeature:
    feature_index: int
    geometry: BaseGeometry | None
    geometry_type: str | None
    source_crs: CRS | None
    properties: dict[str, Any]
    source_feature_id: str | None = None
    geometry_status: GeometryStatus = GeometryStatus.VALID
    warnings: list[str] = field(default_factory=list)
    raw_geometry: dict | None = None


@dataclass
class ReadResult:
    features: list[NormalizedFeature]
    source_crs: CRS | None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReaderLimits:
    max_features: int = 10000
    max_coordinates: int = 250000
    max_attribute_fields: int = 128
    max_property_bytes: int = 16384
    max_kml_depth: int = 32

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, name) for name in cls.__dataclass_fields__})


class UnsupportedKMLGeometry(ValueError):
    pass


def parse_kml(path, limits: ReaderLimits, *, retain_tree=True):
    """Check syntax, depth and budgets incrementally before building geometries."""
    depth = features = coordinates = 0
    root = None
    try:
        for event, element in ElementTree.iterparse(path, events=("start", "end"), forbid_dtd=True):
            if event == "start":
                depth += 1
                if root is None:
                    root = element
                    if root.tag != "{" + KML_NS + "}kml":
                        raise ReaderError("PARSE_ERROR", "Expected a KML 2.2 document")
                if element.get("name") is not None and len(element.get("name").encode("utf-8")) > limits.max_property_bytes:
                    raise ReaderError("RESOURCE_LIMIT", "Property name size exceeds limit")
                if depth > limits.max_kml_depth:
                    raise ReaderError("RESOURCE_LIMIT", "KML nesting exceeds limit")
                if element.tag == "{" + KML_NS + "}Placemark":
                    features += 1
                    if features > limits.max_features:
                        raise ReaderError("RESOURCE_LIMIT", "Feature count exceeds limit")
            else:
                if element.tag == "{" + KML_NS + "}coordinates":
                    coordinates += sum(1 for _ in re.finditer(r"\S+", element.text or ""))
                    if coordinates > limits.max_coordinates:
                        raise ReaderError("RESOURCE_LIMIT", "Coordinate count exceeds limit")
                if element.tag.rsplit("}", 1)[-1] in {"name", "description", "value", "SimpleData"}:
                    if len((element.text or "").encode("utf-8")) > limits.max_property_bytes:
                        raise ReaderError("RESOURCE_LIMIT", "Property size exceeds limit")
                depth -= 1
                if not retain_tree:
                    element.clear()
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        raise ReaderError("PARSE_ERROR", "Unable to parse safe KML XML") from exc
    return root


def inspect_shapefile(path: Path, limits: ReaderLimits):
    siblings = {p.suffix.lower() for p in path.parent.iterdir()
                if p.stem.casefold() == path.stem.casefold()}
    if not {".shp", ".shx", ".dbf"}.issubset(siblings):
        raise ReaderError("MISSING_COMPONENTS", "Shapefile requires .shp, .shx, and .dbf components")
    # Bound DBF width and declared rows before allocating dataframe attributes.
    dbf_path = next(p for p in path.parent.iterdir() if p.stem.casefold() == path.stem.casefold() and p.suffix.lower() == ".dbf")
    with dbf_path.open("rb") as source:
        header = source.read(32)
    if len(header) != 32:
        raise ReaderError("PARSE_ERROR", "Incomplete Shapefile attribute header")
    records = struct.unpack_from("<I", header, 4)[0]
    header_length, record_length = struct.unpack_from("<HH", header, 8)
    if header_length < 33 or record_length < 1:
        raise ReaderError("PARSE_ERROR", "Invalid Shapefile attribute header")
    fields = (header_length - 33) // 32
    if records > limits.max_features or fields > limits.max_attribute_fields or record_length > limits.max_property_bytes:
        raise ReaderError("RESOURCE_LIMIT", "Shapefile attribute budget exceeded")
    # Record headers expose point counts without native geometry allocation.
    try:
        size = path.stat().st_size
        count = coordinates = 0
        with path.open("rb") as source:
            if len(source.read(100)) != 100:
                raise ValueError("Incomplete Shapefile header")
            while source.tell() < size:
                header = source.read(8)
                if len(header) != 8:
                    raise ValueError("Incomplete record header")
                length = struct.unpack(">ii", header)[1] * 2
                end = source.tell() + length
                if length < 4 or end > size:
                    raise ValueError("Invalid record length")
                kind = struct.unpack("<i", source.read(4))[0]
                count += 1
                if kind in {1, 11, 21}:
                    coordinates += 1
                elif kind in {8, 18, 28, 3, 5, 13, 15, 23, 25, 31}:
                    offset = 32 if kind in {8, 18, 28} else 36
                    if length < offset + 8:
                        raise ValueError("Incomplete geometry header")
                    source.seek(offset, 1)
                    points = struct.unpack("<i", source.read(4))[0]
                    if points < 0:
                        raise ValueError("Invalid point count")
                    coordinates += points
                if count > limits.max_features or coordinates > limits.max_coordinates:
                    raise ReaderError("RESOURCE_LIMIT", "Shapefile feature or coordinate count exceeds limit")
                source.seek(end)
        info = read_info(path)
        if info["features"] > limits.max_features:
            raise ReaderError("RESOURCE_LIMIT", "Feature count exceeds limit")
    except (ValueError, struct.error, DataSourceError, DataLayerError, FeatureError, GeometryError) as exc:
        raise ReaderError("PARSE_ERROR", "Unable to parse Shapefile dataset") from exc
    return info


def _check_properties(properties, limits):
    if len(json.dumps(properties, ensure_ascii=False, allow_nan=False).encode("utf-8")) > limits.max_property_bytes:
        raise ReaderError("RESOURCE_LIMIT", "Feature property size exceeds limit")


def _feature(index, geometry, crs, properties, source_id=None):
    result = NormalizedFeature(index, geometry, geometry.geom_type if geometry is not None else None,
                               crs, properties, source_id)
    if crs is None:
        result.warnings.append("MISSING_CRS")
    if geometry is None or geometry.is_empty:
        result.geometry_status = GeometryStatus.EMPTY
        result.warnings.append("EMPTY_GEOMETRY")
    elif coordinate_error(geometry, crs):
        result.geometry_status = GeometryStatus.INVALID
        result.warnings.append(coordinate_error(geometry, crs))
    elif not geometry.is_valid:
        result.geometry_status = GeometryStatus.INVALID
        result.warnings.append(f"INVALID_GEOMETRY: {explain_validity(geometry)}")
    elif geometry.geom_type not in SUPPORTED_TYPES:
        result.geometry_status = GeometryStatus.UNSUPPORTED
        result.warnings.append("UNSUPPORTED_GEOMETRY_TYPE")
    return result


def _property(value):
    """Convert pandas/NumPy attribute scalars into ordinary JSON-compatible values."""
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _property(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _read_shapefile(path: Path, limits: ReaderLimits) -> ReadResult:
    inspect_shapefile(path, limits)
    try:
        frame = gpd.read_file(path, engine="pyogrio", rows=slice(0, limits.max_features + 1))
    except (DataSourceError, DataLayerError, FeatureError, GeometryError, ValueError) as exc:
        raise ReaderError("PARSE_ERROR", "Unable to parse Shapefile dataset") from exc
    if len(frame) > limits.max_features:
        raise ReaderError("RESOURCE_LIMIT", "Feature count exceeds limit")
    try:
        crs = CRS.from_user_input(frame.crs) if frame.crs is not None else None
    except CRSError as exc:
        raise ReaderError("INVALID_CRS", "Shapefile CRS is invalid") from exc
    warnings = [] if crs else ["MISSING_CRS"]
    if len(frame) == 0:
        warnings.append("EMPTY_DATASET")
    geometry_column = frame.geometry.name
    features = [
        _feature(index, row[geometry_column], crs,
                 {str(key): _property(value) for key, value in row.items() if key != geometry_column})
        for index, (_, row) in enumerate(frame.iterrows())
    ]
    for feature in features:
        _check_properties(feature.properties, limits)
    return ReadResult(features, crs, warnings)


def _coordinates(element):
    text = element.findtext("k:coordinates", default="", namespaces=NS)
    coordinates = []
    for token in text.split():
        values = tuple(float(value) for value in token.split(","))
        if len(values) not in (2, 3) or not all(math.isfinite(value) for value in values):
            raise ValueError("Invalid coordinate tuple")
        coordinates.append(values)
    return coordinates


def _ring(element):
    coordinates = _coordinates(element)
    if len(coordinates) < 4 or coordinates[0] != coordinates[-1]:
        raise ValueError("LinearRing must be closed with at least four coordinates")
    return coordinates


def _geometry(element):
    kind = element.tag.removeprefix("{" + KML_NS + "}")
    if kind == "Point":
        coordinates = _coordinates(element)
        if len(coordinates) > 1:
            raise ValueError("Point requires at most one coordinate")
        return Point(coordinates[0]) if coordinates else Point()
    if kind == "LineString":
        return LineString(_coordinates(element))
    if kind == "Polygon":
        outer = element.find("k:outerBoundaryIs/k:LinearRing", NS)
        if outer is None:
            raise ValueError("Polygon requires an outer boundary")
        return Polygon(_ring(outer),
                       [_ring(ring) for ring in element.findall("k:innerBoundaryIs/k:LinearRing", NS)])
    if kind == "MultiGeometry":
        return GeometryCollection([_geometry(child) for child in element])
    raise UnsupportedKMLGeometry("Unsupported KML geometry")


def _read_kml(path: Path, limits: ReaderLimits) -> ReadResult:
    root = parse_kml(path, limits)
    crs = CRS.from_epsg(4326)
    features = []
    for index, placemark in enumerate(root.findall(".//k:Placemark", NS)):
        properties = {}
        for name in ("name", "description"):
            text = placemark.findtext(f"k:{name}", namespaces=NS)
            if text is not None:
                properties[name] = text
        attributes = {}
        for data in placemark.findall("k:ExtendedData/k:Data", NS):
            if data.get("name") is not None:
                attributes[data.get("name")] = data.findtext("k:value", namespaces=NS)
        for data in placemark.findall("k:ExtendedData/k:SchemaData/k:SimpleData", NS):
            if data.get("name") is not None:
                attributes[data.get("name")] = data.text
        # Keep KML metadata separate to avoid collisions with named attributes.
        if attributes:
            properties["attributes"] = attributes
        candidates = [child for child in placemark if child.tag.rsplit("}", 1)[-1] in
                      {"Point", "LineString", "Polygon", "MultiGeometry", "Model", "Track", "MultiTrack", "LinearRing"}]
        source_id = placemark.get("id")
        if not candidates:
            result = _feature(index, None, crs, properties, source_id)
        elif len(candidates) != 1:
            result = _feature(index, None, crs, properties, source_id)
            result.geometry_status = GeometryStatus.PARSE_ERROR
            result.warnings = ["MULTIPLE_PLACEMARK_GEOMETRIES"]
        else:
            element = candidates[0]
            kind = element.tag.rsplit("}", 1)[-1]
            if element.tag.startswith("{" + KML_NS + "}") and kind in {"Point", "LineString", "Polygon", "MultiGeometry"}:
                try:
                    result = _feature(index, _geometry(element), crs, properties, source_id)
                except UnsupportedKMLGeometry:
                    result = _feature(index, None, crs, properties, source_id)
                    result.geometry_type = kind
                    result.geometry_status = GeometryStatus.UNSUPPORTED
                    result.warnings = ["UNSUPPORTED_GEOMETRY_TYPE"]
                    result.raw_geometry = {"format": "kml", "xml": ElementTree.tostring(element, encoding="unicode")}
                except (ValueError, TypeError, GEOSException):
                    result = _feature(index, None, crs, properties, source_id)
                    result.geometry_type = kind
                    result.geometry_status = GeometryStatus.PARSE_ERROR
                    result.warnings = ["INVALID_GEOMETRY_COORDINATES"]
            else:
                result = _feature(index, None, crs, properties, source_id)
                result.geometry_type = kind
                result.geometry_status = GeometryStatus.UNSUPPORTED
                result.warnings = ["UNSUPPORTED_GEOMETRY_TYPE"]
                result.raw_geometry = {"format": "kml", "xml": ElementTree.tostring(element, encoding="unicode")}
        _check_properties(properties, limits)
        features.append(result)
    return ReadResult(features, crs, [] if features else ["EMPTY_DATASET"])


def read_geospatial_file(path: str | Path, limits: ReaderLimits | None = None) -> ReadResult:
    """Read a local KML, Shapefile, or safely extracted Shapefile directory.

    ZIP validation/extraction belongs to the upload service. Never pass remote URLs.
    Missing CRS and individual geometry problems are returned without guessing or repair.
    Dataset-level parse failures raise ReaderError with the original exception chained.
    """
    limits = limits or ReaderLimits()
    path = Path(path)
    if not path.exists():
        raise ReaderError("FILE_NOT_FOUND", "Geospatial input does not exist")
    if path.is_dir():
        candidates = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".shp"]
        if len(candidates) != 1:
            raise ReaderError("AMBIGUOUS_DATASET", "Expected exactly one extracted Shapefile")
        path = candidates[0]
    try:
        if path.suffix.lower() == ".kml":
            return _read_kml(path, limits)
        if path.suffix.lower() == ".shp":
            return _read_shapefile(path, limits)
        raise ReaderError("UNSUPPORTED_FORMAT", "Reader accepts local .kml and .shp files")
    except OSError as exc:
        raise ReaderError("READ_ERROR", "Unable to access geospatial input") from exc
