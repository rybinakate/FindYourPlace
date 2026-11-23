"""
Download geospatial data with OSMnx.
Read the cached boundary or download it, then calculate the bounding box, download parks, and clean the data.
"""

import time

import requests

from . import config as cfg
from . import deps as d


def _setup_osmnx() -> None:
    """Configure OSMnx from the project settings."""
    d.ox.settings.http_user_agent = cfg.OSM_USER_AGENT
    d.ox.settings.requests_timeout = cfg.OSM_TIMEOUT
    d.ox.settings.use_cache = True
    d.ox.settings.log_console = False


def _is_server_alive(base_url: str) -> bool:
    """HTTP Health Check: returns True when the server responds without a 5xx."""
    try:
        response = requests.get(base_url, timeout=3)
        return response.status_code < 500
    except requests.exceptions.RequestException:
        return False


def _download_city_polygon() -> d.gpd.GeoDataFrame | None:
    """
    Download the municipal boundary.
    1. Try Nominatim first (structured query).
    2. Fall back to Overpass API with exponential backoff.
    """
    # Fall back on request or response-processing errors.
    # --- Attempt 1: Nominatim (fast, structured query) ---
    try:
        d.logging.info(
            f"🌐 Attempt 1: querying Nominatim for '{cfg.CITY_NAME}, {cfg.CITY_COUNTRY_NAME}'..."
        )
        query = {"city": cfg.CITY_NAME, "country": cfg.CITY_COUNTRY_NAME}
        city_gdf = d.ox.geocode_to_gdf(query)
        if not city_gdf.empty:
            d.logging.info("✅ Boundary retrieved from Nominatim.")
            return city_gdf
    except Exception as e:  # noqa: BLE001
        d.logging.warning(f"⚠️ Nominatim request failed: {e}")

    # --- Attempt 2: Overpass API with exponential backoff after HTTP 502 ---
    d.logging.info("🔄 Attempt 2: Overpass API with backoff when servers are busy...")
    seed_bbox = d.geom.box(*cfg.COUNTRY_SEED_BBOX)

    for attempt in range(3):  # At most three attempts
        wait_time = 10 * (2**attempt)  # 10 s, 20 s, 40 s

        for base_url in cfg.OVERPASS_URLS:
            if not _is_server_alive(base_url):
                continue

            d.ox.settings.overpass_url = base_url
            try:
                # Query by name only, then filter with pandas
                gdf = d.ox.features_from_polygon(
                    seed_bbox, tags={"name": cfg.CITY_NAME}
                )

                if gdf is None or gdf.empty:
                    continue

                # Filter for the municipality in pandas
                gdf["admin_level"] = gdf.get("admin_level", "").astype(str)
                gdf["boundary"] = gdf.get("boundary", "").astype(str)
                mask = (gdf["admin_level"] == "8") & (
                    gdf["boundary"] == "administrative"
                )
                gdf_filtered = gdf[mask].copy() if not gdf[mask].empty else gdf.copy()

                # Select the feature with the largest area
                gdf_proj = gdf_filtered.to_crs(crs=cfg.CRS_LOCAL)
                gdf_proj["area"] = gdf_proj.geometry.area
                gdf_proj = gdf_proj.sort_values("area", ascending=False).head(1)

                result_gdf = gdf_proj.to_crs(crs=cfg.CRS_GEO).drop(columns=["area"])
                d.logging.info(f"✅ Boundary retrieved from Overpass ({base_url}).")
                return result_gdf

            except Exception as e:  # noqa: BLE001
                d.logging.warning(f"⚠️ Request failed on Overpass ({base_url}): {e}")
                continue

        # Wait before retrying if every server failed in this round
        d.logging.info(
            f"⏳ All servers are busy. Waiting {wait_time} s before attempt {attempt + 2}..."
        )
        time.sleep(wait_time)

    d.logging.error("❌ All methods failed to download the municipal boundary.")
    return None


def _get_city_polygon() -> d.gpd.GeoDataFrame:
    """
    Load the boundary from the local cache if available.
    Otherwise, download and cache it.
    """
    # 🚀 CHECK THE LOCAL CACHE
    if cfg.CITY_BOUNDARY_PATH.exists():
        d.logging.info(f"💾 Local boundary cache found: {cfg.CITY_BOUNDARY_PATH}")
        gdf = d.gpd.read_file(cfg.CITY_BOUNDARY_PATH)
        if not gdf.empty:
            return gdf
        d.logging.warning("⚠️ The local file is empty. Downloading the boundary again.")

    # Download the boundary
    gdf = _download_city_polygon()
    if gdf is None or gdf.empty:
        d.logging.error("❌ The municipal boundary is unavailable.")
        d.sys.exit(1)

    # 🚀 SAVE TO THE LOCAL CACHE
    cfg.CITY_BOUNDARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(cfg.CITY_BOUNDARY_PATH, driver="GeoJSON")
    d.logging.info(f"💾 Boundary saved to the local cache: {cfg.CITY_BOUNDARY_PATH}")

    return gdf


def _compute_bbox_from_polygon(
    city_gdf: d.gpd.GeoDataFrame, buffer_ratio: float = cfg.CITY_BUFFER_RATIO
) -> tuple:
    """Compute the study bounding box from the buffered municipal boundary."""
    minx, miny, maxx, maxy = city_gdf.total_bounds
    x_buf = (maxx - minx) * buffer_ratio
    y_buf = (maxy - miny) * buffer_ratio
    return (minx - x_buf, miny - y_buf, maxx + x_buf, maxy + y_buf)


