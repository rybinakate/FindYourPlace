"""
Network analysis for park accessibility.
Calculates coverage metrics and identifies "park deserts"
and builds accessibility isochrones. Compatible with OSMnx 2.x.
"""

from os import getenv

import networkx as nx
from shapely.geometry import box
from shapely.ops import unary_union

from . import config as cfg
from . import deps as d
from .walking_graph import load_walking_graph


def get_walk_network() -> nx.MultiDiGraph:
    """Load a pedestrian graph from a local PBF for the city's bounding box."""
    pbf_path = getenv("OSM_PBF_PATH")
    if not pbf_path:
        raise ValueError("Set the local .osm.pbf path in OSM_PBF_PATH.")
    if not cfg.CITY_BOUNDARY_PATH.is_file():
        raise FileNotFoundError(
            f"Local boundary GeoJSON not found: {cfg.CITY_BOUNDARY_PATH}"
        )

    city_gdf = d.gpd.read_file(cfg.CITY_BOUNDARY_PATH)
    if city_gdf.empty or city_gdf.crs is None or city_gdf.geometry.is_empty.all():
        raise ValueError("The boundary GeoJSON must contain geometry with a known CRS.")

    minx, miny, maxx, maxy = city_gdf.to_crs(cfg.CRS_GEO).total_bounds
    return load_walking_graph(
        pbf_path,
        (minx, miny, maxx, maxy),
        cache_dir=cfg.WALKING_GRAPH_CACHE_DIR,
    )


def calculate_accessibility_metrics(
    G: nx.MultiDiGraph,
    parks_gdf: d.gpd.GeoDataFrame,
    max_walk_meters: float = 800,
    park_entry_buffer_m: float = 50,  # buffer used to find candidate park access nodes
    simplify_tolerance_m: float = 50,  # 🚀 geometry simplification tolerance in meters
) -> dict:
    """

    Calculate accessibility isochrones for all parks.
    Start at network nodes inside each park polygon.
    """
    d.logging.info(
        f"🚶 Calculating {max_walk_meters} m isochrones for {len(parks_gdf)} parks..."
    )

    # 1. Project to a metric CRS
    G_proj = d.ox.project_graph(G, to_crs=cfg.CRS_LOCAL)
    parks_proj = parks_gdf.to_crs(crs=cfg.CRS_LOCAL)

    # 2. Study area
    minx, miny, maxx, maxy = parks_proj.total_bounds
    study_area = box(minx - 1000, miny - 1000, maxx + 1000, maxy + 1000)
    total_area_sq_m = study_area.area

    # 3. Read graph nodes into a GeoDataFrame
    nodes_gdf = d.ox.graph_to_gdfs(G_proj, edges=False)

    accessible_polygons = []

    # 4. Build an isochrone for each park using nodes near or inside its boundary
    for idx, park in parks_proj.iterrows():
        park_geom = park.geometry

        # 🚀 Find network nodes inside the park, with a small buffer for access points
        # Buffer the park polygon by 50 m to capture nodes along the boundary
        park_with_buffer = park_geom.buffer(park_entry_buffer_m)

        # Find nodes inside the buffered polygon
        mask = nodes_gdf.geometry.within(park_with_buffer)
        entry_nodes = nodes_gdf[mask]

        if entry_nodes.empty:
            # Fallback: use the nearest node outside the park if there are no nodes inside
            centroid = park_geom.centroid
            nearest = d.ox.distance.nearest_nodes(G_proj, X=centroid.x, Y=centroid.y)
            entry_nodes = nodes_gdf.loc[[nearest]]

        # 5. Find the nodes reachable from each candidate access node
        all_reachable = set()
        for node_id in entry_nodes.index:
            reachable = nx.single_source_dijkstra_path_length(
                G_proj, source=node_id, cutoff=max_walk_meters, weight="length"
            )
            all_reachable.update(reachable.keys())

        if not all_reachable:
            continue

        # 6. Approximate the isochrone with a 100 m buffer around reachable nodes
        reachable_coords = nodes_gdf.loc[list(all_reachable)].geometry
        park_isochrone = reachable_coords.buffer(100).unary_union

        accessible_polygons.append(park_isochrone)

    # 7. Merge all isochrones
    total_accessible_area: d.BaseGeometry
    if not accessible_polygons:
        d.logging.warning("⚠️ No isochrones could be generated")
        total_accessible_area = box(0, 0, 0, 0)
    else:
        total_accessible_area = unary_union(accessible_polygons)

    # 8. Clip to the study area boundary
    total_accessible_area = total_accessible_area.intersection(study_area)
    accessible_area_sq_m = total_accessible_area.area

    # 9. Park deserts
    park_deserts = study_area.difference(total_accessible_area)
    desert_area_sq_m = park_deserts.area

    # 10. 🚀 Simplify geometries for a smoother map
    # Simplify after calculating areas to preserve the reported metrics
    if simplify_tolerance_m > 0:
        d.logging.info(
            f"🗜️ Simplifying geometry (tolerance={simplify_tolerance_m} m)..."
        )
        total_accessible_area = total_accessible_area.simplify(
            tolerance=simplify_tolerance_m, preserve_topology=True
        )
        park_deserts = park_deserts.simplify(
            tolerance=simplify_tolerance_m, preserve_topology=True
        )

    d.logging.info(
        f"✅ Generated {len(accessible_polygons)} isochrones. "
        f"Accessible area: {accessible_area_sq_m / 10_000:,.0f} ha ({accessible_area_sq_m / total_area_sq_m * 100:.1f}%)"
    )

    return {
        "total_area_ha": total_area_sq_m / 10_000,
        "accessible_area_ha": accessible_area_sq_m / 10_000,
        "desert_area_ha": desert_area_sq_m / 10_000,
        "accessible_pct": (accessible_area_sq_m / total_area_sq_m) * 100,
        "desert_pct": (desert_area_sq_m / total_area_sq_m) * 100,
        "accessibility_index": accessible_area_sq_m / total_area_sq_m,
        "park_deserts_geometry": park_deserts,
        "accessible_geometry": total_accessible_area,
    }


