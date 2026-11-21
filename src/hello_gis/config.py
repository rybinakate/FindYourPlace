"""
Конфигурация проекта.
Чтобы сменить город, измените CITY_NAME и CITY_COUNTRY_NAME.
"""

from pathlib import Path
from typing import Final

# ============================================================
# 📂 ПУТИ К ДАННЫМ И ВЫВОДАМ
# ============================================================
# parents[2] поднимает нас из src/hello_gis в корень проекта (hello-gis)
BASE_DIR: Final[Path] = Path(__file__).resolve().parents[2]

DATA_RAW_DIR: Final[Path] = BASE_DIR / "data" / "raw"
DATA_REF_DIR: Final[Path] = (
    BASE_DIR / "data" / "reference"
)  # Справочные данные (полигоны, графы)
OUTPUTS_DIR: Final[Path] = BASE_DIR / "outputs"

# Пути к файлам
PARKS_RAW_PATH: Final[Path] = DATA_RAW_DIR / "parks.geojson"
CITY_BOUNDARY_PATH: Final[Path] = DATA_REF_DIR / "city_boundary.geojson"
NETWORK_CACHE_PATH: Final[Path] = DATA_REF_DIR / "walk_network.graphml"

MAP_OUTPUT: Final[Path] = OUTPUTS_DIR / "parks_map.png"
STATS_OUTPUT: Final[Path] = OUTPUTS_DIR / "park_stats.png"
NETWORK_OUTPUT: Final[Path] = OUTPUTS_DIR / "park_accessibility_analysis.png"

# Автоматическое создание директорий при запуске
for _dir in (DATA_RAW_DIR, DATA_REF_DIR, OUTPUTS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# ============================================================
# 🌍 ПРОЕКЦИИ И ГЕОМЕТРИЯ
# ============================================================
CRS_GEO: Final[str] = "EPSG:4326"
CRS_WEB: Final[str] = "EPSG:3857"
CRS_LOCAL: Final[str] = "EPSG:25830"  # UTM Zone 30N (Мадрид)

DEFAULT_FEATURE_NAME: Final[str] = "Unnamed Park"

# ============================================================
# 🏙️ ПАРАМЕТРЫ ГОРОДА
# ============================================================
CITY_NAME: Final[str] = "Madrid"
CITY_COUNTRY_NAME: Final[str] = "Spain"
CITY_COUNTRY_CODE: Final[str] = "ES"
CITY_BUFFER_RATIO: Final[float] = 0.05
MIN_FEATURE_AREA_HA: Final[float] = 5.0
TOP_N_FEATURES: Final[int] = 15

# Границы всей Испании для универсального поиска любого города страны
COUNTRY_SEED_BBOX: Final[tuple[float, float, float, float]] = (-9.5, 35.8, 4.5, 43.8)

# ============================================================
# 🌐 СЕТЬ И API
# ============================================================
OSM_USER_AGENT: Final[str] = "MadridParksAnalysis/1.0 (Educational GIS Project)"
OSM_TIMEOUT: Final[int] = 180

OVERPASS_URLS: Final[list[str]] = [
    "https://maps.mail.ru/osm/tools/overpass/api",
    "https://overpass-api.de/api",
    "https://overpass.openstreetmap.ru/api",
]

# ============================================================
# 🌳 ТЕГИ ДЛЯ СКАЧИВАНИЯ ПАРКОВ
# ============================================================
OSM_TAGS: Final[dict] = {
    "leisure": "park",
    "landuse": "forest",
    "boundary": "protected_area",
}


# ============================================================
# 🎨 НАСТРОЙКИ ВИЗУАЛИЗАЦИИ
# ============================================================
class VIZ:
    map_figsize: tuple[int, int] = (10, 10)
    stats_figsize: tuple[int, int] = (12, 6)
    dpi: int = 300

    park_fill: str = "#2ca02c"
    park_edge: str = "#ffffff"
    park_linewidth: float = 1.0
    park_alpha: float = 0.7

    grid_color: str = "#888888"
    grid_linestyle: str = "--"
    grid_linewidth: float = 0.5
    grid_alpha: float = 0.6

    frame_color: str = "#333333"
    frame_linewidth: float = 1.5

    label_fontsize: int = 9
    label_color: str = "#333333"
    title_fontsize: int = 16

    map_bg_color: str = "#ffffff"
    stats_bg_color: str = "#f5f5f5"
    stat_colormap: str = "viridis"


# Координаты сетки для Мадрида
GRID_LONS: Final[list[float]] = [-3.8, -3.7, -3.6, -3.5]
GRID_LATS: Final[list[float]] = [40.3, 40.4, 40.5, 40.6]
MAP_BUFFER_RATIO: Final[float] = 0.05
