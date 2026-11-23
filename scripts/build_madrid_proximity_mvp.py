"""Assess straight-line proximity to amenities on an existing H3 grid for Madrid.

Read a local OSM PBF, a residential H3 GeoParquet, and the municipal boundary
to produce geospatial layers, a map, a PDF, and a run summary. Alternatively, render the portfolio package from saved results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from textwrap import fill
from time import perf_counter

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
import pyrosm
import shapely

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator

METRIC = "EPSG:25830"
SPECS = {
    "park": ("Parks", 500, {"leisure": "park"}),
    "pharmacy": ("Pharmacies", 400, {"amenity": "pharmacy", "healthcare": "pharmacy"}),
    "school": ("Schools", 700, {"amenity": "school"}),
    "kindergarten": ("Kindergartens", 600, {"amenity": "kindergarten"}),
    "hospital": ("Hospitals", 1500, {"amenity": "hospital", "healthcare": "hospital"}),
}
CLASS_BOUNDS = (0, 20, 40, 60, 80, 100)
CLASS_LABELS = ("0–<20", "20–<40", "40–<60", "60–<80", "80–100")
COLORS = ("#f3d09c", "#ceca9b", "#75aea5", "#326d7d", "#17475b")
INK = "#19303b"
MUTED = "#334e5a"
PAPER = "#ffffff"
NO_SCORE = "#e1e4e5"

LIMITATIONS = [
    "D values are scenario parameters rather than official planning standards.",
    "Distances run straight to feature geometries. Entrances and actual pedestrian access were not assessed.",
    "Only facilities within the municipal boundary are included.",
    "Coverage depends on OSM data. Scores do not account for population.",
]


def deny_network(event: str, args: tuple) -> None:
    """Block Python network requests during the offline run."""
    if event in {
        "socket.connect",
        "socket.sendto",
        "socket.sendmsg",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }:
        raise RuntimeError(f"Offline mode blocked {event}.")


@contextmanager
def stage(timings: dict[str, float], label: str):
    """Record the elapsed time of a workflow stage."""
    start = perf_counter()
    print(f"Starting {label}", flush=True)
    try:
        yield
    finally:
        timings[label] = perf_counter() - start
        print(f"{label}: {timings[label]:.2f} s", flush=True)


def file_identity(path: Path) -> dict:
    """Return the path, size, and checksum of a local input."""
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest}


def usable_geometry(geometry):
    """Keep point and area geometry after clipping a feature.

    Line fragments in a geometry collection are unsuitable as facility footprints.
    """
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type in {"Point", "MultiPoint", "Polygon", "MultiPolygon"}:
        return geometry
    if geometry.geom_type == "GeometryCollection":
        parts = [
            part
            for item in geometry.geoms
            if (part := usable_geometry(item)) is not None
        ]
        return shapely.union_all(parts) if parts else None
    return None


def category_layer(
    raw: gpd.GeoDataFrame, tags: dict[str, str], municipality
) -> tuple[gpd.GeoDataFrame, dict]:
    """Select, clean, and clip facilities for one amenity category."""
    match = pd.Series(False, index=raw.index)
    for key, value in tags.items():
        if key in raw:
            match |= raw[key].eq(value).fillna(False)
    selected = raw.loc[match, ["osm_type", "id", "name", "geometry"]].copy()
    qa = {
        "tag_matches": len(selected),
        "raw_geometry_types": selected.geom_type.value_counts().to_dict(),
        "raw_invalid": int((~selected.is_valid & selected.geometry.notna()).sum()),
        "tag_counts": {
            f"{key}={value}": int(raw[key].eq(value).sum()) if key in raw else 0
            for key, value in tags.items()
        },
    }
    selected = selected.loc[selected.geometry.notna() & ~selected.is_empty].copy()
    qa["missing_or_empty"] = qa["tag_matches"] - len(selected)
    invalid = ~selected.is_valid
    selected.loc[invalid, "geometry"] = selected.loc[invalid].geometry.make_valid()
    selected = selected.to_crs(METRIC)
    inside = selected.geometry.intersects(municipality)
    qa["outside_municipality"] = int((~inside).sum())
    selected = selected.loc[inside].copy()
    selected.geometry = selected.geometry.intersection(municipality).map(
        usable_geometry
    )
    selected = selected.set_geometry("geometry")
    supported = selected.geometry.notna() & selected.is_valid & ~selected.is_empty
    qa["unsupported_or_collapsed"] = int((~supported).sum())
    selected = selected.loc[supported].sort_values(["osm_type", "id"]).copy()
    before = len(selected)
    selected = selected.drop_duplicates(["osm_type", "id"]).copy()
    qa["duplicate_osm_ids"] = before - len(selected)

    # Identical normalized geometries add no new candidates to the search.
    keys = selected.geometry.normalize().to_wkb()
    qa["duplicate_geometry"] = int(keys.duplicated().sum())
    selected = selected.loc[~keys.duplicated()].copy()
    polygonal = selected.loc[selected.geom_type.isin(["Polygon", "MultiPolygon"])]
    point_rows = selected.loc[selected.geom_type.eq("Point") & selected["name"].notna()]
    contained = []
    if not polygonal.empty:
        normalized_names = polygonal["name"].astype("string").str.strip().str.casefold()
        for index, item in point_rows.iterrows():
            name = str(item["name"]).strip().casefold()
            hits = polygonal.sindex.query(item.geometry, predicate="covered_by")
            if name and normalized_names.iloc[hits].eq(name).fillna(False).any():
                contained.append(index)
    qa["same_name_points_covered_by_polygon"] = len(contained)
    selected = selected.drop(index=contained).reset_index(drop=True)
    if selected.empty:
        raise ValueError(f"No valid facilities remain for {tags}.")
    qa["used"] = len(selected)
    qa["used_geometry_types"] = selected.geom_type.value_counts().to_dict()
    qa["bounds_epsg25830"] = selected.total_bounds.tolist()

    # Use bounding-box quadrants as a coarse check of spatial coverage.
    west, south, east, north = municipality.bounds
    representatives = selected.geometry.representative_point()
    east_half = representatives.x.ge((west + east) / 2)
    north_half = representatives.y.ge((south + north) / 2)
    qa["bbox_quadrants"] = {
        "NW": int((~east_half & north_half).sum()),
        "NE": int((east_half & north_half).sum()),
        "SW": int((~east_half & ~north_half).sum()),
        "SE": int((east_half & ~north_half).sum()),
    }
    return selected, qa


def nearest_distances(centers: np.ndarray, features: np.ndarray) -> np.ndarray:
    """Measure the distance from each center to the nearest full geometry."""
    indices, measured = shapely.STRtree(features).query_nearest(
        centers, all_matches=False, return_distance=True
    )
    distances = np.full(len(centers), np.nan)
    distances[indices[0]] = measured
    if not np.isfinite(distances).all() or (distances < 0).any():
        raise ValueError("Could not measure distances for all H3 cells.")
    return distances


def classify_scores(scores: np.ndarray) -> np.ndarray:
    """Assign the MVP's five classes, including each lower class boundary."""
    return np.searchsorted(CLASS_BOUNDS[1:-1], scores, side="right") + 1


