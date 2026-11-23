"""Build and optionally cache a walking graph from a local OSM PBF."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import networkx as nx

logger = logging.getLogger(__name__)
type Bounds = tuple[float, float, float, float]
GRAPH_CRS = "EPSG:4326"
BUFFER_CRS = "EPSG:25830"
CACHE_METADATA = "walking_graph_metadata"
CACHE_SCHEMA = 1


@dataclass(frozen=True)
class GraphStats:
    nodes: int
    edges: int
    weak_components: int
    strong_components: int


def validate_walking_graph(graph: nx.MultiDiGraph) -> GraphStats:
    """Check routing inputs and count components without dropping any."""
    import networkx as nx
    from pyproj import CRS
    from shapely.geometry import LineString

    if not isinstance(graph, nx.MultiDiGraph) or not graph.number_of_edges():
        raise ValueError("Expected a nonempty directed NetworkX multigraph.")
    if (
        "crs" not in graph.graph
        or CRS.from_user_input(graph.graph["crs"]).to_epsg() != 4326
    ):
        raise ValueError(f"Expected graph CRS {GRAPH_CRS}.")
    for u, v, key, attrs in graph.edges(keys=True, data=True):
        length = attrs.get("length")
        if (
            isinstance(length, bool)
            or not isinstance(length, Real)
            or not math.isfinite(length)
            or length <= 0
        ):
            raise ValueError(
                f"Edge {(u, v, key)} has invalid length in metres: {length!r}"
            )
        geometry = attrs.get("geometry")
        if (
            not isinstance(geometry, LineString)
            or geometry.is_empty
            or not geometry.is_valid
        ):
            raise ValueError(f"Edge {(u, v, key)} has invalid line geometry.")
    return GraphStats(
        graph.number_of_nodes(),
        graph.number_of_edges(),
        nx.number_weakly_connected_components(graph),
        nx.number_strongly_connected_components(graph),
    )


def _buffered_bounds(bbox: Bounds, buffer_m: float) -> Bounds:
    from pyproj import Transformer

    west, south, east, north = bbox
    if not all(map(math.isfinite, bbox)) or not (
        -180 <= west < east <= 180 and -90 < south < north < 90
    ):
        raise ValueError("bbox must be (west, south, east, north) in EPSG:4326.")
    if not math.isfinite(buffer_m) or buffer_m < 0:
        raise ValueError("buffer_m must be finite and nonnegative.")
    if not buffer_m:
        return bbox
    forward = Transformer.from_crs(GRAPH_CRS, BUFFER_CRS, always_xy=True)
    reverse = Transformer.from_crs(BUFFER_CRS, GRAPH_CRS, always_xy=True)
    xmin, ymin, xmax, ymax = forward.transform_bounds(*bbox, errcheck=True)
    return reverse.transform_bounds(
        xmin - buffer_m,
        ymin - buffer_m,
        xmax + buffer_m,
        ymax + buffer_m,
        errcheck=True,
    )


def load_walking_graph(
    pbf_path: str | Path,
    bbox: Bounds,
    *,
    buffer_m: float = 500.0,
    cache_dir: str | Path | None = None,
) -> nx.MultiDiGraph:
    """Load Pyrosm walking routes for a Madrid-area WGS84 bounding rectangle.

    Keep all components, original edge geometries and Pyrosm metre weights.
    With cache_dir set, use a dedicated metadata-keyed local GraphML cache.
    Rebuild an entry if its embedded metadata differs from this request.
    """
    import pyrosm

    path = Path(pbf_path).expanduser().resolve(strict=True)
    if not path.is_file() or not path.name.endswith(".osm.pbf"):
        raise ValueError(f"Expected a local .osm.pbf file: {path}")
    bounds = _buffered_bounds(bbox, buffer_m)
    snapshot = path.stat()
    metadata = json.dumps(
        {
            "schema": CACHE_SCHEMA,
            "profile": "walking",
            "pyrosm_version": pyrosm.__version__,
            "pbf_path": str(path),
            "pbf_size": snapshot.st_size,
            "pbf_mtime_ns": snapshot.st_mtime_ns,
            "bbox": tuple(map(float, bbox)),
            "buffer_m": float(buffer_m),
            "buffered_bbox": bounds,
            "buffer_crs": BUFFER_CRS,
            "graph_crs": GRAPH_CRS,
            "retain_all": True,
            "simplify": True,
            "osmnx_compatible": True,
        },
        sort_keys=True,
    )
    cache_path = None
    if cache_dir is not None:
        from osmnx import load_graphml

        key = hashlib.sha256(metadata.encode()).hexdigest()
        cache_path = Path(cache_dir) / f"pyrosm-walking-{key}.graphml"
        if cache_path.is_file():
            cached = load_graphml(cache_path)
            if cached.graph.get(CACHE_METADATA) == metadata:
                logger.info(
                    "Walking graph cache hit: %s", validate_walking_graph(cached)
                )
                return cached
            logger.warning("Walking graph cache metadata mismatch: %s", cache_path)

    graph = pyrosm.OSM(str(path), bounding_box=list(bounds)).get_graph(
        network_type="walking",
        graph_type="networkx",
        retain_all=True,
        simplify=True,
        osmnx_compatible=True,
    )
    stats = validate_walking_graph(graph)
    current = path.stat()
    if (current.st_size, current.st_mtime_ns) != (
        snapshot.st_size,
        snapshot.st_mtime_ns,
    ):
        raise RuntimeError(f"Input PBF changed during graph construction: {path}")
    graph.graph[CACHE_METADATA] = metadata

    if cache_path is not None:
        from osmnx import save_graphml

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=cache_path.parent) as temporary:
            pending = Path(temporary) / cache_path.name
            save_graphml(graph, filepath=pending)
            pending.replace(cache_path)
    logger.info("Walking graph built: %s", stats)
    return graph
