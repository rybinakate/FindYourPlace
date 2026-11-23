#!/usr/bin/env python3
"""Diagnose Pyrosm's walking profile on a small Retiro extract from a local PBF."""

from __future__ import annotations

import argparse
import logging
import math
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import networkx as nx
    from pyproj import Transformer
    from pyrosm import OSM

logger = logging.getLogger(__name__)
BBOX = (-3.6945, 40.4165, -3.6855, 40.4220)  # west, south, east, north
ORIGIN = (-3.6920, 40.4196)  # lon, lat
PARK_ENTRANCE = (-3.68855, 40.41943)  # approximate location
BUFFER_M = 500
CRS_LOCAL = "EPSG:25830"
REPORT_DIR = Path(__file__).resolve().parents[1] / "cache" / "pyrosm_retiro"


def audit_quality(
    osm: OSM,
    graph: nx.MultiDiGraph,
    expanded: nx.MultiDiGraph,
    bbox: tuple[float, float, float, float],
    to_metric: Transformer,
    path: list[int],
) -> None:
    """Compare component membership and inspect raw PBF features around the route."""
    import networkx as nx
    import numpy as np
    import pandas as pd
    from numpy.typing import NDArray
    from shapely import transform, unary_union
    from shapely.geometry import LineString, Point, box

    def project_xy(coords: NDArray[np.float64]) -> NDArray[np.float64]:
        x, y = to_metric.transform(coords[:, 0], coords[:, 1], errcheck=True)
        return np.column_stack((x, y))

    components = sorted(nx.weakly_connected_components(graph), key=len, reverse=True)
    expanded_labels = {
        node: label
        for label, component in enumerate(nx.weakly_connected_components(expanded))
        for node in component
    }
    main_labels = {expanded_labels[n] for n in components[0] if n in expanded_labels}
    boundary = transform(box(*bbox), project_xy).boundary
    way_ids = set()
    for _, _, attrs in graph.edges(data=True):
        osmid = attrs["osmid"]
        way_ids.update(osmid if isinstance(osmid, list) else [osmid])

    for index, nodes in enumerate(components, start=1):
        subgraph = graph.subgraph(nodes)
        points = [Point(graph.nodes[n]["x"], graph.nodes[n]["y"]) for n in nodes]
        footprint = unary_union(points)
        representative = min(points, key=footprint.centroid.distance)
        labels = {expanded_labels[n] for n in nodes if n in expanded_labels}
        ways = sorted({str(a["osmid"]) for _, _, a in subgraph.edges(data=True)})
        logger.info(
            "component=%02d nodes=%d arcs=%d point=(%.6f,%.6f) bounds=%s "
            "boundary_m=%.1f shared_nodes=%d joins_main_at_750m=%s way_samples=%s",
            index,
            len(nodes),
            subgraph.number_of_edges(),
            representative.x,
            representative.y,
            tuple(round(v, 6) for v in footprint.bounds),
            transform(footprint, project_xy).distance(boundary),
            sum(n in expanded_labels for n in nodes),
            bool(labels & main_labels) if labels else "unknown",
            ways[:4],
        )

    filters = {
        "foot_no": '["highway"]["foot"="no"]',
        "private": '["highway"]["access"="private"]',
        "sidewalk_separate": '["highway"]["sidewalk"="separate"]',
        "sidewalk_left": '["highway"]["sidewalk:left"="separate"]',
        "sidewalk_right": '["highway"]["sidewalk:right"="separate"]',
        "sidewalk_both": '["highway"]["sidewalk:both"="separate"]',
        "mapped_sidewalk": '["footway"="sidewalk"]',
        "crossing_node": '["highway"="crossing"]',
        "crossing_way": '["footway"="crossing"]',
        "park_path": '["highway"~"^(footway|path|steps)$"]',
        "barrier": '["barrier"]',
        "highway": '["highway"]',
        "restricted": '["access"~"^(no|private)$"]',
        "foot_restricted": '["foot"~"^(no|private)$"]',
    }
    tags = [
        "highway",
        "foot",
        "access",
        "sidewalk",
        "sidewalk:left",
        "sidewalk:right",
        "sidewalk:both",
        "footway",
        "crossing",
        "barrier",
        "name",
        "locked",
        "layer",
    ]
    features = osm.get_data_by_custom_criteria(
        custom_filter=list(filters.values()), tags_as_columns=tags, keep_relations=False
    )
    if features is None or features.empty:
        raise ValueError("Raw OSM tag query returned no features.")
    features = features[features.geometry.notna() & ~features.geometry.is_empty].copy()
    notes = {
        "foot_no": features["foot"].eq("no") & features["highway"].notna(),
        "private": features["access"].eq("private") & features["highway"].notna(),
        "mapped_sidewalk": features["footway"].eq("sidewalk"),
        "crossing_node": features["highway"].eq("crossing"),
        "crossing_way": features["footway"].eq("crossing"),
        "park_path": features["highway"].isin(["footway", "path", "steps"])
        & features.geometry.intersects(box(-3.6878, 40.4175, -3.6857, 40.4192)),
    }
    for tag in ("sidewalk", "sidewalk:left", "sidewalk:right", "sidewalk:both"):
        if tag in features:
            notes[tag] = features[tag].eq("separate") & features["highway"].notna()

    def report_tags(row: pd.Series) -> dict[str, str]:
        return {tag: str(row[tag]) for tag in tags if tag in row and pd.notna(row[tag])}

    for category, mask in notes.items():
        selection = features[mask]
        present = (
            selection.apply(
                lambda row: (
                    row["id"] in (graph if row["osm_type"] == "node" else way_ids)
                ),
                axis=1,
            )
            if not selection.empty
            else pd.Series(dtype=bool)
        )
        logger.info(
            "category=%s raw=%d graph_ids=%d", category, len(selection), present.sum()
        )
        for included in (True, False):
            if selection.empty:
                continue
            for _, row in (
                selection[present == included].sort_values("id").head(3).iterrows()
            ):
                logger.info(
                    "sample category=%s osm=%s/%s graph_id=%s tags=%s",
                    category,
                    row["osm_type"],
                    row["id"],
                    included,
                    report_tags(row),
                )

    for index, nodes in enumerate(components[1:], start=2):
        ids = set()
        for _, _, attrs in graph.subgraph(nodes).edges(data=True):
            osmid = attrs["osmid"]
            ids.update(osmid if isinstance(osmid, list) else [osmid])
        ways = features[(features["osm_type"] == "way") & features["id"].isin(ids)]
        logger.info(
            "component=%02d raw_ways=%s",
            index,
            [(row["id"], report_tags(row)) for _, row in ways.iterrows()],
        )

    if not path:
        logger.warning("No forward route for the barrier overlay.")
        return
    route_edges = [
        min(graph[u][v].values(), key=lambda attrs: attrs["length"])
        for u, v in pairwise(path)
    ]
    logger.info("route_osm_way_ids=%s", [edge["osmid"] for edge in route_edges])
    route = unary_union([attrs["geometry"] for attrs in route_edges])
    endpoints = [
        (graph.nodes[n]["x"], graph.nodes[n]["y"]) for n in (path[0], path[-1])
    ]
    connectors = [
        LineString([anchor, endpoint])
        for anchor, endpoint in zip((ORIGIN, PARK_ENTRANCE), endpoints, strict=True)
    ]
    barriers = features[features["barrier"].notna()].to_crs(CRS_LOCAL)
    restricted_areas = features[
        features.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        & (
            features["access"].isin(["no", "private"])
            | features["foot"].isin(["no", "private"])
        )
    ].to_crs(CRS_LOCAL)
    logger.info(
        "mapped_barriers=%d restricted_areas=%d", len(barriers), len(restricted_areas)
    )
    metric_entrance = transform(Point(PARK_ENTRANCE), project_xy)
    for _, row in barriers[
        barriers.geometry.distance(metric_entrance) <= 25
    ].iterrows():
        logger.info(
            "barrier_near_anchor osm=%s/%s distance_m=%.2f tags=%s",
            row["osm_type"],
            row["id"],
            row.geometry.distance(metric_entrance),
            report_tags(row),
        )
    for label, geometry in zip(
        ("network_route", "origin_snap", "entrance_snap"),
        (route, *connectors),
        strict=True,
    ):
        metric = transform(geometry, project_xy)
        hits = []
        for _, row in barriers.iterrows():
            barrier = row.geometry
            if barrier.geom_type in {"Polygon", "MultiPolygon"}:
                barrier = barrier.boundary
            if metric.intersects(barrier):
                hits.append((row["osm_type"], row["id"], report_tags(row)))
        logger.info("barrier_intersections segment=%s hits=%s", label, hits)
        restricted_hits = restricted_areas[restricted_areas.geometry.intersects(metric)]
        logger.info(
            "restricted_area_intersections segment=%s hits=%s",
            label,
            [
                (row["osm_type"], row["id"], report_tags(row))
                for _, row in restricted_hits.iterrows()
            ],
        )


