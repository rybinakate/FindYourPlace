import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import networkx as nx
from osmnx import load_graphml, save_graphml
from shapely.geometry import LineString

from hello_gis.walking_graph import (
    CACHE_METADATA,
    GraphStats,
    load_walking_graph,
    validate_walking_graph,
)

BBOX = (-3.6945, 40.4165, -3.6855, 40.4220)


def sample_graph() -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph(crs="EPSG:4326", simplified=True)
    graph.add_node(1, x=-3.69, y=40.42)
    graph.add_node(2, x=-3.68, y=40.42)
    graph.add_node(3, x=-3.67, y=40.42)
    graph.add_edge(
        1,
        2,
        length=1234.5,
        geometry=LineString([(-3.69, 40.42), (-3.685, 40.423), (-3.68, 40.42)]),
    )
    return graph


class WalkingGraphTests(unittest.TestCase):
    def test_validation_keeps_component_and_direction_counts(self) -> None:
        self.assertEqual(validate_walking_graph(sample_graph()), GraphStats(3, 1, 2, 3))

    def test_invalid_lengths_and_crs(self) -> None:
        for length in (None, False, "10", 0, -1, float("inf"), float("nan")):
            with self.subTest(length=length):
                graph = sample_graph()
                graph.edges[1, 2, 0]["length"] = length
                with self.assertRaisesRegex(ValueError, "invalid length"):
                    validate_walking_graph(graph)
        graph = sample_graph()
        graph.graph["crs"] = "EPSG:25830"
        with self.assertRaisesRegex(ValueError, "graph CRS"):
            validate_walking_graph(graph)

    @patch("pyrosm.OSM")
    def test_graphml_roundtrip_and_cache_scope(self, osm: unittest.mock.Mock) -> None:
        osm.return_value.get_graph.side_effect = lambda **_: sample_graph()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pbf = root / "sample.osm.pbf"
            pbf.write_bytes(b"test marker, not parsed")
            cache = root / "cache"
            first = load_walking_graph(pbf, BBOX, cache_dir=cache)
            cached = load_walking_graph(pbf, BBOX, cache_dir=cache)
            osm.assert_called_once()
            self.assertEqual(cached.edges[1, 2, 0]["length"], 1234.5)
            self.assertEqual(
                cached.edges[1, 2, 0]["geometry"], first.edges[1, 2, 0]["geometry"]
            )
            self.assertEqual(validate_walking_graph(cached), GraphStats(3, 1, 2, 3))

            entry = next(cache.glob("*.graphml"))
            wrong_profile = load_graphml(entry)
            metadata = json.loads(wrong_profile.graph[CACHE_METADATA])
            metadata["profile"] = "all"
            wrong_profile.graph[CACHE_METADATA] = json.dumps(metadata, sort_keys=True)
            save_graphml(wrong_profile, filepath=entry)
            repaired = load_walking_graph(pbf, BBOX, cache_dir=cache)
            self.assertEqual(
                json.loads(repaired.graph[CACHE_METADATA])["profile"], "walking"
            )
            self.assertEqual(osm.call_count, 2)

            load_walking_graph(pbf, (-3.695, *BBOX[1:]), cache_dir=cache)
            load_walking_graph(pbf, BBOX, buffer_m=750, cache_dir=cache)
            pbf.write_bytes(b"new snapshot")
            load_walking_graph(pbf, BBOX, cache_dir=cache)
            self.assertEqual(osm.call_count, 5)
            self.assertEqual(len(list(cache.glob("*.graphml"))), 4)
            osm.return_value.get_graph.assert_called_with(
                network_type="walking",
                graph_type="networkx",
                retain_all=True,
                simplify=True,
                osmnx_compatible=True,
            )