def score_residential_cells(
    original: gpd.GeoDataFrame,
    residential: pd.Series,
    centers: np.ndarray,
    facility_layers: dict[str, gpd.GeoDataFrame],
    category_qa: dict,
) -> tuple[gpd.GeoDataFrame, np.ndarray, np.ndarray, int]:
    """Score the selected cells and update category QA using EPSG:25830 geometries.

    The centers follow the order of the selected rows. Facility layers are
    projected by category_layer before this function runs.
    """
    result = original.to_crs(METRIC)
    scores = []
    # Cross-check the index by measuring against every full geometry for a sample.
    check_indices = np.linspace(0, len(centers) - 1, 8, dtype=int)
    for key, (_, scale, _) in SPECS.items():
        features = facility_layers[key].geometry.to_numpy()
        distances = nearest_distances(centers, features)
        for index in check_indices:
            expected = float(shapely.distance(centers[index], features).min())
            if not math.isclose(expected, distances[index], abs_tol=1e-7, rel_tol=0):
                raise ValueError(
                    f"Spatial-index and exhaustive-search distances disagree for {key}."
                )
        score = 100 * np.exp(-distances / scale)
        result.loc[residential, f"{key}_distance_m"] = distances
        result.loc[residential, f"{key}_score"] = score
        scores.append(score)
        category_qa[key]["distance_m"] = describe(distances)
        category_qa[key]["score"] = describe(score)
        category_qa[key]["centers_on_geometry"] = int(np.count_nonzero(distances == 0))
    integrated = np.mean(scores, axis=0)
    result.loc[residential, "integrated_score"] = integrated
    result["score_class"] = pd.array([pd.NA] * len(result), dtype="Int64")
    codes = classify_scores(integrated)
    result.loc[residential, "score_class"] = codes
    if (
        not np.isfinite(integrated).all()
        or not ((0 <= integrated) & (integrated <= 100)).all()
    ):
        raise ValueError("Composite scores must be finite and within 0–100.")
    if not result.residential_status.equals(original.residential_status):
        raise ValueError("The input residential statuses changed unexpectedly.")
    return result, integrated, codes, len(check_indices) * len(SPECS)


