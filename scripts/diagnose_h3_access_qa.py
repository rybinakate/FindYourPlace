"""Audit residential evidence and candidate network connections for eight selected H3 cells.

Inputs: the source H3 grid, existing node snaps, GraphML, and a local PBF.
Outputs: four QA tables and candidate geometries. No routes are computed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import resource
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from xml.etree.ElementTree import iterparse

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pyrosm
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point

from hello_gis.h3_grid import RESIDENTIAL_TYPES

H3_IDS = (
    "8b390cb1b203fff",
    "8b390ca25d68fff",
    "8b390cb20909fff",
    "8b390cb42466fff",
    "8b390ca32c5dfff",
    "8b390cb2831dfff",
    "8b390cb58d80fff",
    "8b390ca21883fff",
)
QA_VERSION = "eight-h3-access-v1"
METRIC = "EPSG:25830"
GEOGRAPHIC = "EPSG:4326"
TAGS = (
    "building",
    "building:use",
    "amenity",
    "man_made",
    "access",
    "foot",
    "access:conditional",
    "foot:conditional",
    "entrance",
    "barrier",
    "highway",
    "footway",
    "crossing",
    "locked",
    "opening_hours",
    "natural",
    "waterway",
    "landuse",
    "military",
    "layer",
    "bridge",
    "tunnel",
    "name",
)
BARRIERS = {"fence", "wall", "hedge", "retaining_wall", "city_wall", "ditch"}
GATES = {"gate", "lift_gate", "swing_gate", "kissing_gate", "stile", "turnstile"}
RESTRICTED = {"no", "private"}


def deny_network(event: str, args: tuple) -> None:
    """Block process-level network events. Event arguments are unused."""
    if event in {
        "socket.connect",
        "socket.sendto",
        "socket.sendmsg",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }:
        raise RuntimeError(f"Offline diagnostics blocked {event}")


def json_text(value: object) -> str:
    """Serialize a record consistently for tables and repeatability checks."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def file_version(path: Path) -> str:
    """Compute the file's SHA-256 hash without loading the large GraphML into memory."""
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def building_evidence(tags: dict[str, str], overlap_m2: float) -> dict:
    """Record a building's contribution to the original and target masks based on overlap area."""
    building = tags.get("building", "").strip().lower()
    original_kind = (
        "confirmed"
        if building in RESIDENTIAL_TYPES
        else "uncertain"
        if building == "yes"
        else "none"
    )
    qualifies = overlap_m2 >= 10 and original_kind != "none"
    nursing_home = tags.get("amenity") == "nursing_home"
    use = {
        "confirmed": "explicit_residential",
        "uncertain": "unspecified_use",
        "none": "insufficient_data",
    }[original_kind]
    review = None
    if nursing_home:
        use, review = "collective_living", "nursing_home_excluded_in_target"
    elif (
        building == "dormitory"
        or tags.get("building:use") == "mixed_use"
        or building == "mixed_use"
    ):
        use, review = (
            "collective_living" if building == "dormitory" else use,
            "methodology_review",
        )
    elif tags.get("man_made") in {"tower", "works", "water_tower"} or tags.get(
        "building:use"
    ) in {"industrial", "commercial", "office", "warehouse"}:
        use, review = "nonresidential_hint", "verify_osm_use_tags"
    return {
        "building_tag": tags.get("building"),
        "use_evidence": use,
        "review_reason": review,
        "original_evidence_kind": original_kind,
        "qualifies_under_current_rule": qualifies,
        "contributes_to_target_mask": qualifies and not nursing_home,
    }


def evidence_status(rows: list[dict], flag: str) -> tuple[str, bool, bool]:
    """Return the residential status and two independent flags for the selected mask."""
    kinds = {row["original_evidence_kind"] for row in rows if row[flag]}
    confirmed, uncertain = "confirmed" in kinds, "uncertain" in kinds
    return (
        "confirmed" if confirmed else "uncertain" if uncertain else "excluded",
        confirmed,
        uncertain,
    )


