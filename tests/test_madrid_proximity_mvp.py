"""Exercise the portfolio scoring workflow using synthetic metric geometries."""

import math

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from scripts import build_madrid_proximity_mvp as mvp


@pytest.mark.parametrize(
    ("facility", "center", "expected"),
    [
        (Point(3, 4), Point(0, 0), 5),
        (box(10, 10, 30, 30), Point(5, 20), 5),
        (box(10, 10, 30, 30), Point(15, 15), 0),
    ],
    ids=["point", "polygon-edge", "inside-polygon"],
)
def test_nearest_distance(facility, center, expected):
    distances = mvp.nearest_distances(
        np.array([center]), np.array([Point(100, 100), facility])
    )
    assert distances.tolist() == [expected]


@pytest.fixture
def pharmacy():
    """Provide a synthetic OSM pharmacy point within a metric study extent."""
    return gpd.GeoDataFrame(
        {
            "osm_type": ["node"],
            "id": [1],
            "name": ["Pharmacy"],
            "amenity": ["pharmacy"],
        },
        geometry=[Point(440_000, 4_475_000)],
        crs=mvp.METRIC,
    )


def test_category_layer_projects_to_meters(pharmacy):
    city = box(439_900, 4_474_900, 440_100, 4_475_100)
    layer, _ = mvp.category_layer(pharmacy.to_crs(4326), {"amenity": "pharmacy"}, city)

    assert layer.crs.to_epsg() == 25830
    distances = mvp.nearest_distances(
        np.array([Point(440_003, 4_475_004)]), layer.geometry.to_numpy()
    )
    np.testing.assert_allclose(distances, [5], atol=1e-7, rtol=0)


def test_category_layer_requires_input_crs(pharmacy):
    city = box(439_900, 4_474_900, 440_100, 4_475_100)
    without_crs = pharmacy.set_crs(None, allow_override=True)

    with pytest.raises(ValueError, match="naive geometries"):
        mvp.category_layer(without_crs, {"amenity": "pharmacy"}, city)


def test_category_layer_rejects_missing_facilities(pharmacy):
    with pytest.raises(ValueError, match="No valid facilities remain"):
        mvp.category_layer(
            pharmacy, {"amenity": "school"}, box(439_900, 4_474_900, 440_100, 4_475_100)
        )


@pytest.mark.parametrize("facility", [None, Point()], ids=["missing", "empty"])
def test_nearest_distance_rejects_unusable_facilities(facility):
    with pytest.raises(ValueError, match="Could not measure distances"):
        mvp.nearest_distances(np.array([Point(0, 0)]), np.array([facility]))


def test_score_class_boundaries():
    inputs = np.array([0, 19.999, 20, 39.999, 40, 59.999, 60, 79.999, 80, 100])
    assert mvp.classify_scores(inputs).tolist() == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


def test_residential_scoring_and_determinism():
    grid = gpd.GeoDataFrame(
        {"residential_status": ["confirmed", "excluded", "uncertain"]},
        geometry=[box(-1, -1, 1, 1), box(4, -1, 6, 1), box(9, -1, 11, 1)],
        crs=mvp.METRIC,
        index=[4, 2, 7],
    )
    residential = grid.residential_status.isin(["confirmed", "uncertain"])
    centers = np.array([Point(0, 0), Point(10, 0)])
    scales = {
        "park": 500,
        "pharmacy": 400,
        "school": 700,
        "kindergarten": 600,
        "hospital": 1500,
    }
    layers = {
        key: gpd.GeoDataFrame(geometry=[Point(-scale, 0)], crs=mvp.METRIC)
        for key, scale in scales.items()
    }
    qa = {key: {} for key in layers}
    result, composite, classes, checks = mvp.score_residential_cells(
        grid, residential, centers, layers, qa
    )

    score_columns = [column for column in result if column not in grid]
    assert result.loc[2, score_columns].isna().all()
    pd.testing.assert_series_equal(result.residential_status, grid.residential_status)
    for key, scale in scales.items():
        np.testing.assert_allclose(
            result.loc[residential, f"{key}_distance_m"], [scale, scale + 10]
        )
        np.testing.assert_allclose(
            result.loc[residential, f"{key}_score"],
            [100 / math.e, 100 * math.exp(-(scale + 10) / scale)],
        )

    expected = [
        100 / math.e,
        sum(100 * math.exp(-(d + 10) / d) for d in scales.values()) / 5,
    ]
    np.testing.assert_allclose(composite, expected)
    np.testing.assert_array_equal(
        result.loc[residential, "integrated_score"], composite
    )
    assert classes.tolist() == [2, 2]
    assert checks == 40

    other_qa = {key: {} for key in layers}
    repeated, _, _, _ = mvp.score_residential_cells(
        grid, residential, centers, layers, other_qa
    )
    pd.testing.assert_frame_equal(result, repeated, check_exact=True)
    assert qa == other_qa
