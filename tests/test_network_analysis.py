import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import geopandas as gpd
import networkx as nx
from shapely.geometry import box

from hello_gis import network_analysis

BBOX = (-3.6945, 40.4165, -3.6855, 40.4220)
PBF_PATH = "/local/madrid-latest.osm.pbf"


class NetworkAnalysisTests(unittest.TestCase):
    def test_analysis_passes_local_graph_to_unchanged_park_workflow(self) -> None:
        graph = nx.MultiDiGraph()
        city = gpd.GeoDataFrame(geometry=[box(*BBOX)], crs="EPSG:4326")
        parks = gpd.GeoDataFrame(
            {"name": ["Test park"], "area_hectares": [2.0]},
            geometry=[box(*BBOX)],
            crs="EPSG:4326",
        )
        metrics = dict.fromkeys(
            (
                "total_area_ha",
                "accessible_area_ha",
                "desert_area_ha",
                "accessible_pct",
                "desert_pct",
                "accessibility_index",
            ),
            0.0,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            boundary, parks_path, legacy = (
                root / "boundary.geojson",
                root / "parks.geojson",
                root / "walk_network.graphml",
            )
            for path in (boundary, parks_path, legacy):
                path.touch()
            graph_cache = root / "walking_graph"
            with (
                patch.dict("os.environ", {"OSM_PBF_PATH": PBF_PATH}),
                patch.multiple(
                    network_analysis.cfg,
                    CITY_BOUNDARY_PATH=boundary,
                    PARKS_RAW_PATH=parks_path,
                    NETWORK_CACHE_PATH=legacy,
                    WALKING_GRAPH_CACHE_DIR=graph_cache,
                ),
                patch.object(
                    network_analysis.d.gpd, "read_file", side_effect=[parks, city]
                ),
                patch.object(
                    network_analysis, "load_walking_graph", return_value=graph
                ) as load,
                patch.object(network_analysis.d.ox, "graph_from_bbox") as overpass,
                patch.object(network_analysis.d.ox, "load_graphml") as old_cache,
                patch.object(
                    network_analysis.d.ox.distance, "add_edge_lengths"
                ) as recalculate,
                patch(
                    "requests.sessions.Session.request",
                    side_effect=AssertionError("HTTP request"),
                ),
                patch.object(
                    network_analysis,
                    "calculate_accessibility_metrics",
                    return_value=metrics,
                ) as calculate,
                patch.object(network_analysis, "save_park_deserts"),
                patch.object(network_analysis, "plot_accessibility_and_deserts"),
                redirect_stdout(StringIO()),
            ):
                network_analysis.run_network_analysis()
            load.assert_called_once_with(PBF_PATH, BBOX, cache_dir=graph_cache)
            args, kwargs = calculate.call_args
            self.assertIs(args[0], graph)
            self.assertEqual(kwargs, {"max_walk_meters": 800})
            overpass.assert_not_called()
            old_cache.assert_not_called()
            recalculate.assert_not_called()

    def test_missing_pbf_setting_fails_before_reading_boundary(self) -> None:
        with (
            patch.dict("os.environ", {"OSM_PBF_PATH": ""}),
            patch.object(network_analysis.d.gpd, "read_file") as read,
            self.assertRaisesRegex(ValueError, "OSM_PBF_PATH"),
        ):
            network_analysis.get_walk_network()
        read.assert_not_called()