def graph_batches(path: Path, topology: nx.Graph, xy: dict):
    """Stream GraphML edges and build the topology used for weak connectivity."""
    ns = "{http://graphml.graphdrawing.org/xmlns}"
    names, stack, batch = {}, [], []
    edge_fields = {"geometry", "osmid", "tags", *TAGS}
    for event, item in iterparse(path, events=("start", "end")):
        kind = item.tag.removeprefix(ns)
        if event == "start":
            if kind == "graph" and item.get("edgedefault") != "directed":
                raise ValueError("GraphML must contain a directed graph.")
            stack.append(item)
            continue
        stack.pop()
        parent = stack[-1] if stack else None
        if kind == "key":
            names[item.attrib["id"]] = item.attrib["attr.name"]
        elif kind == "node":
            coords = {
                names[child.attrib["key"]]: float(child.text)
                for child in item
                if names[child.attrib["key"]] in {"x", "y"}
            }
            node = int(item.attrib["id"])
            xy[node] = coords["x"], coords["y"]
            topology.add_node(node)
        elif kind == "edge":
            u, v, raw_key = (
                int(item.attrib["source"]),
                int(item.attrib["target"]),
                item.attrib["id"],
            )
            # GraphML stores each key as XML text. Validate this snapshot's keys as int64.
            key = int(raw_key)
            if str(key) != raw_key or not -(2**63) <= key < 2**63:
                raise ValueError(f"Edge key is not a canonical int64: {raw_key}")
            fields = {
                names[child.attrib["key"]]: child.text
                for child in item
                if names[child.attrib["key"]] in edge_fields
            }
            topology.add_edge(u, v)
            batch.append((u, v, key, fields))
        elif kind == "data" and parent is not None and parent.tag == ns + "graph":
            topology.graph[names[item.attrib["key"]]] = item.text
            parent.remove(item)
        if kind in {"key", "node", "edge"}:
            parent.remove(item)
        if len(batch) == 20_000:
            yield batch
            batch = []
    if batch:
        yield batch


def osm_way_ids(value: str | None) -> tuple[int, ...]:
    """Normalize edge OSM IDs, including ID lists produced by simplification."""
    if value is None:
        return ()
    parsed = ast.literal_eval(value)
    return tuple(sorted(set(parsed))) if isinstance(parsed, list) else (int(parsed),)


def nearest_physical_edges(
    path: Path, centers: np.ndarray, radii: np.ndarray
) -> tuple[list[list[dict]], nx.Graph, dict, int]:
    """Return up to three distinct lines for each center, along with graph metadata.

    Batched spatial indexes compare full lines. Opposite directions are grouped
    only when their geometry, endpoints, and set of source OSM way IDs match.
    """
    forward = Transformer.from_crs(GEOGRAPHIC, METRIC, always_xy=True)
    topology, xy = nx.Graph(), {}
    candidates: list[dict] = [{} for _ in centers]
    count = 0

    def project_xy(coords: np.ndarray) -> np.ndarray:
        """Project every line vertex to a metric CRS for spatial indexing."""
        x, y = forward.transform(coords[:, 0], coords[:, 1], errcheck=True)
        return np.column_stack((x, y))

    for batch in graph_batches(path, topology, xy):
        count += len(batch)
        lines = shapely.from_wkt([fields["geometry"] for _, _, _, fields in batch])
        if not (
            shapely.is_valid(lines)
            & ~shapely.is_empty(lines)
            & (shapely.get_type_id(lines) == 1)
        ).all():
            raise ValueError("Citywide GraphML contains an unusable line geometry.")
        projected = shapely.transform(lines, project_xy)
        pairs = shapely.STRtree(projected).query(
            centers, predicate="dwithin", distance=radii
        )
        for cell_index, edge_index in pairs.T:
            u, v, key, fields = batch[edge_index]
            line = projected[edge_index]
            way_ids = osm_way_ids(fields.get("osmid"))
            edge_id = (u, v, key)
            # If the edge's source is unknown, there is insufficient evidence to group it.
            signature = (
                (
                    min(u, v),
                    max(u, v),
                    way_ids,
                    shapely.normalize(lines[edge_index]).wkb,
                )
                if way_ids
                else edge_id
            )
            distance = centers[cell_index].distance(line)
            bucket = candidates[cell_index]
            if signature not in bucket:
                bucket[signature] = {
                    "distance": distance,
                    "arcs": [],
                    "way_ids": way_ids,
                }
            record = bucket[signature]
            record["arcs"].append(edge_id)
            if "edge_id" not in record or edge_id < record["edge_id"]:
                record.update(edge_id=edge_id, line=line, fields=fields)
    if topology.graph["crs"] != GEOGRAPHIC:
        raise ValueError("The prototype requires GraphML in EPSG:4326.")
    ranked = [
        sorted(bucket.values(), key=lambda row: (row["distance"], row["edge_id"]))[:3]
        for bucket in candidates
    ]
    return ranked, topology, xy, count


