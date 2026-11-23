"""Test snapping on a small synthetic graph with known distances.

The fixture sets node and cell-center coordinates. Tests verify distances,
components, preservation of residential statuses, and handling of invalid inputs.
"""

import unittest

import geopandas as gpd
import networkx as nx
from pyproj import Transformer
from shapely.geometry import Point

from hello_gis.h3_snapping import snap_residential_origins


class H3SnappingTests(unittest.TestCase):
    """Verify snapping results without loading the citywide graph."""

    def setUp(self) -> None:
        """Prepare cells and a graph with known distances for each test."""
        # Define offsets in meters, then transform centers to the input H3 layer's CRS.
        self.x, self.y = 441000, 4474500
        geographic = Transformer.from_crs(25830, 4326, always_xy=True)
        lon, lat = geographic.transform(
            [self.x + 3, self.x + 101, self.x], [self.y + 4, self.y, self.y]
        )
        self.cells = gpd.GeoDataFrame(
            {
                "h3_id": ["a", "b", "c"],
                "center_lon": lon,
                "center_lat": lat,
                "residential_status": ["confirmed", "uncertain", "excluded"],
            },
            geometry=[Point(x, y) for x, y in zip(lon, lat, strict=True)],
            crs="EPSG:4326",
            index=[7, 7, 8],
        )
        self.graph = nx.MultiDiGraph(crs="EPSG:25830")
        self.graph.add_node(8, x=self.x, y=self.y)
        # The node belongs to a component even though it has no coordinates for snapping.
        self.graph.add_node(3)
        self.graph.add_node(10, x=self.x + 100, y=self.y)
        self.graph.add_edge(3, 8)

    def test_distances_components_and_statuses(self) -> None:
        """Verify known distances, components, and the original residential statuses."""
        result = snap_residential_origins(self.cells, self.graph)
        self.assertEqual(result.h3_id.tolist(), ["a", "b"])
        self.assertEqual(result.residential_status.tolist(), ["confirmed", "uncertain"])
        self.assertEqual(result.origin_node_id.tolist(), [8, 10])
        self.assertEqual(result.origin_component_id.tolist(), [3, 10])
        self.assertAlmostEqual(result.origin_snap_distance_m.iloc[0], 5)
        self.assertAlmostEqual(result.origin_snap_distance_m.iloc[1], 1)
        self.assertEqual(result.origin_snap_quality.tolist(), ["candidate"] * 2)
        self.assertEqual(
            result.origin_snap_flag.tolist(), ["requires_access_check"] * 2
        )

    def test_rejects_duplicate_ids(self) -> None:
        """Reject duplicate H3 IDs before searching for nodes."""
        cells = self.cells.assign(h3_id=["a", "a", "c"])
        with self.assertRaisesRegex(ValueError, "unique"):
            snap_residential_origins(cells, self.graph)

    def test_missing_coordinates_and_empty_graph(self) -> None:
        """Verify why synthetic inputs can remain unmatched."""
        self.cells.iloc[1, self.cells.columns.get_loc("center_lat")] = float("nan")
        no_coordinates = nx.MultiDiGraph(crs="EPSG:25830")
        no_coordinates.add_node(3)
        for graph in (nx.MultiDiGraph(), no_coordinates):
            with self.subTest(nodes=graph.number_of_nodes()):
                result = snap_residential_origins(self.cells, graph)
                self.assertTrue(result.origin_node_id.isna().all())
                self.assertTrue(result.origin_component_id.isna().all())
                self.assertTrue(result.origin_snap_distance_m.isna().all())
                self.assertEqual(result.origin_snap_quality.tolist(), ["unmatched"] * 2)
                self.assertEqual(
                    result.origin_snap_flag.tolist(),
                    ["no_usable_nodes", "invalid_center"],
                )