def diagnose(pbf: Path, *, audit: bool) -> int:
    stage = "local input"
    try:
        if not pbf.is_file() or not pbf.name.endswith(".osm.pbf"):
            raise ValueError(f"Expected a local .osm.pbf file: {pbf}")

        stage = "dependency import"
        import networkx as nx
        import pyrosm
        from pyproj import CRS, Geod, Transformer
        from scipy.spatial import cKDTree
        from shapely.geometry import LineString

        stage = "buffered bounding box"
        to_metric = Transformer.from_crs("EPSG:4326", CRS_LOCAL, always_xy=True)
        to_geographic = Transformer.from_crs(CRS_LOCAL, "EPSG:4326", always_xy=True)
        west, south, east, north = to_metric.transform_bounds(*BBOX, errcheck=True)

        def buffered_bounds(metres: int) -> tuple[float, float, float, float]:
            return to_geographic.transform_bounds(
                west - metres,
                south - metres,
                east + metres,
                north + metres,
                errcheck=True,
            )

        buffered_bbox = buffered_bounds(BUFFER_M)
        logger.info(
            "pbf=%s bytes=%d pyrosm=%s networkx=%s bbox=%s "
            "buffer_m=%d expanded_bbox=%s origin=%s approximate_entrance=%s",
            pbf,
            pbf.stat().st_size,
            pyrosm.__version__,
            nx.__version__,
            BBOX,
            BUFFER_M,
            buffered_bbox,
            ORIGIN,
            PARK_ENTRANCE,
        )

        stage = "Pyrosm walking graph extraction and conversion"
        osm = pyrosm.OSM(str(pbf), bounding_box=list(buffered_bbox))
        graph = osm.get_graph(
            network_type="walking",
            graph_type="networkx",
            retain_all=True,
            simplify=True,
            osmnx_compatible=True,
        )

        stage = "graph validation"
        if not isinstance(graph, nx.MultiDiGraph) or not graph.number_of_edges():
            raise ValueError("Expected a nonempty directed NetworkX multigraph.")
        crs = CRS.from_user_input(graph.graph["crs"])
        if crs.to_epsg() != 4326:
            raise ValueError(f"Expected EPSG:4326 graph coordinates, found {crs}.")
        logger.info(
            "type=%s crs=%s directed=%s nodes=%d edges=%d weak_components=%d "
            "strong_components=%d",
            type(graph).__name__,
            crs.to_string(),
            graph.is_directed(),
            graph.number_of_nodes(),
            graph.number_of_edges(),
            nx.number_weakly_connected_components(graph),
            nx.number_strongly_connected_components(graph),
        )
        geod = Geod(ellps="WGS84")
        max_relative_error = 0.0
        arcs_without_reverse = 0
        for u, v, key, attrs in graph.edges(keys=True, data=True):
            length = attrs.get("length")
            geometry = attrs.get("geometry")
            if (
                not isinstance(length, (int, float))
                or not math.isfinite(length)
                or length <= 0
            ):
                raise ValueError(f"Edge {(u, v, key)} has invalid length: {length!r}")
            if (
                not isinstance(geometry, LineString)
                or geometry.is_empty
                or not geometry.is_valid
            ):
                raise ValueError(f"Edge {(u, v, key)} has invalid line geometry.")
            measured = geod.geometry_length(geometry)
            if not math.isfinite(measured) or measured <= 0:
                raise ValueError(f"Edge {(u, v, key)} has invalid geodesic length.")
            max_relative_error = max(max_relative_error, abs(length / measured - 1))
            arcs_without_reverse += not graph.has_edge(v, u)
        logger.info(
            "All edge lengths positive; maximum relative difference from geodesic "
            "polyline length=%.3f%%; arcs without reverse=%d",
            max_relative_error * 100,
            arcs_without_reverse,
        )

        stage = "projection and nearest-node snapping"
        node_ids = list(graph)
        x, y = to_metric.transform(
            [graph.nodes[node]["x"] for node in node_ids],
            [graph.nodes[node]["y"] for node in node_ids],
            errcheck=True,
        )
        tree = cKDTree(list(zip(x, y, strict=True)))
        origin_offset, origin_index = tree.query(
            to_metric.transform(*ORIGIN, errcheck=True)
        )
        entrance_offset, entrance_index = tree.query(
            to_metric.transform(*PARK_ENTRANCE, errcheck=True)
        )
        origin, entrance = node_ids[origin_index], node_ids[entrance_index]
        logger.info(
            "origin_node=%s snap_m=%.2f approximate_entrance_node=%s snap_m=%.2f",
            origin,
            origin_offset,
            entrance,
            entrance_offset,
        )
        if origin == entrance:
            logger.warning("Both coordinates snap to the same node.")

        forward_path: list[int] = []
        for direction, source, target in (
            ("origin -> approximate entrance", origin, entrance),
            ("approximate entrance -> origin", entrance, origin),
        ):
            stage = f"routing {direction}"
            try:
                length, path = nx.single_source_dijkstra(
                    graph, source, target=target, weight="length"
                )
            except nx.NetworkXNoPath:
                logger.exception("stage=%s: unreachable", stage)
            else:
                logger.info("%s length_m=%.2f nodes=%s", direction, length, path)
                if source == origin and target == entrance:
                    forward_path = path
        if audit:
            stage = "expanded local bounding box comparison"
            expanded = pyrosm.OSM(
                str(pbf), bounding_box=list(buffered_bounds(750))
            ).get_graph(
                network_type="walking",
                graph_type="networkx",
                retain_all=True,
                simplify=True,
                osmnx_compatible=True,
            )
            stage = "quality audit"
            audit_quality(osm, graph, expanded, buffered_bbox, to_metric, forward_path)
    except Exception:
        logger.exception("stage=%s: failed", stage)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pbf", type=Path, required=True, help="Path to a local OSM PBF"
    )
    parser.add_argument(
        "--audit", action="store_true", help="Audit local components and OSM tags"
    )
    args = parser.parse_args()

    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        report = REPORT_DIR / f"run-{timestamp}.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(report, encoding="utf-8"),
            ],
        )
    except OSError:
        logger.exception("stage=report setup: failed")
        return 1

    logger.info("report=%s", report)
    return diagnose(args.pbf.expanduser().absolute(), audit=args.audit)


if __name__ == "__main__":
    raise SystemExit(main())