def osm_ref(feature: pd.Series) -> dict:
    """Return the OSM reference and available tags for traceable checks."""
    return {
        "osm_type": str(feature["osm_type"]),
        "osm_id": int(feature["id"]),
        "tags": {
            tag: str(feature[tag])
            for tag in TAGS
            if tag in feature and pd.notna(feature[tag])
        },
    }


def bbox_with_margin(geometry, margin_m: float, reverse: Transformer) -> list[float]:
    """Return a WGS84 bounding box for a metric extent with a buffer."""
    xmin, ymin, xmax, ymax = geometry.bounds
    return list(
        reverse.transform_bounds(
            xmin - margin_m, ymin - margin_m, xmax + margin_m, ymax + margin_m
        )
    )


def tag_access(tags: dict) -> str:
    """Interpret explicit pedestrian access tags separately from physical passability."""
    foot, access = tags.get("foot"), tags.get("access")
    if (
        tags.get("foot:conditional")
        or tags.get("access:conditional")
        or tags.get("opening_hours")
    ):
        return "conditional"
    if foot in RESTRICTED or tags.get("locked") == "yes":
        return "restricted"
    if foot in {"yes", "designated", "permissive"}:
        return "permitted"
    if foot is not None:
        return "ambiguous"
    if access in RESTRICTED:
        return "restricted"
    if access in {"yes", "designated", "permissive"}:
        return "permitted"
    return "ambiguous" if access is not None else "not_mapped"


def classify_crossings(context: gpd.GeoDataFrame, line) -> list[dict]:
    """Return OSM features intersecting the segment and any available level information."""
    hits = []
    if context.empty:
        return hits
    for _, feature in context.loc[context.intersects(line)].iterrows():
        ref = osm_ref(feature)
        tags = ref["tags"]
        kinds = []
        if tags.get("barrier") in BARRIERS:
            kinds.append("barrier")
        if tags.get("natural") == "water" or tags.get("waterway") in {
            "stream",
            "river",
            "canal",
            "drain",
        }:
            kinds.append("water")
        if (
            tag_access(tags) in {"restricted", "conditional", "ambiguous"}
            or tags.get("landuse") == "military"
        ):
            kinds.append("access_tag")
        if (
            tags.get("entrance")
            or tags.get("barrier") in GATES
            or tags.get("highway") == "crossing"
            or tags.get("footway") == "crossing"
        ):
            kinds.append("entrance_or_crossing")
        if kinds:
            level = {
                key: tags[key] for key in ("layer", "bridge", "tunnel") if key in tags
            }
            hits.append({**ref, "categories": kinds, "level_tags": level})
    return sorted(hits, key=lambda row: (row["osm_type"], row["osm_id"]))