def describe(values: np.ndarray) -> dict[str, float]:
    """Return summary statistics for the JSON report and PDF."""
    return {
        "min": float(np.min(values)),
        "median": float(np.median(values)),
        "mean": float(np.mean(values)),
        "p90": float(np.quantile(values, 0.90)),
        "max": float(np.max(values)),
    }


def number(value: float, digits: int = 0) -> str:
    """Format numbers with English thousands and decimal separators."""
    return f"{value:,.{digits}f}"


def draw_map(
    grid: gpd.GeoDataFrame, municipal: gpd.GeoDataFrame, summary: dict, out: Path
):
    """Draw the thematic map in a metric CRS and save it as a PNG."""
    fig = plt.figure(figsize=(13.8, 8.3), facecolor=PAPER)
    fig.text(
        0.055,
        0.948,
        "FIND YOUR PLACE   /   GIS PORTFOLIO",
        color="#347275",
        fontsize=10,
        weight="bold",
    )
    fig.text(
        0.055,
        0.887,
        "Madrid | Access to Everyday Amenities",
        color=INK,
        fontsize=29,
        weight="bold",
    )
    fig.text(
        0.055,
        0.848,
        "An H3-based assessment of residential areas using straight-line distances to essential services",
        color=MUTED,
        fontsize=13,
    )

    ax = fig.add_axes((0.05, 0.11, 0.67, 0.70))
    ax.set_facecolor(PAPER)
    # Draw the municipality in gray and overlay the assessed cells.
    municipal.plot(ax=ax, color=NO_SCORE, linewidth=0, zorder=1)
    for index, color in enumerate(COLORS, 1):
        cells = grid.loc[grid.score_class.eq(index).fillna(False)]
        if not cells.empty:
            cells.plot(
                ax=ax,
                color=color,
                linewidth=0,
                edgecolor="none",
                rasterized=True,
                zorder=2,
            )
    municipal.boundary.plot(ax=ax, color=INK, linewidth=0.8, zorder=3)
    west, south, east, north = municipal.total_bounds
    ax.set(
        xlim=(west - 850, east + 850), ylim=(south - 1300, north + 850), aspect="equal"
    )
    ax.set_axis_off()
    ax.annotate(
        "N",
        xy=(0.94, 0.91),
        xytext=(0.94, 0.83),
        xycoords="axes fraction",
        arrowprops={"arrowstyle": "-|>", "color": INK, "lw": 1.4},
        ha="center",
        color=INK,
        fontsize=10,
        fontweight="bold",
    )
    scale_x, scale_y = west + 750, south - 640
    ax.plot([scale_x, scale_x + 5000], [scale_y, scale_y], color=INK, lw=2)
    ax.vlines(
        [scale_x, scale_x + 2500, scale_x + 5000],
        scale_y - 125,
        scale_y + 125,
        color=INK,
        lw=1,
    )
    ax.annotate(
        "5 km",
        xy=(scale_x + 2500, scale_y),
        xytext=(0, -5),
        textcoords="offset points",
        ha="center",
        va="top",
        color=INK,
        fontsize=9,
    )

    sidebar = fig.add_axes((0.752, 0.15, 0.21, 0.64))
    sidebar.axis("off")
    sidebar.text(
        0, 0.98, "Composite Proximity Score", fontsize=14, color=INK, weight="bold"
    )
    sidebar.text(
        0, 0.928, "Score out of 100 · Five equal-width classes", fontsize=9, color=MUTED
    )
    handles = [
        Patch(color=color, label=label)
        for color, label in zip(COLORS, CLASS_LABELS, strict=True)
    ]
    handles += [
        Patch(color=NO_SCORE, label="Not assessed"),
        Line2D([], [], color=INK, lw=1.1, label="Municipal boundary"),
    ]
    sidebar.legend(
        handles=handles,
        loc="upper left",
        bbox_to_anchor=(-0.02, 0.865),
        frameon=False,
        fontsize=10,
        labelcolor=INK,
    )
    sidebar.text(
        0,
        0.40,
        number(summary["residential_cells"]),
        color="#17475b",
        fontsize=24,
        weight="bold",
    )
    sidebar.text(
        0, 0.345, "residential H3 cells · Resolution 11", fontsize=10, color=MUTED
    )
    sidebar.text(
        0,
        0.235,
        number(summary["integrated"]["median"], 1),
        color="#17475b",
        fontsize=24,
        weight="bold",
    )
    sidebar.text(0, 0.18, "Median composite score", fontsize=10, color=MUTED)
    sidebar.text(
        0,
        0.082,
        "5 amenity categories · Equal weights of 20%",
        fontsize=10,
        color=MUTED,
    )
    fig.text(
        0.055,
        0.065,
        "Source: © OpenStreetMap contributors. Local OSM PBF. Madrid municipal boundary. EPSG:25830.",
        fontsize=9,
        color=MUTED,
    )
    fig.text(
        0.055,
        0.037,
        "Straight-line distances to OSM geometries. Walking routes, entrances, and actual pedestrian access were not assessed.",
        fontsize=9,
        color=MUTED,
    )
    fig.savefig(out, dpi=220, facecolor=fig.get_facecolor())
    return fig


