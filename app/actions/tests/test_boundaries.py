import json

import pytest

from app.actions import boundaries as b

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
SQUARE2 = {"type": "Polygon", "coordinates": [[[2, 2], [2, 3], [3, 3], [3, 2], [2, 2]]]}
BOWTIE = {"type": "Polygon", "coordinates": [[[5, 5], [6, 6], [6, 5], [5, 6], [5, 5]]]}
LINE = {"type": "LineString", "coordinates": [[0, 0], [9, 9]]}


def fc(geom):
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geom}]}


def test_group_unions_features():
    inside = b.make_filter(b.geometry_from_feature_group({"features": [fc(SQUARE), fc(SQUARE2)]}))
    assert inside(0.5, 0.5) and inside(2.5, 2.5) and not inside(1.5, 1.5)


def test_filter_argument_order_is_lat_lon():
    wide = {"type": "Polygon", "coordinates": [[[30, 0], [30, 1], [40, 1], [40, 0], [30, 0]]]}
    inside = b.make_filter(b.geometry_from_polygon(wide))
    assert inside(0.5, 35.0) and not inside(35.0, 0.5)


def test_invalid_feature_is_repaired_and_lines_ignored():
    geom = b.geometry_from_feature_group(
        {"features": [fc(BOWTIE), fc(LINE), {"type": "Feature", "geometry": SQUARE}]}
    )
    inside = b.make_filter(geom)
    assert geom.is_valid and inside(0.5, 0.5) and inside(5.25, 5.9)
    assert not inside(4.0, 4.0)  # on the line, which is not part of the boundary


def test_multipolygon_feature_is_kept():
    multi = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"], SQUARE2["coordinates"]]}
    inside = b.make_filter(b.geometry_from_feature_group({"features": [fc(multi)]}))
    assert inside(0.5, 0.5) and inside(2.5, 2.5)


@pytest.mark.parametrize(
    "group",
    [{}, {"features": []}, {"features": [fc(LINE)]}, {"features": [fc(None)]}, {"features": ["junk", None]}, None],
)
def test_group_without_usable_polygons_raises(group):
    with pytest.raises(b.BoundaryError):
        b.geometry_from_feature_group(group)


def test_polygon_from_dict_or_string():
    assert b.geometry_from_polygon(SQUARE).equals(b.geometry_from_polygon(json.dumps(SQUARE)))


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not json",
        "[1,2,3,4]",
        {"type": "Point", "coordinates": [0, 0]},
        {"typed_edges": ["1", "2"]},
        {"type": "Polygon", "coordinates": []},
        None,
    ],
)
def test_polygon_rejects(bad):
    with pytest.raises(b.BoundaryError):
        b.geometry_from_polygon(bad)


def test_union_of_nothing_raises():
    with pytest.raises(b.BoundaryError):
        b.union_geometries([])


def test_geojson_round_trip():
    geom = b.geometry_from_polygon(SQUARE)
    assert b.from_geojson(json.loads(json.dumps(b.to_geojson(geom)))).equals(geom)