def main() -> None:
    """Read the inputs, check eight H3 cells, and write four tables and spatial layers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = args.out.resolve()
    if not root.is_relative_to(Path("cache").resolve()):
        raise ValueError("The output directory must be inside cache/.")
    root.mkdir(parents=True, exist_ok=False)
    sys.addaudithook(deny_network)
    started = perf_counter()
    checked_at = datetime.now(UTC).isoformat()
    grid_path = Path(
        "cache/h3-madrid-20261122-EggEEb/madrid_h3_r11_residential.geoparquet"
    )
    snap_path = Path(
        "cache/h3-city-snapping-20261123-TvoO3g/madrid_residential_origins.geoparquet"
    )
    report = json.loads(snap_path.with_name("report.json").read_text())
    graph_path = Path(report["inputs"]["graph"])
    source = (
        gpd.read_parquet(grid_path)
        .set_index("h3_id", drop=False)
        .loc[list(H3_IDS)]
        .reset_index(drop=True)
    )
    snaps = (
        gpd.read_parquet(snap_path).set_index("h3_id").loc[list(H3_IDS)].reset_index()
    )
    evidence_flags = [
        "residential_status",
        "has_confirmed_building",
        "has_uncertain_building",
    ]
    pd.testing.assert_frame_equal(source[evidence_flags], snaps[evidence_flags])
    assert source.crs.to_epsg() == 4326 and source.is_valid.all()
    municipal = (
        gpd.read_file("data/reference/city_boundary.geojson")
        .to_crs(METRIC)
        .geometry.union_all()
    )
    source_metric = source.to_crs(METRIC)
    centers = (
        gpd.GeoSeries(
            gpd.points_from_xy(source.center_lon, source.center_lat), crs=GEOGRAPHIC
        )
        .to_crs(METRIC)
        .to_numpy()
    )
    forward = Transformer.from_crs(GEOGRAPHIC, METRIC, always_xy=True)
    reverse = Transformer.from_crs(METRIC, GEOGRAPHIC, always_xy=True)
    radii = snaps.origin_snap_distance_m.to_numpy() + 500
    selected, topology, xy, edge_count = nearest_physical_edges(
        graph_path, centers, radii
    )
    assert edge_count == 878800 and len(xy) == 297604
    meta = json.loads(topology.graph["walking_graph_metadata"])
    assert meta["profile"] == "walking" and meta["retain_all"] is True
    pbf = Path(meta["pbf_path"])
    pbf_state = pbf.stat()
    assert (
        pbf_state.st_size == meta["pbf_size"]
        and pbf_state.st_mtime_ns == meta["pbf_mtime_ns"]
    )
    versions = {
        "h3_version": file_version(grid_path),
        "pbf_version": file_version(pbf),
        "graph_version": file_version(graph_path),
        "qa_version": QA_VERSION,
    }
    # Weak connectivity ignores edge direction.
    components = {}
    sizes = {}
    for nodes in nx.connected_components(topology):
        component_id = min(nodes)
        components.update(dict.fromkeys(nodes, component_id))
        sizes[component_id] = len(nodes)
    assert len(sizes) == 1553
    largest_component = max(sizes, key=sizes.__getitem__)
    print(
        f"Graph loaded and candidates found in {perf_counter() - started:.1f} s.",
        flush=True,
    )

    context_evidence_path = Path(
        "cache/h3-origin-context-20261123-xjy2D9/osm_evidence.json"
    )
    previous_evidence = json.loads(context_evidence_path.read_text())
    versions["context_evidence_version"] = file_version(context_evidence_path)
    cell_rows, building_rows, candidate_rows, check_rows = [], [], [], []
    points, connectors, full_lines = [], [], []
    for i, cell in enumerate(source.itertuples()):
        center, polygon = centers[i], source_metric.geometry.iloc[i]
        node = int(snaps.origin_node_id.iloc[i])
        component = components[node]
        assert component == snaps.origin_component_id.iloc[i]
        node_point = Point(forward.transform(*xy[node]))
        assert (
            abs(node_point.distance(center) - snaps.origin_snap_distance_m.iloc[i])
            < 1e-7
        )
        options = [
            {
                "source": "nearest_node",
                "node_id": node,
                "component_id": component,
                "point": node_point,
                "line": None,
            }
        ]
        for rank, edge in enumerate(selected[i], 1):
            u, v, key = edge["edge_id"]
            assert components[u] == components[v]
            a, b = Point(forward.transform(*xy[u])), Point(forward.transform(*xy[v]))
            line = edge["line"]
            # Orient the full line to match the chosen edge before calculating the length fraction.
            if Point(line.coords[0]).distance(a) > 1e-6:
                line = line.reverse()
            assert (
                Point(line.coords[0]).distance(a) < 1e-6
                and Point(line.coords[-1]).distance(b) < 1e-6
            )
            offset = line.project(center)
            point = line.interpolate(offset)
            assert abs(point.distance(center) - edge["distance"]) < 1e-7
            assert point.distance(line) < 1e-7 and 0 <= offset <= line.length
            options.append(
                {
                    "source": "nearest_edge",
                    "rank": rank,
                    "edge_u": u,
                    "edge_v": v,
                    "edge_key": key,
                    "arc_ids": json_text(sorted(edge["arcs"])),
                    "way_ids": json_text(edge["way_ids"]),
                    "component_id": components[u],
                    "edge_fraction": offset / line.length,
                    "edge_offset_m": offset,
                    "point": point,
                    "line": line,
                    "tags": {
                        tag: edge["fields"][tag]
                        for tag in TAGS
                        if edge["fields"].get(tag)
                    },
                }
            )
        assert (
            options[1]["point"].distance(center) <= node_point.distance(center) + 1e-7
        )

        buildings = pyrosm.OSM(
            str(pbf),
            bounding_box=bbox_with_margin(polygon, 60, reverse),
            complete_relations=True,
        ).get_buildings(tags_to_keep=list(TAGS))
        local_evidence = []
        if buildings is not None:
            buildings = buildings.to_crs(METRIC)
            for _, feature in buildings[buildings.intersects(polygon)].iterrows():
                assert feature.geometry.is_valid
                overlap = polygon.intersection(
                    feature.geometry.intersection(municipal)
                ).area
                ref = osm_ref(feature)
                local_evidence.append(
                    {
                        "evidence_id": f"{cell.h3_id}:{ref['osm_type']}:{ref['osm_id']}",
                        "h3_id": cell.h3_id,
                        "osm_type": ref["osm_type"],
                        "osm_id": ref["osm_id"],
                        "intersection_in_city_m2": overlap,
                        "evidence_source": "local_pbf_window",
                        "tags": json_text(ref["tags"]),
                        **building_evidence(ref["tags"], overlap),
                    }
                )
        expected = (
            cell.residential_status,
            cell.has_confirmed_building,
            cell.has_uncertain_building,
        )
        archived = previous_evidence.get(cell.h3_id, {}).get("building_evidence")
        archived_ids = {(row["osm_type"], row["osm_id"]) for row in archived or ()}
        local_ids = {(row["osm_type"], row["osm_id"]) for row in local_evidence}
        # A narrow extract may miss a large closed way if none of its vertices
        # fall within the window. Cross-check building outlines for previously reviewed cells.
        if archived and (
            not archived_ids.issubset(local_ids)
            or evidence_status(local_evidence, "qualifies_under_current_rule")
            != expected
        ):
            local_evidence = [
                {
                    "evidence_id": f"{cell.h3_id}:{row['osm_type']}:{row['osm_id']}",
                    "h3_id": cell.h3_id,
                    "osm_type": row["osm_type"],
                    "osm_id": row["osm_id"],
                    "intersection_in_city_m2": row["intersection_in_city_m2"],
                    "evidence_source": "prior_local_pbf_context_audit",
                    "tags": json_text(row["tags"]),
                    **building_evidence(row["tags"], row["intersection_in_city_m2"]),
                }
                for row in archived
            ]
        local_evidence.sort(key=lambda row: row["evidence_id"])
        original, confirmed, uncertain = evidence_status(
            local_evidence, "qualifies_under_current_rule"
        )
        assert (original, confirmed, uncertain) == expected, cell.h3_id
        target, target_confirmed, target_uncertain = evidence_status(
            local_evidence, "contributes_to_target_mask"
        )
        cell_rows.append(
            {
                "h3_id": cell.h3_id,
                **{flag: getattr(cell, flag) for flag in evidence_flags},
                "center_lon": cell.center_lon,
                "center_lat": cell.center_lat,
                "center_crs": GEOGRAPHIC,
                "housing_evidence_state": "reproduced",
                "target_residential_status": target,
                "target_has_confirmed_building": target_confirmed,
                "target_has_uncertain_building": target_uncertain,
                "geometry": cell.geometry,
            }
        )
        building_rows.extend(local_evidence)

        span = shapely.union_all(
            [polygon, *(LineString([center, opt["point"]]) for opt in options)]
        )
        osm = pyrosm.OSM(
            str(pbf),
            bounding_box=bbox_with_margin(span, 60, reverse),
            complete_relations=True,
        )
        context = osm.get_data_by_custom_criteria(
            custom_filter=[
                '["entrance"]',
                '["barrier"]',
                '["highway"="crossing"]',
                '["footway"="crossing"]',
                '["access"]',
                '["foot"]',
                '["access:conditional"]',
                '["foot:conditional"]',
                '["natural"="water"]',
                '["waterway"~"^(stream|river|canal|drain)$"]',
                '["landuse"="military"]',
            ],
            tags_to_keep=list(TAGS),
            keep_other_tags=False,
        )
        context = (
            context.to_crs(METRIC)
            if context is not None
            else gpd.GeoDataFrame(geometry=[], crs=METRIC)
        )
        passage_refs = []
        # A shared OSM node ID provides topological evidence of a graph connection.
        near = context.loc[context.intersects(polygon.buffer(50))]
        if not near.empty:
            near = near.sort_values(["osm_type", "id"])
        for _, feature in near.iterrows():
            ref = osm_ref(feature)
            tags = ref["tags"]
            entrance = tags.get("entrance") not in {None, "no"}
            passage = (
                tags.get("barrier") in GATES
                or tags.get("highway") == "crossing"
                or tags.get("footway") == "crossing"
            )
            if not entrance and not passage:
                continue
            ref["network_link"] = "unverified"
            if (
                ref["osm_type"] == "node"
                and ref["osm_id"] in xy
                and topology.degree(ref["osm_id"]) > 0
            ):
                mapped_node = ref["osm_id"]
                mapped = Point(forward.transform(*xy[mapped_node]))
                if (
                    feature.geometry.geom_type == "Point"
                    and mapped.distance(feature.geometry) < 1e-5
                ):
                    ref["network_link"] = "shared_osm_node"
                    options.append(
                        {
                            "source": "mapped_entrance"
                            if entrance
                            else "mapped_passage",
                            "node_id": mapped_node,
                            "component_id": components[mapped_node],
                            "point": mapped,
                            "line": None,
                            "tags": tags,
                            "mapped_ref": ref,
                        }
                    )
            passage_refs.append(ref)
        passage_refs.sort(key=lambda row: (row["osm_type"], row["osm_id"]))
        for number, option in enumerate(options, 1):
            candidate_id = f"{cell.h3_id}:{number:02d}"
            point, line = option["point"], option["line"]
            connector = LineString([center, point])
            hits = classify_crossings(context, connector)
            # Check the original OSM ways that make up the simplified edge.
            edge_ways = set(json.loads(option.get("way_ids", "[]")))
            source_access = []
            if edge_ways and not context.empty:
                source_ways = context[
                    context.osm_type.eq("way") & context.id.isin(edge_ways)
                ]
                for _, feature in source_ways.sort_values("id").iterrows():
                    ref = osm_ref(feature)
                    source_access.append(
                        {**ref, "access_tags": tag_access(ref["tags"])}
                    )
            barrier = any(set(ref["categories"]) & {"barrier", "water"} for ref in hits)
            candidate_rows.append(
                {
                    "candidate_id": candidate_id,
                    "h3_id": cell.h3_id,
                    **{
                        k: v
                        for k, v in option.items()
                        if k not in {"point", "line", "tags", "mapped_ref"}
                    },
                    "snap_distance_m": center.distance(point),
                    "snap_geometry_status": "candidate",
                    "distance_crs": METRIC,
                    "component_size": sizes[option["component_id"]],
                    "in_largest_component": option["component_id"] == largest_component,
                }
            )
            points.append(point)
            connectors.append(connector)
            full_lines.append(line)
            for check_type, method, result, refs in (
                (
                    "geometry",
                    "distance_and_graph_topology_epsg25830",
                    "candidate",
                    [option.get("mapped_ref")] if option.get("mapped_ref") else [],
                ),
                (
                    "osm_screen",
                    "local_pbf_2d_intersection_and_osm_tags",
                    "intersection_found"
                    if barrier
                    else "no_intersection_in_checked_data",
                    {
                        "intersections": hits,
                        "nearby_passages": passage_refs,
                        "candidate_access_tags": tag_access(option.get("tags", {})),
                        "candidate_tags": option.get("tags", {}),
                        "edge_source_access": source_access,
                    },
                ),
                (
                    "physical_access",
                    "no_documented_passage_verification",
                    "unknown",
                    [],
                ),
            ):
                check_rows.append(
                    {
                        "check_id": f"{candidate_id}:{check_type}",
                        "candidate_id": candidate_id,
                        "check_type": check_type,
                        "checked_at": checked_at,
                        "method": method,
                        "result": result,
                        "evidence_refs": json_text(refs),
                        "physical_access": "unknown",
                    }
                )
        print(
            f"{cell.h3_id}: {original} → {target}, buildings: {len(local_evidence)}, candidates: {len(options)}.",
            flush=True,
        )

    cells = gpd.GeoDataFrame(cell_rows, geometry="geometry", crs=GEOGRAPHIC).assign(
        **versions
    )
    buildings_table = pd.DataFrame(building_rows).assign(**versions, overlap_crs=METRIC)
    candidates = pd.DataFrame(candidate_rows).assign(**versions)
    checks = pd.DataFrame(check_rows).assign(**versions)
    for column in ("node_id", "edge_u", "edge_v", "edge_key", "rank"):
        candidates[column] = pd.array(candidates[column], dtype="Int64")
    assert not any(
        frame[key].duplicated().any()
        for frame, key in (
            (cells, "h3_id"),
            (buildings_table, "evidence_id"),
            (candidates, "candidate_id"),
            (checks, "check_id"),
        )
    )
    assert (
        checks.candidate_id.isin(candidates.candidate_id).all()
        and checks.physical_access.eq("unknown").all()
    )
    assert (
        candidates.h3_id.isin(cells.h3_id).all()
        and buildings_table.h3_id.isin(cells.h3_id).all()
    )
    assert cells[evidence_flags].equals(source[evidence_flags])
    spatial = gpd.GeoDataFrame(candidates, geometry=gpd.GeoSeries(points, crs=METRIC))
    spatial["connector_geometry"] = gpd.GeoSeries(connectors, crs=METRIC)
    spatial["edge_geometry"] = gpd.GeoSeries(full_lines, crs=METRIC)
    for name, frame in (
        ("h3_cells", cells),
        ("building_evidence", buildings_table),
        ("access_candidates", spatial),
        ("candidate_checks", checks),
    ):
        path = root / f"{name}.parquet"
        frame.to_parquet(path, index=False)
        restored = (
            gpd.read_parquet(path)
            if isinstance(frame, gpd.GeoDataFrame)
            else pd.read_parquet(path)
        )
        if isinstance(frame, gpd.GeoDataFrame):
            assert restored.crs == frame.crs
        pd.testing.assert_frame_equal(restored, frame)
    spatial.drop(columns=["edge_geometry", "connector_geometry"]).to_crs(
        GEOGRAPHIC
    ).to_file(root / "access_points.geojson", driver="GeoJSON", index=False)
    gpd.GeoDataFrame(
        candidates[["candidate_id", "h3_id", "source"]], geometry=connectors, crs=METRIC
    ).to_crs(GEOGRAPHIC).to_file(
        root / "access_connectors.geojson", driver="GeoJSON", index=False
    )
    for path in sorted(root.glob("*.geojson")):
        reloaded = gpd.read_file(path)
        assert len(reloaded) == len(candidates) and reloaded.crs.to_epsg() == 4326
        assert reloaded.candidate_id.tolist() == candidates.candidate_id.tolist()
    last_pbf = pbf.stat()
    assert (last_pbf.st_size, last_pbf.st_mtime_ns) == (
        pbf_state.st_size,
        pbf_state.st_mtime_ns,
    )
    payload = {
        "versions": versions,
        "counts": {
            "h3": len(cells),
            "evidence": len(buildings_table),
            "candidates": len(candidates),
            "checks": len(checks),
            "graph_edges_read": edge_count,
        },
        "candidate_sources": candidates.source.value_counts().to_dict(),
        "evidence_sources": buildings_table.evidence_source.value_counts().to_dict(),
        "edge_key": {
            "graphml_representation": "XML string",
            "table_type": "nullable int64",
            "canonical_integer_ids_checked": edge_count,
        },
        "physical_access": "unknown",
        "edge_search_radius_m": dict(zip(H3_IDS, map(float, radii), strict=True)),
        "limits": [
            "Opposite directions are grouped only when the full geometry, endpoints, and OSM way IDs match.",
            "Barrier intersections were checked in 2D. Crossing levels and permission to pass remain unknown.",
            "Edge candidates were selected within the diagnostic search radius.",
            "The target mask excludes contributions from nursing_home. Other ambiguous categories are retained.",
        ],
        "statuses": cells.drop(columns=["geometry", *versions])[
            ["h3_id", "residential_status", "target_residential_status"]
        ].to_dict("records"),
        "elapsed_s": perf_counter() - started,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    (root / "report.json").write_text(json_text(payload) + "\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
