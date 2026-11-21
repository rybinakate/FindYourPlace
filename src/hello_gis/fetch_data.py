"""
Модуль для скачивания геопространственных данных через OSMnx.
Архитектура: Локальный кэш полигона -> Fallback скачивание -> Расчет bbox -> Скачивание парков -> Очистка.
"""

import time

import requests

from . import config as cfg
from . import deps as d


def _setup_osmnx() -> None:
    """Настраивает OSMnx согласно конфигурации."""
    d.ox.settings.http_user_agent = cfg.OSM_USER_AGENT
    d.ox.settings.requests_timeout = cfg.OSM_TIMEOUT
    d.ox.settings.use_cache = True
    d.ox.settings.log_console = False


def _is_server_alive(base_url: str) -> bool:
    """HTTP Health Check: возвращает True, если сервер отвечает и не отдает 5xx."""
    try:
        response = requests.get(base_url, timeout=3)
        return response.status_code < 500
    except requests.exceptions.RequestException:
        return False


def _download_city_polygon() -> d.gpd.GeoDataFrame | None:
    """
    Скачивает полигон города.
    1. Пытается через Nominatim (структурированный запрос).
    2. Если не вышло, пытается через Overpass API с экспоненциальной задержкой (backoff).
    """
    # Сохраняем fallback при ошибках запроса и обработки ответа.
    # --- ПОПЫТКА 1: Nominatim (быстрый, структурированный) ---
    try:
        d.logging.info(
            f"🌐 Попытка 1: Nominatim для '{cfg.CITY_NAME}, {cfg.CITY_COUNTRY_NAME}'..."
        )
        query = {"city": cfg.CITY_NAME, "country": cfg.CITY_COUNTRY_NAME}
        city_gdf = d.ox.geocode_to_gdf(query)
        if not city_gdf.empty:
            d.logging.info("✅ Полигон получен через Nominatim.")
            return city_gdf
    except Exception as e:  # noqa: BLE001
        d.logging.warning(f"⚠️ Nominatim не сработал: {e}")

    # --- ПОПЫТКА 2: Overpass API с экспоненциальной задержкой (как советует сам сервер при 502) ---
    d.logging.info("🔄 Попытка 2: Overpass API (с backoff при перегрузке)...")
    seed_bbox = d.geom.box(*cfg.COUNTRY_SEED_BBOX)

    for attempt in range(3):  # Максимум 3 попытки
        wait_time = 10 * (2**attempt)  # 10 сек, 20 сек, 40 сек

        for base_url in cfg.OVERPASS_URLS:
            if not _is_server_alive(base_url):
                continue

            d.ox.settings.overpass_url = base_url
            try:
                # Простой запрос только по имени, фильтрация потом в Pandas
                gdf = d.ox.features_from_polygon(
                    seed_bbox, tags={"name": cfg.CITY_NAME}
                )

                if gdf is None or gdf.empty:
                    continue

                # Фильтрация в Pandas: ищем именно муниципалитет
                gdf["admin_level"] = gdf.get("admin_level", "").astype(str)
                gdf["boundary"] = gdf.get("boundary", "").astype(str)
                mask = (gdf["admin_level"] == "8") & (
                    gdf["boundary"] == "administrative"
                )
                gdf_filtered = gdf[mask].copy() if not gdf[mask].empty else gdf.copy()

                # Берем крупнейший по площади
                gdf_proj = gdf_filtered.to_crs(crs=cfg.CRS_LOCAL)
                gdf_proj["area"] = gdf_proj.geometry.area
                gdf_proj = gdf_proj.sort_values("area", ascending=False).head(1)

                result_gdf = gdf_proj.to_crs(crs=cfg.CRS_GEO).drop(columns=["area"])
                d.logging.info(f"✅ Полигон получен через Overpass ({base_url}).")
                return result_gdf

            except Exception as e:  # noqa: BLE001
                d.logging.warning(f"⚠️ Ошибка Overpass ({base_url}): {e}")
                continue

        # Если все сервера отпали на этой попытке, ждем перед следующей
        d.logging.info(
            f"⏳ Все сервера перегружены. Пауза {wait_time} сек перед попыткой {attempt + 2}..."
        )
        time.sleep(wait_time)

    d.logging.error("❌ Не удалось скачать полигон ни одним способом.")
    return None


def _get_city_polygon() -> d.gpd.GeoDataFrame:
    """
    Главная функция получения полигона: сначала ищет локальный кэш,
    если его нет — скачивает и сохраняет.
    """
    # 🚀 ПРОВЕРКА ЛОКАЛЬНОГО КЭША
    if cfg.CITY_BOUNDARY_PATH.exists():
        d.logging.info(f"💾 Найден локальный кэш полигона: {cfg.CITY_BOUNDARY_PATH}")
        gdf = d.gpd.read_file(cfg.CITY_BOUNDARY_PATH)
        if not gdf.empty:
            return gdf
        d.logging.warning("⚠️ Локальный файл пуст, буду скачивать заново.")

    # Скачиваем с нуля
    gdf = _download_city_polygon()
    if gdf is None or gdf.empty:
        d.logging.error("❌ Критическая ошибка: нет полигона города.")
        d.sys.exit(1)

    # 🚀 СОХРАНЕНИЕ В ЛОКАЛЬНЫЙ КЭШ
    cfg.CITY_BOUNDARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(cfg.CITY_BOUNDARY_PATH, driver="GeoJSON")
    d.logging.info(f"💾 Полигон сохранен в локальный кэш: {cfg.CITY_BOUNDARY_PATH}")

    return gdf


