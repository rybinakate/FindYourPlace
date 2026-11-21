"""
ETL пайплайн: Скачивание OSM PBF -> Фильтрация через Pyrosm -> DuckDB -> GeoParquet в S3 Filebase.
Еженедельный запуск запланирован в дорожной карте.
"""

import os
from pathlib import Path

import duckdb
import pandas as pd
import requests
from dotenv import load_dotenv
from pyrosm import OSM
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 1. Загрузка переменных окружения
env_path = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=env_path)

FILEBASE_KEY = os.getenv("FILEBASE_KEY")
FILEBASE_SECRET = os.getenv("FILEBASE_SECRET")

print("🔍 Проверка Filebase credentials:")
print("FILEBASE_KEY загружен:", bool(FILEBASE_KEY))
print("FILEBASE_SECRET загружен:", bool(FILEBASE_SECRET))

if not FILEBASE_KEY or not FILEBASE_SECRET:
    raise ValueError(
        "❌ Ошибка: Установите переменные окружения FILEBASE_KEY и FILEBASE_SECRET"
    )

print("✅ Переменные загружены успешно!")


# ================= КОНФИГУРАЦИЯ =================
CITY_NAME = "madrid"
PBF_URL = f"https://download.geofabrik.de/europe/spain/{CITY_NAME}-latest.osm.pbf"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_PBF_PATH = RAW_DATA_DIR / f"{CITY_NAME}-latest.osm.pbf"

S3_BUCKET = "osmspain"
S3_ENDPOINT = "s3.filebase.io"
AWS_KEY = FILEBASE_KEY
AWS_SECRET = FILEBASE_SECRET
# ================================================


