import unittest

import geopandas as gpd
import h3
from shapely.geometry import Point, box

from hello_gis.h3_grid import build_h3_grid, classify_residential_cells


class H3GridTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cells = gpd.GeoDataFrame(
            {"h3_id": ["a", "b", "c", "d"]},
            geometry=[box(x, 0, x + 10, 10) for x in (0, 10, 20, 30)],
            crs="EPSG:25830",
        )
        self.area = gpd.GeoDataFrame(geometry=[box(0, 0, 34, 10)], crs=self.cells.crs)
        self.buildings = gpd.GeoDataFrame(
            {"building": ["apartments", "yes", "retail", "house", "yes"]},
            geometry=[
                box(0, 0, 2, 5),
                box(8, 0, 12, 5),
                box(20, 0, 25, 5),
                box(30, 0, 33, 3),
                box(33, 0, 38, 4),
            ],
            crs=self.cells.crs,
        )

    def test_status_threshold_precedence_and_study_clipping(self) -> None:
        result = classify_residential_cells(self.cells, self.buildings, self.area)
        self.assertEqual(
            result.residential_status.tolist(),
            [
                "confirmed",
                "uncertain",
                "excluded",
                "excluded",
            ],
        )
        self.assertEqual(
            result.has_uncertain_building.tolist(), [True, True, False, False]
        )
        self.assertEqual(
            result.has_confirmed_building.tolist(), [True, False, False, False]
        )
        self.assertEqual(result.crs, self.cells.crs)
        self.assertEqual(
            classify_residential_cells(
                self.cells, self.buildings, self.area, min_overlap_m2=9
            ).residential_status.tolist(),
            ["confirmed", "uncertain", "excluded", "confirmed"],
        )

    def test_duplicate_building_index_and_no_residential_candidates(self) -> None:
        buildings = self.buildings.copy()
        buildings.index = [0] * len(buildings)
        result = classify_residential_cells(self.cells, buildings, self.area)
        self.assertEqual(
            result.residential_status.tolist(),
            [
                "confirmed",
                "uncertain",
                "excluded",
                "excluded",
            ],
        )
        result = classify_residential_cells(
            self.cells, buildings[buildings.building == "retail"], self.area
        )
        self.assertEqual(result.residential_status.tolist(), ["excluded"] * 4)

    def test_invalid_crs_geometry_and_threshold(self) -> None:
        missing_crs = self.buildings.set_crs(None, allow_override=True)
        with self.assertRaisesRegex(ValueError, "CRS"):
            classify_residential_cells(self.cells, missing_crs, self.area)
        point = gpd.GeoDataFrame(
            {"building": ["house"]}, geometry=[Point(1, 1)], crs=self.cells.crs
        )
        with self.assertRaisesRegex(ValueError, "polygon"):
            classify_residential_cells(self.cells, point, self.area)
        with self.assertRaisesRegex(ValueError, "min_overlap"):
            classify_residential_cells(
                self.cells, self.buildings, self.area, min_overlap_m2=0
            )

    def test_real_h3_centers_are_inside_small_control_window(self) -> None:
        window = box(-3.6915, 40.4190, -3.6905, 40.4200)
        area = gpd.GeoDataFrame(geometry=[window], crs="EPSG:4326")
        grid = build_h3_grid(area)
        self.assertFalse(grid.empty)
        self.assertEqual(grid.crs.to_epsg(), 4326)
        self.assertTrue(grid.h3_id.is_unique)
        for row in grid.itertuples():
            self.assertEqual(h3.get_resolution(row.h3_id), 11)
            center = Point(row.center_lon, row.center_lat)
            self.assertTrue(window.covers(center))
            self.assertTrue(row.geometry.covers(center))

    def test_real_cell_classifies_uncertain_building_at_area_threshold(self) -> None:
        area = gpd.GeoDataFrame(
            geometry=[box(-3.6915, 40.4190, -3.6905, 40.4200)], crs="EPSG:4326"
        )
        grid = build_h3_grid(area)
        cell = grid.iloc[[len(grid) // 2]]
        metric_cell = cell.to_crs(25830).geometry.iloc[0]
        study = area.to_crs(25830).geometry.iloc[0]
        point = metric_cell.intersection(study).representative_point()
        footprint = box(point.x - 2, point.y - 2, point.x + 2, point.y + 2)
        self.assertTrue(metric_cell.intersection(study).covers(footprint))
        buildings = gpd.GeoDataFrame(
            {"building": ["yes"]}, geometry=[footprint], crs="EPSG:25830"
        )
        result = classify_residential_cells(cell, buildings, area, min_overlap_m2=16)
        self.assertEqual(result.residential_status.tolist(), ["uncertain"])
        self.assertEqual(
            classify_residential_cells(
                cell, buildings, area, min_overlap_m2=16.1
            ).residential_status.tolist(),
            ["excluded"],
        )
