"""Compare OSMnx profiles on the northwestern corner of Madrid's Retiro park.

The area covers the Puerta de la Independencia approach from Calle de Alcalá
and paths inside the park. The coordinates are fixed test anchors. Verify
the approximate entrance against the map before interpreting accessibility.
"""

import argparse
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BBOX = (-3.6945, 40.4165, -3.6855, 40.4220)  # west, south, east, north
ORIGIN = (-3.6920, 40.4196)  # lon, lat, near Calle de Alcalá
PARK_ENTRANCE = (-3.68855, 40.41943)  # lon, lat, approximate park gate
CRS_LOCAL = "EPSG:25830"
# This experiment ID identifies the fixed bbox and graph construction options.
CACHE_ROOT = Path(__file__).resolve().parents[1] / "cache" / "osmnx_retiro_v1"


def diagnose(profile: str, *, allow_overpass: bool) -> bool:
    """Report graph validation and routes independently for one profile."""
    stage = "cache check"
    try:
        profile_dir = CACHE_ROOT / profile
        graph_path = profile_dir / "graph.graphml"
        from_cache = graph_path.is_file()
        if not from_cache and not allow_overpass:
            raise FileNotFoundError(
                f"{graph_path} is missing. An approved run with "
                "--allow-overpass is required to build it."
            )

        stage = "dependency import"
        import networkx as nx
        import osmnx as ox
        from pyproj import Transformer

        if from_cache:
            stage = "graph cache load"
            logger.info("[%s] loading %s", profile, graph_path)
            graph = ox.load_graphml(graph_path)
            if graph.graph.get("diagnostic_profile") != profile:
                raise ValueError(
                    "GraphML profile metadata does not match the requested profile."
                )
        else:
            stage = "graph construction / Overpass"
            # A response hook runs before OSMnx can retry HTTP 429 or 504.
            ox.settings.requests_kwargs = {
                "allow_redirects": False,
                "hooks": {
                    "response": lambda response, **_: response.raise_for_status()
                },
            }
            ox.settings.overpass_rate_limit = False  # Avoid repeated /status polls.
            ox.settings.use_cache = True
            ox.settings.cache_folder = profile_dir / "overpass"
            logger.info("[%s] building graph with OSMnx %s", profile, ox.__version__)
            graph = ox.graph_from_bbox(
                BBOX, network_type=profile, simplify=True, retain_all=True
            )
            graph.graph["diagnostic_profile"] = profile

        stage = "graph validation"
        weak_sizes = sorted(
            map(len, nx.weakly_connected_components(graph)), reverse=True
        )
        logger.info(
            "[%s] nodes=%d edges=%d weak_components=%d largest_weak_sizes=%s "
            "strong_components=%d",
            profile,
            graph.number_of_nodes(),
            graph.number_of_edges(),
            len(weak_sizes),
            weak_sizes[:5],
            nx.number_strongly_connected_components(graph),
        )
        if not graph.number_of_edges():
            raise ValueError("Graph has no edges.")
        for start, end, key, attributes in graph.edges(keys=True, data=True):
            length = attributes.get("length")
            if (
                not isinstance(length, (int, float))
                or not math.isfinite(length)
                or length <= 0
            ):
                raise ValueError(
                    f"Edge {(start, end, key)} has invalid length: {length!r}"
                )
        logger.info("[%s] all edge lengths are finite and positive (m)", profile)

        if not from_cache:
            stage = "graph cache save"
            profile_dir.mkdir(parents=True, exist_ok=True)
            ox.save_graphml(graph, filepath=graph_path)

        stage = "projection and snapping"
        projected = ox.project_graph(graph, to_crs=CRS_LOCAL)
        transformer = Transformer.from_crs("EPSG:4326", CRS_LOCAL, always_xy=True)
        origin_x, origin_y = transformer.transform(*ORIGIN, errcheck=True)
        entrance_x, entrance_y = transformer.transform(*PARK_ENTRANCE, errcheck=True)
        origin_node, origin_offset = ox.distance.nearest_nodes(
            projected, X=origin_x, Y=origin_y, return_dist=True
        )
        entrance_node, entrance_offset = ox.distance.nearest_nodes(
            projected, X=entrance_x, Y=entrance_y, return_dist=True
        )
        logger.info(
            "[%s] origin_node=%s snap=%.2f m; entrance_node=%s snap=%.2f m",
            profile,
            origin_node,
            origin_offset,
            entrance_node,
            entrance_offset,
        )
        if origin_node == entrance_node:
            logger.warning("[%s] both points snap to the same node", profile)

        for direction, source, target in (
            ("origin to park", origin_node, entrance_node),
            ("park to origin", entrance_node, origin_node),
        ):
            stage = f"routing ({direction})"
            try:
                distance, path = nx.single_source_dijkstra(
                    projected, source, target=target, weight="length"
                )
            except nx.NetworkXNoPath:
                logger.exception("[%s] stage=%s: unreachable", profile, stage)
            else:
                logger.info(
                    "[%s] stage=%s: reachable, network length=%.2f m, path=%s",
                    profile,
                    stage,
                    distance,
                    path,
                )
    except Exception:
        logger.exception("[%s] stage=%s: failed", profile, stage)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-overpass",
        action="store_true",
        help="Allow a request to Overpass when a profile's GraphML cache is missing.",
    )
    args = parser.parse_args()

    try:
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        log_path = CACHE_ROOT / f"run-{timestamp}.log"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(log_path, encoding="utf-8"),
            ],
        )
    except OSError:
        logger.exception("stage=log setup: failed")
        return 1

    logger.info(
        "log=%s bbox=%s origin=%s park_entrance=%s",
        log_path,
        BBOX,
        ORIGIN,
        PARK_ENTRANCE,
    )
    results = [
        diagnose(profile, allow_overpass=args.allow_overpass)
        for profile in ("all", "walk")
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