def draw_methodology(summary: dict):
    """Lay out the methodology, summary statistics, and limitations for page two."""
    fig = plt.figure(figsize=(13.8, 8.3), facecolor=PAPER)
    fig.text(
        0.065,
        0.94,
        "FIND YOUR PLACE   /   METHODOLOGY & RESULTS",
        color="#347275",
        fontsize=10,
        weight="bold",
    )
    fig.text(
        0.065,
        0.877,
        "Understanding the Accessibility Score",
        color=INK,
        fontsize=28,
        weight="bold",
    )
    fig.text(
        0.065,
        0.823,
        "Comparing straight-line proximity to five types of amenities across Madrid's residential areas",
        color=MUTED,
        fontsize=12,
    )

    left = fig.add_axes((0.065, 0.43, 0.52, 0.34))
    left.axis("off")
    heading = [
        "Category",
        "Facilities",
        "D (m)",
        "Median distance (m)",
        "P90 distance (m)",
    ]
    rows = []
    for key, (label, scale, _) in SPECS.items():
        item = summary["categories"][key]
        rows.append(
            [
                label,
                number(item["used"]),
                number(scale),
                number(item["distance_m"]["median"]),
                number(item["distance_m"]["p90"]),
            ]
        )
    tab = left.table(
        cellText=rows,
        colLabels=heading,
        cellLoc="left",
        colLoc="left",
        colWidths=[0.24, 0.14, 0.10, 0.27, 0.25],
        bbox=(0, 0.04, 1, 0.96),
    )
    tab.auto_set_font_size(False)
    tab.set_fontsize(10)
    for (row, _), cell in tab.get_celld().items():
        cell.set_edgecolor(PAPER)
        cell.set_facecolor(
            "#1d4756" if row == 0 else "#edf0e9" if row % 2 else "#f4f2ea"
        )
        cell.set_text_props(
            color="white" if row == 0 else INK, weight="bold" if row == 0 else "normal"
        )

    right = fig.add_axes((0.64, 0.47, 0.29, 0.29))
    bar_counts = [summary["class_counts"][str(i)] for i in range(1, 6)]
    right.barh(CLASS_LABELS, bar_counts, color=COLORS, height=0.66)
    right.invert_yaxis()
    right.spines[["top", "right", "left"]].set_visible(False)
    right.set_facecolor(PAPER)
    right.tick_params(colors=MUTED, labelsize=9, length=0)
    right.xaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
    right.xaxis.set_major_formatter(FuncFormatter(lambda value, _: number(value)))
    right.set_title(
        "Cells by Score Class",
        color=INK,
        loc="left",
        fontsize=12,
        pad=11,
        weight="bold",
    )
    right.set_xlabel("Number of H3 cells", color=MUTED, fontsize=9)

    figures = summary["integrated"]
    fig.text(0.065, 0.376, "Scoring Method", fontsize=13, weight="bold", color=INK)
    fig.text(
        0.065,
        0.336,
        "S = 100 × exp(−d / D)   ·   Composite score = mean of five category scores",
        fontsize=13,
        weight="bold",
        color="#17475b",
    )
    method = (
        "d is the shortest straight-line distance from the H3 cell center to the full OSM feature geometry in EPSG:25830. "
        "Distance is zero when the center falls inside a polygon. At d = D, the score is about 36.8. "
        "D controls how quickly the score falls with distance. Everyday amenities use shorter decay distances "
        "and hospitals use a longer one to reflect a wider service area. D is a scenario parameter rather than an official planning standard."
    )
    fig.text(
        0.065,
        0.303,
        fill(method, 155),
        color=MUTED,
        fontsize=10,
        va="top",
        linespacing=1.5,
    )
    fig.text(0.065, 0.206, "Limitations", fontsize=13, weight="bold", color=INK)
    limits = (
        "The assessment uses OSM facilities within Madrid municipality. "
        "Facilities immediately outside the municipal boundary are excluded. Park boundaries do not necessarily represent accessible entrances. "
        "Walking networks, barriers, opening hours, service quality, and population are outside the scope. "
        "Both confirmed and uncertain residential H3 cells are included."
    )
    fig.text(
        0.065,
        0.177,
        fill(limits, 123),
        color=MUTED,
        fontsize=10,
        va="top",
        linespacing=1.5,
    )
    fig.text(
        0.065,
        0.075,
        f"Median: {number(figures['median'], 1)}   ·   Mean: {number(figures['mean'], 1)}   ·   "
        f"P90: {number(figures['p90'], 1)}   ·   Straight-line proximity · No walking routes modeled",
        color="#347275",
        fontsize=10,
        weight="bold",
    )
    return fig


