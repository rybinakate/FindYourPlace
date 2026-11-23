"""Snap residential H3 cell centers to the pedestrian graph.

Inputs: a GeoDataFrame with centers and residential statuses, and a NetworkX graph with a CRS
and node coordinates. The result includes each residential cell's candidate node,
straight-line distance in meters, graph component, and diagnostic flag.
"""

from __future__ import annotations

from math import isfinite
from numbers import Real
from typing import TYPE_CHECKING

import networkx as nx
import numpy as np
import pandas as pd
from pyproj import CRS, Transformer
from scipy.spatial import cKDTree

if TYPE_CHECKING:
    import geopandas as gpd

METRIC_CRS = "EPSG:25830"
CENTER_CRS = "EPSG:4326"


def snap_residential_origins(
    cells: gpd.GeoDataFrame, graph: nx.MultiDiGraph
) -> gpd.GeoDataFrame:
    """Return a copy of the residential cells with geometric snapping results.

    The cells argument contains WGS84 centers and residential statuses. The graph is a directed
    multigraph with node coordinates in its own CRS and integer OSM IDs.
    Cell geometry and residential status are preserved. Candidate distances
    are measured in a straight line. No access checks or distance thresholds are applied.
    """
    required = {"h3_id", "center_lon", "center_lat", "residential_status"}
    if not required.issubset(cells.columns):
        raise ValueError(
            f"Missing cell columns: {sorted(required - set(cells.columns))}"
        )
    if cells.h3_id.isna().any() or not cells.h3_id.is_unique:
        raise ValueError("h3_id must be non-null and unique.")
    if cells.crs is None or not cells.crs.equals(
        CRS.from_user_input(CENTER_CRS), ignore_axis_order=True
    ):
        raise ValueError("Cells and their center coordinates must use WGS84.")
    if not isinstance(graph, nx.MultiDiGraph):
        raise TypeError("Expected a NetworkX MultiDiGraph.")

    result = cells.loc[cells.residential_status.isin(["confirmed", "uncertain"])].copy()
    result["origin_node_id"] = pd.array([pd.NA] * len(result), dtype="Int64")
    result["origin_component_id"] = pd.array([pd.NA] * len(result), dtype="Int64")
    result["origin_snap_distance_m"] = np.nan
    result["origin_snap_quality"] = "unmatched"
    result["origin_snap_flag"] = "no_usable_nodes"

    # Validate longitude and latitude before projecting to EPSG:25830, where distances are in meters.
    lon = pd.to_numeric(result.center_lon, errors="coerce").to_numpy(dtype=float)
    lat = pd.to_numeric(result.center_lat, errors="coerce").to_numpy(dtype=float)
    valid = (
        np.isfinite(lon) & np.isfinite(lat) & (np.abs(lon) <= 180) & (np.abs(lat) < 90)
    )
    center_projection = Transformer.from_crs(CENTER_CRS, METRIC_CRS, always_xy=True)
    center_x, center_y = center_projection.transform(lon[valid], lat[valid])
    center_ok = np.isfinite(center_x) & np.isfinite(center_y)
    positions = np.flatnonzero(valid)[center_ok]
    valid[:] = False
    valid[positions] = True
    result.loc[~valid, "origin_snap_flag"] = "invalid_center"
    if not len(positions) or not graph.number_of_nodes():
        return result

    if "crs" not in graph.graph:
        raise ValueError("Graph must have a CRS.")

    # Transform node x/y coordinates from the graph CRS to the same metric CRS, preserving OSM IDs.
    nodes = []
    for node_id, attrs in graph.nodes(data=True):
        x, y = attrs.get("x"), attrs.get("y")
        if (
            isinstance(x, Real)
            and isinstance(y, Real)
            and isfinite(float(x))
            and isfinite(float(y))
        ):
            nodes.append((node_id, x, y))
    if not nodes:
        return result
    node_ids, x, y = zip(*nodes, strict=True)
    graph_projection = Transformer.from_crs(
        graph.graph["crs"], METRIC_CRS, always_xy=True
    )
    x_m, y_m = graph_projection.transform(x, y)
    xy = np.column_stack((x_m, y_m))
    usable = np.isfinite(xy).all(axis=1)
    if not usable.any():
        return result

    # Use one spatial index for all nearest-node searches in meters.
    distance, index = cKDTree(xy[usable]).query(
        np.column_stack((center_x[center_ok], center_y[center_ok]))
    )
    matches = np.asarray(node_ids, dtype=np.int64)[usable][index]

    # Compute weakly connected components over all graph nodes, ignoring edge direction.
    # The smallest OSM ID identifies the component in this snapshot and says nothing about access.
    components = {}
    for members in nx.weakly_connected_components(graph):
        component_id = min(members)
        components.update(dict.fromkeys(members, component_id))

    # Assign results by row position because GeoDataFrame index labels can repeat.
    result.iloc[positions, result.columns.get_loc("origin_node_id")] = matches
    result.iloc[positions, result.columns.get_loc("origin_component_id")] = [
        components[node] for node in matches
    ]
    result.iloc[positions, result.columns.get_loc("origin_snap_distance_m")] = distance
    result.iloc[positions, result.columns.get_loc("origin_snap_quality")] = "candidate"
    result.iloc[positions, result.columns.get_loc("origin_snap_flag")] = (
        "requires_access_check"
    )
    return result