def _download_parks(bbox: d.Polygon) -> d.gpd.GeoDataFrame | None:
    """Download parks by trying each configured server and logging the result."""
    d.logging.info(f"🔄 Trying {len(cfg.OVERPASS_URLS)} servers...")

    # Try the next server on response-processing errors as well.
    for i, base_url in enumerate(cfg.OVERPASS_URLS, 1):
        d.logging.info(f"🌐 Attempt {i}/{len(cfg.OVERPASS_URLS)}: {base_url}")

        d.ox.settings.overpass_url = base_url
        # Use a shorter timeout to avoid waiting three minutes for an unresponsive server
        d.ox.settings.requests_timeout = 60

        try:
            d.logging.info("⏳ Sending request...")
            gdf = d.ox.features_from_polygon(bbox, tags=cfg.OSM_TAGS)

            if gdf is not None and not gdf.empty:
                d.logging.info(f"✅ Data retrieved from {base_url}")
                return gdf
            else:
                d.logging.warning(f"⚠️ Server {base_url} returned an empty response.")

        except Exception as e:  # noqa: BLE001
            error_msg = str(e)
            # Log a message specific to the error
            if "502" in error_msg or "504" in error_msg:
                d.logging.warning(
                    f"⚠️ Server {base_url} is overloaded (502/504). Trying the next server."
                )
            elif "timed out" in error_msg.lower():
                d.logging.warning(
                    f"⚠️ Server {base_url} timed out. Trying the next server."
                )
            else:
                d.logging.warning(f"⚠️ Error on {base_url}: {error_msg[:100]}...")

            # Continue to the next server after an error!
            continue

    d.logging.error(
        "❌ Every Overpass API server returned an error or an empty response."
    )
    return None


def _clean_and_prepare(
    gdf: d.gpd.GeoDataFrame, city_boundary: d.gpd.GeoDataFrame
) -> d.gpd.GeoDataFrame:
    """Clean geometries, calculate areas, clip to the city, filter, and simplify."""

    gdf = gdf[~gdf.geometry.is_empty].copy()
    gdf["name"] = (
        gdf["name"].fillna("Unnamed Park") if "name" in gdf.columns else "Unnamed Park"
    )
    gdf = gdf[["name", "geometry"]].copy()

    gdf_proj = gdf.to_crs(crs=cfg.CRS_LOCAL)
    gdf["area_hectares"] = gdf_proj.geometry.area / 10_000

    d.logging.info("✂️ Clipping parks to the municipal boundary...")
    boundary_matched = city_boundary.to_crs(gdf.crs)
    gdf = d.gpd.clip(gdf, boundary_matched)

    gdf_proj = gdf.to_crs(crs=cfg.CRS_LOCAL)
    gdf["area_hectares"] = gdf_proj.geometry.area / 10_000
    d.logging.info("✅ Clipping complete.")

    if "boundary" in gdf.columns:
        is_protected = (gdf["boundary"] == "protected_area").fillna(False)
        valid_class = (
            gdf.get("protect_class")
            .astype(str)
            .str.match(r"^(?:[1-9]|1[1-9]|9[7-9])$")
            .fillna(False)
        )
        keep_mask = (~is_protected) | (is_protected & valid_class)
        gdf = gdf[keep_mask].copy()
        d.logging.info("🔍 Filtered protected_area features by protect_class")

    gdf = gdf[gdf["area_hectares"] > cfg.MIN_FEATURE_AREA_HA].copy()
    d.logging.info(
        f"📉 After area filtering (>{cfg.MIN_FEATURE_AREA_HA} ha), {len(gdf)} features remain."
    )

    gdf["geometry"] = gdf.simplify(tolerance=0.0001, preserve_topology=True)
    d.logging.info("🗜️ Geometries simplified to reduce memory use.")

    return gdf.to_crs(crs=cfg.CRS_GEO)


def fetch_parks(output_path: d.Path = cfg.PARKS_RAW_PATH) -> None:
    """Run the data download and preparation pipeline."""
    d.logging.basicConfig(level=d.logging.INFO, format="%(levelname)s: %(message)s")
    d.logging.info("🌍 Initializing OSMnx (production mode)...")

    _setup_osmnx()

    # STEP 1: Load the municipal boundary (from cache or by downloading it)
    city_gdf = _get_city_polygon()

    # STEP 2: Compute a bounding box from the boundary
    bbox_coords = _compute_bbox_from_polygon(city_gdf)
    bbox = d.geom.box(*bbox_coords)
    d.logging.info(f"📐 Bounding box calculated from the boundary: {bbox_coords}")

    # STEP 3: Download parks using the calculated bbox
    d.logging.info("🌳 Downloading green spaces within the calculated bbox...")
    gdf = _download_parks(bbox)

    if gdf is None or gdf.empty:
        d.logging.error("❌ Could not retrieve park data.")
        d.sys.exit(1)

    # STEP 4: Clean, clip, and save
    d.logging.info("🧹 Cleaning, clipping, and preparing the data...")
    gdf = _clean_and_prepare(gdf, city_boundary=city_gdf)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(output_path, driver="GeoJSON")
    d.logging.info(f"✅ Saved {len(gdf)} features to {output_path}")


if __name__ == "__main__":
    fetch_parks()