def render_portfolio(
    grid: gpd.GeoDataFrame, municipal: gpd.GeoDataFrame, summary: dict, out: Path
) -> None:
    """Write the English map and two-page PDF from analyzed data."""
    plt.rcParams.update({"font.family": "DejaVu Sans", "pdf.fonttype": 42})
    figure = draw_map(
        grid.loc[grid.integrated_score.notna()],
        municipal,
        summary,
        out / "madrid_proximity_map.png",
    )
    method = draw_methodology(summary)
    with PdfPages(
        out / "find_your_place_madrid.pdf",
        metadata={
            "Title": "Find Your Place | Madrid",
            "Subject": "Straight-line proximity to everyday amenities",
        },
    ) as pdf:
        pdf.savefig(figure, dpi=220, facecolor=PAPER)
        pdf.savefig(method, dpi=220, facecolor=PAPER)
    plt.close(figure)
    plt.close(method)


def render_from_saved(source: Path, out: Path) -> None:
    """Copy saved analytical layers verbatim and localize presentation metadata."""
    parquet = source / "madrid_h3_proximity.geoparquet"
    package = source / "madrid_proximity.gpkg"
    summary = json.loads((source / "report.json").read_text())
    grid = gpd.read_parquet(parquet)
    municipal = gpd.read_file(package, layer="madrid_boundary")

    for key, (label, scale, tags) in SPECS.items():
        parameter = summary["parameters"][key]
        if (parameter["D_m"], parameter["weight"], parameter["osm_tags_any"]) != (
            scale,
            0.2,
            tags,
        ):
            raise ValueError(f"The saved scenario differs from this MVP for {key}.")
        parameter["label"] = label
    if summary["class_bounds"] != list(CLASS_BOUNDS):
        raise ValueError("The saved score classes differ from this MVP.")
    summary["limitations"] = LIMITATIONS
    # Keep the historical durations, with English labels in the workflow's stage order.
    stages = (
        "Input validation",
        "Amenity extraction and validation",
        "Distances and scores",
        "Geodata export and validation",
        "Map and PDF rendering",
    )
    summary["stages_s"] = dict(zip(stages, summary["stages_s"].values(), strict=True))
    summary["presentation"] = {"language": "en", "mode": "render_from_saved"}
    render_portfolio(grid, municipal, summary, out)
    for path in (parquet, package):
        shutil.copyfile(path, out / path.name)
    (out / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    """Build the MVP from local inputs or render the English package from saved results."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pbf", "h3", "boundary"):
        parser.add_argument(f"--{name}", type=Path)
    parser.add_argument(
        "--render-from", type=Path, help="Directory containing saved MVP results"
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="New output directory under cache/"
    )
    args = parser.parse_args()
    inputs_provided = [args.pbf, args.h3, args.boundary]
    if args.render_from and any(inputs_provided):
        parser.error(
            "--render-from cannot be combined with --pbf, --h3, or --boundary."
        )
    if not args.render_from and not all(inputs_provided):
        parser.error("Provide --render-from or all of --pbf, --h3, and --boundary.")
    sys.addaudithook(deny_network)
    out = args.out.resolve()
    if not out.is_relative_to(Path("cache").resolve()):
        raise ValueError("Choose a new output directory inside cache/.")
    out.mkdir(parents=True, exist_ok=False)
    if args.render_from:
        render_from_saved(args.render_from, out)
        print(f"Finished. Results saved to {out}", flush=True)
        return
    timings: dict[str, float] = {}

    with stage(timings, "Input validation"):
        inputs = {
            name: file_identity(getattr(args, name))
            for name in ("pbf", "h3", "boundary")
        }
        city = gpd.read_file(args.boundary)
        if (
            city.crs is None
            or city.osm_id.tolist() != [5326784]
            or not city.is_valid.all()
        ):
            raise ValueError("Expected the verified Madrid municipal boundary.")
        geographic_city = city.to_crs(4326)
        municipal = city.to_crs(METRIC)[["geometry"]]
        municipality = municipal.geometry.iloc[0]
        original = gpd.read_parquet(args.h3)
        required = {
            "h3_id",
            "h3_resolution",
            "center_lon",
            "center_lat",
            "residential_status",
        }
        if (
            original.crs is None
            or not required.issubset(original.columns)
            or original.h3_id.isna().any()
            or not original.h3_id.is_unique
            or not original.h3_resolution.eq(11).all()
            or not original.residential_status.isin(
                ["confirmed", "uncertain", "excluded"]
            ).all()
        ):
            raise ValueError(
                "The input H3 layer does not meet the schema or resolution requirements."
            )
        residential = original.residential_status.isin(["confirmed", "uncertain"])
        centers = (
            gpd.GeoSeries(
                gpd.points_from_xy(
                    original.loc[residential, "center_lon"],
                    original.loc[residential, "center_lat"],
                ),
                crs=4326,
            )
            .to_crs(METRIC)
            .to_numpy()
        )
        if not shapely.covers(municipality, centers).all():
            raise ValueError(
                "Some residential cell centers lie outside Madrid municipality."
            )

    with stage(timings, "Amenity extraction and validation"):
        filters = sorted(
            {
                f'["{key}"="{value}"]'
                for _, _, tags in SPECS.values()
                for key, value in tags.items()
            }
        )
        osm = pyrosm.OSM(
            str(args.pbf),
            bounding_box=geographic_city.total_bounds.tolist(),
            complete_relations=True,
        )
        raw = osm.get_data_by_custom_criteria(
            custom_filter=filters,
            tags_to_keep=["name", "amenity", "leisure", "healthcare"],
            keep_other_tags=False,
        )
        if raw is None or raw.empty or raw.crs is None:
            raise ValueError("Pyrosm did not return an amenity layer with a known CRS.")
        if "name" not in raw:
            raw["name"] = None
        facility_layers, category_qa = {}, {}
        for key, (_, _, tags) in SPECS.items():
            layer, qa = category_layer(raw, tags, municipality)
            facility_layers[key] = layer.assign(category=key)
            category_qa[key] = qa
            print(
                f"{key}: {number(qa['used'])} facilities, {qa['used_geometry_types']}",
                flush=True,
            )
        del osm, raw

    with stage(timings, "Distances and scores"):
        result, integrated, codes, independent_distance_checks = (
            score_residential_cells(
                original, residential, centers, facility_layers, category_qa
            )
        )

    summary = {
        "method": "euclidean_distance_to_full_osm_geometry",
        "crs": METRIC,
        "h3_resolution": 11,
        "inputs": inputs,
        "versions": {
            "python": sys.version.split()[0],
            "pyrosm": pyrosm.__version__,
            "geopandas": gpd.__version__,
            "shapely": shapely.__version__,
            "matplotlib": matplotlib.__version__,
        },
        "parameters": {
            key: {"label": label, "D_m": scale, "weight": 0.2, "osm_tags_any": tags}
            for key, (label, scale, tags) in SPECS.items()
        },
        "formula": "100 * exp(-d / D); integrated = arithmetic mean of the five scores",
        "class_bounds": CLASS_BOUNDS,
        "class_labels": CLASS_LABELS,
        "all_cells": len(result),
        "residential_cells": int(residential.sum()),
        "residential_status_counts": result.residential_status.value_counts().to_dict(),
        "integrated": describe(integrated),
        "integrated_by_residential_status": {
            status: describe(
                result.loc[
                    result.residential_status.eq(status), "integrated_score"
                ].to_numpy()
            )
            for status in ("confirmed", "uncertain")
        },
        "class_counts": {str(i): int((codes == i).sum()) for i in range(1, 6)},
        "categories": category_qa,
        "independent_distance_checks": independent_distance_checks,
        "limitations": LIMITATIONS,
    }
    with stage(timings, "Geodata export and validation"):
        path = out / "madrid_h3_proximity.geoparquet"
        result.to_parquet(path, index=False, compression="zstd")
        layers = out / "madrid_proximity.gpkg"
        result.to_file(layers, layer="h3_proximity", driver="GPKG", index=False)
        facilities = gpd.GeoDataFrame(
            pd.concat(facility_layers.values(), ignore_index=True), crs=METRIC
        )
        facilities.to_file(
            layers, layer="osm_facilities", driver="GPKG", mode="a", index=False
        )
        municipal.to_file(
            layers, layer="madrid_boundary", driver="GPKG", mode="a", index=False
        )
        check = gpd.read_parquet(path)
        pd.testing.assert_frame_equal(check, result, check_exact=True)
        if check.crs != result.crs:
            raise ValueError("The GeoParquet CRS changed during export.")
        del check
        qgis = gpd.read_file(layers, layer="h3_proximity")
        if qgis.crs != result.crs or not qgis.h3_id.equals(result.h3_id):
            raise ValueError("The QGIS layer did not pass the CRS and H3 ID checks.")
        np.testing.assert_allclose(
            qgis.integrated_score, result.integrated_score, rtol=0, atol=1e-10
        )
        if len(gpd.read_file(layers, layer="osm_facilities")) != len(facilities):
            raise ValueError("The amenity feature count changed during export.")

    with stage(timings, "Map and PDF rendering"):
        render_portfolio(result, municipal, summary, out)
    summary["stages_s"] = timings
    summary["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    (out / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"Finished. Results saved to {out}", flush=True)


if __name__ == "__main__":
    main()
