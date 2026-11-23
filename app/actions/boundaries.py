"""Geometry helpers for the area FIRMS detections are filtered by. Pure shapely: no I/O, no template imports."""

import json
import logging
from typing import Any, Callable, Iterable, Iterator, List, Optional, Union

from shapely import make_valid
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import prep

logger = logging.getLogger(__name__)

_AREAL_TYPES = ("Polygon", "MultiPolygon")


class BoundaryError(ValueError):
    """No usable area could be built from the input."""


def _areal(geometry: BaseGeometry) -> Optional[BaseGeometry]:
    """The valid polygonal part of `geometry`, or None when it has none."""
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    if geometry.geom_type == "GeometryCollection":
        parts = [part for part in geometry.geoms if part.geom_type in _AREAL_TYPES and not part.is_empty]
        geometry = unary_union(parts) if parts else None
    if geometry is None or geometry.is_empty or geometry.geom_type not in _AREAL_TYPES:
        return None
    return geometry


def _feature_geometries(item: Any) -> Iterator[Any]:
    """GeoJSON geometry dicts inside a FeatureCollection or a bare Feature."""
    if not isinstance(item, dict):
        return
    if item.get("type") == "FeatureCollection":
        for feature in item.get("features") or []:
            yield from _feature_geometries(feature)
    elif item.get("type") == "Feature":
        yield item.get("geometry")


def union_geometries(geometries: Iterable[BaseGeometry]) -> BaseGeometry:
    geometries = list(geometries)
    if not geometries:
        raise BoundaryError("No geometries to combine.")
    return unary_union(geometries)


def geometry_from_feature_group(group: Any) -> BaseGeometry:
    """The union of the polygons in an EarthRanger spatial feature group's detail payload.

    ER serialises each feature as a one-feature GeoJSON FeatureCollection. Lines and points are ignored, invalid
    polygons are repaired, and features that cannot be read are skipped. Raises BoundaryError when nothing usable
    remains.
    """
    features = group.get("features") if isinstance(group, dict) else None
    polygons: List[BaseGeometry] = []
    for item in features or []:
        for geometry in _feature_geometries(item):
            if not isinstance(geometry, dict) or geometry.get("type") not in _AREAL_TYPES:
                continue
            try:
                polygon = _areal(shape(geometry))
            except Exception as exc:  # shapely raises a variety of errors on malformed coordinates
                logger.warning("Skipping an unreadable feature geometry: %s", type(exc).__name__)
                continue
            if polygon is not None:
                polygons.append(polygon)
    if not polygons:
        raise BoundaryError("The spatial feature group has no usable polygons.")
    return union_geometries(polygons)


def geometry_from_polygon(value: Union[dict, str, None]) -> BaseGeometry:
    """A GeoJSON Polygon geometry, given as a dict or as its JSON text. Raises BoundaryError for anything else."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise BoundaryError("The area is not valid JSON.") from exc
    if not isinstance(value, dict) or value.get("type") != "Polygon":
        raise BoundaryError("The area must be a GeoJSON Polygon geometry.")
    try:
        polygon = _areal(shape(value))
    except Exception as exc:
        raise BoundaryError("The area's coordinates could not be read.") from exc
    if polygon is None:
        raise BoundaryError("The area is empty.")
    return polygon


def to_geojson(geometry: BaseGeometry) -> dict:
    return json.loads(json.dumps(mapping(geometry)))


def from_geojson(value: dict) -> BaseGeometry:
    return shape(value)


def make_filter(geometry: BaseGeometry) -> Callable[[float, float], bool]:
    """`inside(latitude, longitude)` for `geometry`."""
    prepared = prep(geometry)

    def inside(latitude: float, longitude: float) -> bool:
        return prepared.contains(Point(longitude, latitude))

    return inside
