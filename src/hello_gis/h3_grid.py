"""Build H3 cells and classify preliminary residential evidence for Madrid."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import geopandas as gpd
from shapely.geometry import Polygon

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

H3_RESOLUTION = 11
GEOGRAPHIC_CRS = "EPSG:4326"
METRIC_CRS = "EPSG:25830"
RESIDENTIAL_TYPES = frozenset(
    {
        "apartments",
        "residential",
        "house",
        "detached",
        "semidetached_house",
        "terrace",
        "bungalow",
        "dormitory",
    }
)
UNCERTAIN_TYPES = frozenset({"yes"})


def _validate_polygons(frame: gpd.GeoDataFrame, label: str) -> None:
    if frame.crs is None:
        raise ValueError(f"{label} must have a CRS.")
    if (
        frame.geometry.isna().any()
        or frame.is_empty.any()
        or not frame.is_valid.all()
        or not frame.geom_type.isin(["Polygon", "MultiPolygon"]).all()
    ):
        raise ValueError(f"{label} must contain valid nonempty polygon geometries.")


def _study_geometry(area: gpd.GeoDataFrame, crs: str) -> BaseGeometry:
    _validate_polygons(area, "Study area")
    if area.empty:
        raise ValueError("Study area must contain at least one polygon.")
    return area.to_crs(crs).geometry.union_all()


def build_h3_grid(area: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Return full resolution-11 cells whose H3 centers lie in the study area.

    The caller supplies the municipal polygon, or its intersection with a
    smaller control window. Coordinates are WGS84 in longitude/latitude order.
    """
    study = _study_geometry(area, GEOGRAPHIC_CRS)
    try:
        import h3
    except ModuleNotFoundError as error:
        if error.name != "h3":
            raise
        raise RuntimeError(
            "Grid generation requires the h3 Python package (v4 API)."
        ) from error

    ids = sorted(h3.geo_to_cells(study.__geo_interface__, H3_RESOLUTION))
    centers = [h3.cell_to_latlng(cell) for cell in ids]
    grid = gpd.GeoDataFrame(
        {
            "h3_id": ids,
            "h3_resolution": H3_RESOLUTION,
            "center_lon": [lon for lat, lon in centers],
            "center_lat": [lat for lat, lon in centers],
        },
        geometry=[
            Polygon([(lon, lat) for lat, lon in h3.cell_to_boundary(cell)])
            for cell in ids
        ],
        crs=GEOGRAPHIC_CRS,
    )
    _validate_polygons(grid, "H3 grid")
    return grid


def classify_residential_cells(
    cells: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    area: gpd.GeoDataFrame,
    *,
    min_overlap_m2: float = 10.0,
) -> gpd.GeoDataFrame:
    """Assign statuses from individual footprints clipped to the study area.

    A building qualifies at or above min_overlap_m2. Confirmed evidence takes
    precedence, while uncertain evidence is kept independently for hatching.
    The geometry and CRS of the input cells are retained.
    """
    if not math.isfinite(min_overlap_m2) or min_overlap_m2 <= 0:
        raise ValueError("min_overlap_m2 must be finite and positive.")
    _validate_polygons(cells, "Cells")
    _validate_polygons(buildings, "Buildings")
    study = _study_geometry(area, METRIC_CRS)
    if (
        "h3_id" not in cells
        or cells["h3_id"].isna().any()
        or not cells["h3_id"].is_unique
    ):
        raise ValueError("Cells must have unique non-null h3_id values.")
    if "building" not in buildings:
        raise ValueError("Buildings must include the OSM building tag.")

    result = cells.copy()
    tags = buildings["building"].astype("string").str.strip().str.lower()
    eligible = tags.isin(RESIDENTIAL_TYPES | UNCERTAIN_TYPES)
    candidates = buildings.loc[eligible, [buildings.geometry.name]].to_crs(METRIC_CRS)
    candidates["evidence"] = tags[eligible].isin(RESIDENTIAL_TYPES).to_numpy()
    candidates = candidates.reset_index(drop=True)
    candidates.geometry = candidates.geometry.intersection(study)
    candidates = candidates.loc[~candidates.is_empty]

    metric_cells = cells[["h3_id", cells.geometry.name]].to_crs(METRIC_CRS)
    pairs = metric_cells.sjoin(candidates, predicate="intersects")
    footprints = candidates.geometry.reindex(pairs["index_right"]).reset_index(
        drop=True
    )
    overlap = pairs.geometry.reset_index(drop=True).intersection(footprints).area
    qualifying = pairs.iloc[(overlap >= min_overlap_m2).to_numpy()]
    confirmed = qualifying.loc[qualifying["evidence"], "h3_id"]
    uncertain = qualifying.loc[~qualifying["evidence"], "h3_id"]
    result["has_confirmed_building"] = result["h3_id"].isin(confirmed)
    result["has_uncertain_building"] = result["h3_id"].isin(uncertain)
    result["residential_status"] = "excluded"
    result.loc[result["has_uncertain_building"], "residential_status"] = "uncertain"
    result.loc[result["has_confirmed_building"], "residential_status"] = "confirmed"
    return result