def get_robust_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def download_pbf(force_download: bool = False) -> None:
    """Скачивает PBF файл, если его нет или если принудительно запрошено обновление."""

    """# 🚀 УМНАЯ ПРОВЕРКА: если файл есть и он больше 10 МБ, пропускаем скачивание
    if LOCAL_PBF_PATH.exists() and not force_download:
        file_size_mb = LOCAL_PBF_PATH.stat().st_size / (1024 * 1024)
        if file_size_mb > 10:
            print(f"✅ Файл уже существует ({file_size_mb:.2f} MB). Пропускаем скачивание.")
            return"""

    print(f"⬇️ Начинаем скачивание {CITY_NAME}-latest.osm.pbf...")
    if LOCAL_PBF_PATH.exists():
        print("⚠️ Найден старый/битый файл, удаляем...")
        LOCAL_PBF_PATH.unlink()

    session = get_robust_session()
    with session.get(PBF_URL, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(LOCAL_PBF_PATH, "wb") as file:
            for chunk in response.iter_content(chunk_size=8192 * 16):
                if chunk:
                    file.write(chunk)

    file_size_mb = LOCAL_PBF_PATH.stat().st_size / (1024 * 1024)
    print(f"✅ Скачивание завершено! ({file_size_mb:.2f} MB)")


def process_and_upload_to_s3() -> None:
    print("🦆 Инициализация DuckDB...")

    db_path = PROJECT_ROOT / "data" / "reference" / "osm_madrid.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))

    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")

    con.execute("SET s3_region='auto';")
    con.execute(f"SET s3_endpoint='{S3_ENDPOINT}';")
    con.execute("SET s3_url_style='path';")
    con.execute(f"SET s3_access_key_id='{AWS_KEY}';")
    con.execute(f"SET s3_secret_access_key='{AWS_SECRET}';")

    print("🧪 Тестируем запись в Filebase...")

    con.execute("""
        COPY (
            SELECT
                1 AS test_id,
                'hello-gis' AS test_name
        )
        TO 's3://osmspain/test/duckdb_connection_test.parquet'
        (FORMAT PARQUET);
    """)

    print("✅ Тестовая запись в Filebase прошла успешно!")

    print("🔄 Чтение PBF через pyrosm...")
    osm = OSM(str(LOCAL_PBF_PATH))

    # 1. ПАРКИ
    print("   📍 Извлекаем парки...")

    all_landuse = osm.get_data_by_custom_criteria(
        custom_filter={
            "leisure": ["park"],
            "landuse": ["forest", "recreation_ground"],
            "boundary": ["protected_area"],
        },
        tags_as_columns=["name"],
        filter_type="keep",
    )

    print(f"      Всего объектов landuse: {len(all_landuse)}")
    print(f"      Колонки: {all_landuse.columns.tolist()}")

    mask_leisure = (
        all_landuse["leisure"].isin(["park"])
        if "leisure" in all_landuse.columns
        else pd.Series(False, index=all_landuse.index)
    )

    mask_landuse = (
        all_landuse["landuse"].isin(["forest", "recreation_ground"])
        if "landuse" in all_landuse.columns
        else pd.Series(False, index=all_landuse.index)
    )

    mask_boundary = (
        all_landuse["boundary"].isin(["protected_area"])
        if "boundary" in all_landuse.columns
        else pd.Series(False, index=all_landuse.index)
    )

    mask_parks = mask_leisure | mask_landuse | mask_boundary
    parks_gdf = all_landuse[mask_parks].copy()

    # Пока не удаляем безымянные объекты
    print(f"      Найдено парков/зелёных зон: {len(parks_gdf)}")

    # 2. POI
    print("   📍 Извлекаем POI...")
    all_pois = osm.get_pois()

    print(f"      Всего объектов POI: {len(all_pois)}")
    print(f"      Колонки: {all_pois.columns.tolist()}")

    mask_amenity = (
        all_pois["amenity"].isin(["school", "kindergarten", "hospital", "clinic"])
        if "amenity" in all_pois.columns
        else pd.Series(False, index=all_pois.index)
    )

    pois_gdf = all_pois[mask_amenity].copy()

    if "name" in pois_gdf.columns:
        pois_gdf = pois_gdf[pois_gdf["name"].notna()]
    else:
        print("⚠️ Колонка 'name' отсутствует в POI")
        pois_gdf = pois_gdf.iloc[0:0]

    print(f"      Найдено {len(pois_gdf)} POI")

    # 3. МЕТРО
    print("   📍 Извлекаем метро...")

    metro_gdf = osm.get_data_by_custom_criteria(
        custom_filter='["railway"="station"]["station"="subway"]', filter_type="keep"
    )
    print(f"      Найдено {len(metro_gdf)} станций метро")

    # Подготовка геометрии для DuckDB
    parks_gdf_wkb = parks_gdf.copy()
    pois_gdf_wkb = pois_gdf.copy()
    metro_gdf_wkb = metro_gdf.copy()

    parks_gdf_wkb["geometry"] = parks_gdf_wkb.geometry.to_wkb()
    pois_gdf_wkb["geometry"] = pois_gdf_wkb.geometry.to_wkb()
    metro_gdf_wkb["geometry"] = metro_gdf_wkb.geometry.to_wkb()

    # Регистрируем DataFrame в DuckDB
    con.register("parks_gdf_wkb", parks_gdf_wkb)
    con.register("pois_gdf_wkb", pois_gdf_wkb)
    con.register("metro_gdf_wkb", metro_gdf_wkb)

    # Загружаем в DuckDB
    print("   💾 Загружаем в DuckDB...")
    con.execute("""
    CREATE OR REPLACE TABLE parks AS
    SELECT
        * EXCLUDE (geometry),
        ST_GeomFromWKB(geometry) AS geometry
    FROM parks_gdf_wkb
    """)

    con.execute("""
        CREATE OR REPLACE TABLE pois AS
        SELECT
            * EXCLUDE (geometry),
            ST_GeomFromWKB(geometry) AS geometry
        FROM pois_gdf_wkb
    """)

    con.execute("""
        CREATE OR REPLACE TABLE metro AS
        SELECT
            * EXCLUDE (geometry),
            ST_GeomFromWKB(geometry) AS geometry
        FROM metro_gdf_wkb
    """)

    print(f"✅ Локальная база обновлена: {db_path}")

    parks_count = con.execute("SELECT COUNT(*) FROM parks").fetchone()[0]
    pois_count = con.execute("SELECT COUNT(*) FROM pois").fetchone()[0]
    metro_count = con.execute("SELECT COUNT(*) FROM metro").fetchone()[0]

    print(f"   📊 parks: {parks_count} записей")
    print(f"   📊 pois: {pois_count} записей")
    print(f"   📊 metro: {metro_count} записей")

    print("📤 Экспорт в S3 Filebase...")

    con.execute(
        f"COPY parks TO 's3://{S3_BUCKET}/osm/{CITY_NAME}_parks.geoparquet' (FORMAT PARQUET);"
    )
    con.execute(
        f"COPY pois TO 's3://{S3_BUCKET}/osm/{CITY_NAME}_pois.geoparquet' (FORMAT PARQUET);"
    )
    con.execute(
        f"COPY metro TO 's3://{S3_BUCKET}/osm/{CITY_NAME}_metro.geoparquet' (FORMAT PARQUET);"
    )

    con.close()
    print("✅ Данные успешно загружены в S3!")


def cleanup() -> None:
    # Мы НЕ удаляем файл при отладке, чтобы не качать его снова.
    # Если захотите очистить место, раскомментируйте строку ниже.
    # if LOCAL_PBF_PATH.exists():
    #     LOCAL_PBF_PATH.unlink()
    #     print("🧹 Временный PBF файл удален.")
    pass


if __name__ == "__main__":
    if not AWS_KEY or not AWS_SECRET:
        print(
            "❌ Ошибка: Установите переменные окружения FILEBASE_KEY и FILEBASE_SECRET"
        )
        raise SystemExit(1)

    # force_download=False означает: "используй существующий файл, если он есть"
    download_pbf(force_download=False)
    process_and_upload_to_s3()
    cleanup()