def save_park_deserts(metrics: dict, output_path: d.Path) -> None:
    """Save park desert geometries to GeoJSON for further analysis."""
    gdf = d.gpd.GeoDataFrame(
        [
            {
                "zone": "Park Desert",
                "area_ha": metrics["desert_area_ha"],
                "geometry": metrics["park_deserts_geometry"],
            }
        ],
        crs=cfg.CRS_LOCAL,
    ).to_crs(crs=cfg.CRS_GEO)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(output_path, driver="GeoJSON")
    d.logging.info(f"💾 Park desert geometries saved to {output_path}")


def plot_accessibility_and_deserts(
    parks_gdf: d.gpd.GeoDataFrame,
    metrics: dict,
    output_path: d.Path = cfg.OUTPUTS_DIR / "park_accessibility_analysis.png",
) -> None:
    """Map park accessibility and highlight park deserts."""
    import contextily as ctx  # Import locally so the dependency is optional for make data

    d.logging.info("🎨 Rendering the park accessibility map...")

    fig, ax = d.plt.subplots(figsize=(10, 10))

    # Project to Web Mercator
    parks_web = parks_gdf.to_crs(crs=cfg.CRS_WEB)
    accessible_web = (
        d.gpd.GeoSeries([metrics["accessible_geometry"]], crs=cfg.CRS_LOCAL)
        .to_crs(crs=cfg.CRS_WEB)
        .iloc[0]
    )
    deserts_web = (
        d.gpd.GeoSeries([metrics["park_deserts_geometry"]], crs=cfg.CRS_LOCAL)
        .to_crs(crs=cfg.CRS_WEB)
        .iloc[0]
    )

    # Set the map extent with a small buffer
    minx, miny, maxx, maxy = parks_web.total_bounds
    ax.set_xlim(minx - (maxx - minx) * 0.1, maxx + (maxx - minx) * 0.1)
    ax.set_ylim(miny - (maxy - miny) * 0.1, maxy + (maxy - miny) * 0.1)

    # 1. Basemap (Esri World Gray Canvas)
    ctx.add_basemap(
        ax,
        source=ctx.providers.Esri.WorldGrayCanvas,
        crs=cfg.CRS_WEB,
        zoom=13,
    )

    # Reposition the attribution
    for text in ax.texts:
        if "Esri" in text.get_text() or "OpenStreetMap" in text.get_text():
            x, y = text.get_position()
            text.set_position((x + 0.02, y + 0.02))
            text.set_bbox(
                {"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 1.5}
            )
            text.set_fontsize(7)
            text.set_color("#555555")

    # 2. Park deserts (semi-transparent red with no outline)
    d.gpd.GeoSeries([deserts_web]).plot(
        ax=ax, color="#e5b85e", edgecolor="none", alpha=0.3, zorder=1
    )

    # 3. Accessible zones (semi-transparent green with no outline)
    d.gpd.GeoSeries([accessible_web]).plot(
        ax=ax, color="#048608", edgecolor="none", alpha=0.3, zorder=2
    )

    # 4. Parks (dark green with a white outline for contrast)
    parks_web.plot(ax=ax, color="#043e04", zorder=3)

    # 6. Metrics panel
    info_text = (
        f"Accessibility Index: {metrics['accessibility_index']:.2f}\n"
        f"Accessible Area: {metrics['accessible_pct']:.1f}%\n"
        f"Park Deserts: {metrics['desert_pct']:.1f}%"
    )
    ax.text(
        0.05,
        0.95,
        info_text,
        transform=ax.transAxes,
        fontsize=12,
        fontweight="bold",
        verticalalignment="top",
        bbox={
            "boxstyle": "round",
            "facecolor": "white",
            "alpha": 0.85,
            "edgecolor": "gray",
        },
    )

    ax.set_title(
        "Park Accessibility & Park Deserts Analysis",
        fontsize=16,
        fontweight="bold",
        pad=15,
    )
    ax.axis("off")

    d.plt.savefig(output_path, dpi=cfg.VIZ.dpi, bbox_inches="tight", facecolor="white")
    d.plt.close(fig)
    d.logging.info(f"✅ Analysis map saved to {output_path}")


def run_network_analysis() -> None:
    """Run the park accessibility pipeline."""
    d.logging.basicConfig(level=d.logging.INFO, format="%(levelname)s: %(message)s")

    if not cfg.PARKS_RAW_PATH.exists():
        d.logging.error("⚠️ Download the park data first: make data")
        return

    parks = d.gpd.read_file(cfg.PARKS_RAW_PATH)
    parks = parks[
        (parks["name"] != cfg.DEFAULT_FEATURE_NAME) & (parks["area_hectares"] > 1.0)
    ]

    if parks.empty:
        d.logging.error("⚠️ No parks found for analysis.")
        return

    # 1. Pedestrian network
    G = get_walk_network()

    # 2. Calculate metrics (800 m is approximately a 10-minute walk)
    metrics = calculate_accessibility_metrics(G, parks, max_walk_meters=800)

    # 3. Print the metrics
    print("\n" + "=" * 50)
    print("📊 PARK ACCESSIBILITY RESULTS (10-MINUTE WALK)")
    print("=" * 50)
    print(f"📍 Total study area: {metrics['total_area_ha']:,.0f} ha")
    print(
        f"✅ Accessible area: {metrics['accessible_area_ha']:,.0f} ha ({metrics['accessible_pct']:.1f}%)"
    )
    print(
        f"❌ Park deserts: {metrics['desert_area_ha']:,.0f} ha ({metrics['desert_pct']:.1f}%)"
    )
    print(f"📈 Accessibility index: {metrics['accessibility_index']:.2f} (ideal = 1.0)")
    print("=" * 50 + "\n")

    # 4. Save park desert geometries
    desert_path = cfg.DATA_RAW_DIR / "park_deserts.geojson"
    save_park_deserts(metrics, desert_path)

    # 5. Render the map
    plot_accessibility_and_deserts(parks, metrics)

    d.logging.info("✨ Network analysis complete!")


if __name__ == "__main__":
    run_network_analysis()