def _compute_bbox_from_polygon(
    city_gdf: d.gpd.GeoDataFrame, buffer_ratio: float = cfg.CITY_BUFFER_RATIO
) -> tuple:
    """Вычисляет ФИНАЛЬНЫЙ bbox из полигона города с буфером."""
    minx, miny, maxx, maxy = city_gdf.total_bounds
    x_buf = (maxx - minx) * buffer_ratio
    y_buf = (maxy - miny) * buffer_ratio
    return (minx - x_buf, miny - y_buf, maxx + x_buf, maxy + y_buf)


def _download_parks(bbox: d.Polygon) -> d.gpd.GeoDataFrame | None:
    """Скачивает парки с жестким перебором всех серверов и подробным логом."""
    d.logging.info(f"🔄 Начинаю перебор {len(cfg.OVERPASS_URLS)} серверов...")

    # Переключаем сервер и при ошибках обработки ответа.
    for i, base_url in enumerate(cfg.OVERPASS_URLS, 1):
        d.logging.info(f"🌐 Попытка {i}/{len(cfg.OVERPASS_URLS)}: {base_url}")

        d.ox.settings.overpass_url = base_url
        # Уменьшаем таймаут, чтобы не ждать 3 минуты, если сервер завис
        d.ox.settings.requests_timeout = 60

        try:
            d.logging.info("⏳ Отправка запроса...")
            gdf = d.ox.features_from_polygon(bbox, tags=cfg.OSM_TAGS)

            if gdf is not None and not gdf.empty:
                d.logging.info(f"✅ Успех! Данные получены с {base_url}")
                return gdf
            else:
                d.logging.warning(f"⚠️ Сервер {base_url} вернул пустой ответ.")

        except Exception as e:  # noqa: BLE001
            error_msg = str(e)
            # Проверяем тип ошибки, чтобы дать понятный лог
            if "502" in error_msg or "504" in error_msg:
                d.logging.warning(
                    f"⚠️ Сервер {base_url} перегружен (502/504). Переходим к следующему."
                )
            elif "timed out" in error_msg.lower():
                d.logging.warning(
                    f"⚠️ Сервер {base_url} не ответил вовремя. Переходим к следующему."
                )
            else:
                d.logging.warning(f"⚠️ Ошибка на {base_url}: {error_msg[:100]}...")

            # Явно продолжаем цикл, несмотря на ошибку!
            continue

    d.logging.error("❌ Все серверы Overpass API вернули ошибку или пустой ответ.")
    return None


def _clean_and_prepare(
    gdf: d.gpd.GeoDataFrame, city_boundary: d.gpd.GeoDataFrame
) -> d.gpd.GeoDataFrame:
    """Очистка, расчет площади, ОБРЕЗКА по городу, фильтрация и оптимизация."""

    gdf = gdf[~gdf.geometry.is_empty].copy()
    gdf["name"] = (
        gdf["name"].fillna("Unnamed Park") if "name" in gdf.columns else "Unnamed Park"
    )
    gdf = gdf[["name", "geometry"]].copy()

    gdf_proj = gdf.to_crs(crs=cfg.CRS_LOCAL)
    gdf["area_hectares"] = gdf_proj.geometry.area / 10_000

    d.logging.info("✂️ Обрезка парков по границе города...")
    boundary_matched = city_boundary.to_crs(gdf.crs)
    gdf = d.gpd.clip(gdf, boundary_matched)

    gdf_proj = gdf.to_crs(crs=cfg.CRS_LOCAL)
    gdf["area_hectares"] = gdf_proj.geometry.area / 10_000
    d.logging.info("✅ Обрезка завершена.")

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
        d.logging.info("🔍 Отфильтрованы protected_area по protect_class")

    gdf = gdf[gdf["area_hectares"] > cfg.MIN_FEATURE_AREA_HA].copy()
    d.logging.info(
        f"📉 После фильтрации по площади (>{cfg.MIN_FEATURE_AREA_HA} га) осталось {len(gdf)} объектов."
    )

    gdf["geometry"] = gdf.simplify(tolerance=0.0001, preserve_topology=True)
    d.logging.info("🗜️ Геометрия упрощена для оптимизации памяти.")

    return gdf.to_crs(crs=cfg.CRS_GEO)


def fetch_parks(output_path: d.Path = cfg.PARKS_RAW_PATH) -> None:
    """Главная функция пайплайна."""
    d.logging.basicConfig(level=d.logging.INFO, format="%(levelname)s: %(message)s")
    d.logging.info("🌍 Инициализация OSMnx (production mode)...")

    _setup_osmnx()

    # ЭТАП 1: Получаем полигон города (из локального кэша или скачиваем)
    city_gdf = _get_city_polygon()

    # ЭТАП 2: Расчет идеального bbox из этого полигона
    bbox_coords = _compute_bbox_from_polygon(city_gdf)
    bbox = d.geom.box(*bbox_coords)
    d.logging.info(f"📐 Финальный Bbox вычислен из полигона: {bbox_coords}")

    # ЭТАП 3: Скачивание парков по этому точному bbox
    d.logging.info("🌳 Скачивание зеленых зон по вычисленному bbox...")
    gdf = _download_parks(bbox)

    if gdf is None or gdf.empty:
        d.logging.error("❌ Не удалось получить данные парков.")
        d.sys.exit(1)

    # ЭТАП 4: Очистка, обрезка и сохранение
    d.logging.info("🧹 Очистка, обрезка и подготовка данных...")
    gdf = _clean_and_prepare(gdf, city_boundary=city_gdf)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(output_path, driver="GeoJSON")
    d.logging.info(
        f"✅ Финальный результат: сохранено {len(gdf)} объектов в {output_path}"
    )


if __name__ == "__main__":
    fetch_parks()
