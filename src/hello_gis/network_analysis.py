"""
Модуль сетевого анализа доступности парков.
Рассчитывает метрики покрытия территории, выявляет "парковые пустыни"
и строит изохроны доступности. Совместим с OSMnx 2.x.
"""

import networkx as nx
import requests
from shapely.geometry import box
from shapely.ops import unary_union

from . import config as cfg
from . import deps as d


def _is_server_alive(base_url: str) -> bool:
    """Быстрая проверка доступности сервера."""
    try:
        response = requests.get(base_url, timeout=3)
        return response.status_code < 500
    except requests.exceptions.RequestException:
        return False


def get_walk_network() -> nx.MultiDiGraph:
    """
    Скачивает пешеходную сеть по tight bbox из полигона города.
    Кэширует результат в GraphML для мгновенной загрузки при повторных запусках.
    """
    d.ox.settings.use_cache = True
    d.ox.settings.log_console = False
    d.ox.settings.requests_timeout = 300

    # 1. 🚀 ПРОВЕРКА КЭША: если сеть уже скачана, загружаем из файла
    if cfg.NETWORK_CACHE_PATH.exists():
        d.logging.info(f"💾 Найден кэш сети: {cfg.NETWORK_CACHE_PATH}")
        d.logging.info("⏳ Загрузка из файла (1-2 секунды)...")
        G = d.ox.load_graphml(str(cfg.NETWORK_CACHE_PATH))
        d.logging.info(
            f"✅ Сеть загружена из кэша: {G.number_of_nodes()} узлов, {G.number_of_edges()} ребер"
        )
        return G

    # 2. Читаем полигон Мадрида из кэша
    if not cfg.CITY_BOUNDARY_PATH.exists():
        d.logging.error("❌ Сначала запустите 'make data'!")
        d.sys.exit(1)

    city_gdf = d.gpd.read_file(cfg.CITY_BOUNDARY_PATH)

    # 3. Вычисляем tight bbox из полигона
    minx, miny, maxx, maxy = city_gdf.total_bounds
    tight_bbox = (minx, miny, maxx, maxy)

    d.logging.info(
        f"🌐 Скачивание сети по tight bbox: ({minx:.2f}, {miny:.2f}, {maxx:.2f}, {maxy:.2f})..."
    )
    d.logging.info("⏳ Это займёт 1-3 минуты (только первый раз)...")

    # 4. Скачиваем сеть
    G = d.ox.graph_from_bbox(bbox=tight_bbox, network_type="all", simplify=True)

    G = d.ox.distance.add_edge_lengths(G)

    # 5. 🚀 СОХРАНЕНИЕ В КЭШ
    cfg.NETWORK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    d.ox.save_graphml(G, filepath=str(cfg.NETWORK_CACHE_PATH))
    d.logging.info(f"💾 Сеть сохранена в кэш: {cfg.NETWORK_CACHE_PATH}")

    d.logging.info(
        f"✅ Сеть загружена: {G.number_of_nodes()} узлов, {G.number_of_edges()} ребер"
    )
    return G


def calculate_accessibility_metrics(
    G: nx.MultiDiGraph,
    parks_gdf: d.gpd.GeoDataFrame,
    max_walk_meters: float = 800,
    park_entry_buffer_m: float = 50,  # буфер для поиска входов в парк
    simplify_tolerance_m: float = 50,  # 🚀 НОВОЕ: допуск упрощения в метрах
) -> dict:
    """

    Рассчитывает изохроны доступности для ВСЕХ парков.
    Стартовые точки — узлы сети ВНУТРИ полигона парка (а не центроид).
    """
    d.logging.info(
        f"🚶 Расчет изохрон {max_walk_meters} м для {len(parks_gdf)} парков..."
    )

    # 1. Проецируем в метрическую систему
    G_proj = d.ox.project_graph(G, to_crs=cfg.CRS_LOCAL)
    parks_proj = parks_gdf.to_crs(crs=cfg.CRS_LOCAL)

    # 2. Изучаемая территория
    minx, miny, maxx, maxy = parks_proj.total_bounds
    study_area = box(minx - 1000, miny - 1000, maxx + 1000, maxy + 1000)
    total_area_sq_m = study_area.area

    # 3. Получаем узлы графа как GeoDataFrame
    nodes_gdf = d.ox.graph_to_gdfs(G_proj, edges=False)

    accessible_polygons = []

    # 4. Для каждого парка строим изохрону от ПЕРИМЕТРА
    for idx, park in parks_proj.iterrows():
        park_geom = park.geometry

        # 🚀 НАХОДИМ ВСЕ УЗЛЫ СЕТИ ВНУТРИ ПАРКА (с небольшим буфером для входов)
        # Расширяем полигон парка на 50м, чтобы захватить узлы на границе
        park_with_buffer = park_geom.buffer(park_entry_buffer_m)

        # Находим узлы, которые лежат внутри расширенного полигона
        mask = nodes_gdf.geometry.within(park_with_buffer)
        entry_nodes = nodes_gdf[mask]

        if entry_nodes.empty:
            # Fallback: если внутри парка нет узлов, берем ближайший снаружи
            centroid = park_geom.centroid
            nearest = d.ox.distance.nearest_nodes(G_proj, X=centroid.x, Y=centroid.y)
            entry_nodes = nodes_gdf.loc[[nearest]]

        # 5. Для каждого входного узла находим достижимые узлы
        all_reachable = set()
        for node_id in entry_nodes.index:
            reachable = nx.single_source_dijkstra_path_length(
                G_proj, source=node_id, cutoff=max_walk_meters, weight="length"
            )
            all_reachable.update(reachable.keys())

        if not all_reachable:
            continue

        # 6. Строим изохрону: буфер 100м вокруг всех достижимых узлов
        reachable_coords = nodes_gdf.loc[list(all_reachable)].geometry
        park_isochrone = reachable_coords.buffer(100).unary_union

        accessible_polygons.append(park_isochrone)

    # 7. Объединяем все изохроны
    if not accessible_polygons:
        d.logging.warning("⚠️ Не удалось построить ни одну изохрону")
        total_accessible_area = box(0, 0, 0, 0)
    else:
        total_accessible_area = unary_union(accessible_polygons)

    # 8. Обрезаем по границам изучаемой территории
    total_accessible_area = total_accessible_area.intersection(study_area)
    accessible_area_sq_m = total_accessible_area.area

    # 9. Парковые пустыни
    park_deserts = study_area.difference(total_accessible_area)
    desert_area_sq_m = park_deserts.area

    # 10. 🚀 УПРОЩЕНИЕ ГЕОМЕТРИИ для визуальной гладкости
    # Применяем ПОСЛЕ всех расчетов площадей, чтобы не искажать метрики
    if simplify_tolerance_m > 0:
        d.logging.info(f"🗜️ Упрощение геометрии (tolerance={simplify_tolerance_m}м)...")
        total_accessible_area = total_accessible_area.simplify(
            tolerance=simplify_tolerance_m, preserve_topology=True
        )
        park_deserts = park_deserts.simplify(
            tolerance=simplify_tolerance_m, preserve_topology=True
        )

    d.logging.info(
        f"✅ Построено {len(accessible_polygons)} изохрон. "
        f"Доступно: {accessible_area_sq_m / 10_000:,.0f} га ({accessible_area_sq_m / total_area_sq_m * 100:.1f}%)"
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
    """Сохраняет геометрию парковых пустынь в GeoJSON для дальнейшего анализа."""
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
    d.logging.info(f"💾 Геометрия 'парковых пустынь' сохранена: {output_path}")


def plot_accessibility_and_deserts(
    parks_gdf: d.gpd.GeoDataFrame,
    metrics: dict,
    output_path: d.Path = cfg.OUTPUTS_DIR / "park_accessibility_analysis.png",
) -> None:
    """Визуализирует доступность и выделяет парковые пустыни."""
    import contextily as ctx  # Локальный импорт, чтобы не ломать make data

    d.logging.info("🎨 Визуализация анализа доступности...")

    fig, ax = d.plt.subplots(figsize=(10, 10))

    # Проецируем для отображения в Web Mercator
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

    # Устанавливаем границы карты с небольшим буфером
    minx, miny, maxx, maxy = parks_web.total_bounds
    ax.set_xlim(minx - (maxx - minx) * 0.1, maxx + (maxx - minx) * 0.1)
    ax.set_ylim(miny - (maxy - miny) * 0.1, maxy + (maxy - miny) * 0.1)

    # 1. Подложка (Esri World Gray Canvas)
    ctx.add_basemap(
        ax,
        source=ctx.providers.Esri.WorldGrayCanvas,
        crs=cfg.CRS_WEB,
        zoom=13,
    )

    # Сдвигаем подпись источника
    for text in ax.texts:
        if "Esri" in text.get_text() or "OpenStreetMap" in text.get_text():
            x, y = text.get_position()
            text.set_position((x + 0.02, y + 0.02))
            text.set_bbox(
                {"facecolor": "white", "alpha": 0.85, "edgecolor": "none", "pad": 1.5}
            )
            text.set_fontsize(7)
            text.set_color("#555555")

    # 2. Парковые пустыни (красный, полупрозрачный, БЕЗ контура)
    d.gpd.GeoSeries([deserts_web]).plot(
        ax=ax, color="#e5b85e", edgecolor="none", alpha=0.3, zorder=1
    )

    # 3. Зоны доступности (зеленый, полупрозрачный, БЕЗ контура)
    d.gpd.GeoSeries([accessible_web]).plot(
        ax=ax, color="#048608", edgecolor="none", alpha=0.3, zorder=2
    )

    # 4. Парки (темно-зеленые, с белой обводкой для контраста)
    parks_web.plot(ax=ax, color="#043e04", zorder=3)

    # 6. Блок с метриками
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
    d.logging.info(f"✅ Карта анализа сохранена: {output_path}")


def run_network_analysis() -> None:
    """Главная функция пайплайна."""
    d.logging.basicConfig(level=d.logging.INFO, format="%(levelname)s: %(message)s")

    if not cfg.PARKS_RAW_PATH.exists():
        d.logging.error("⚠️ Сначала скачайте данные парков: make data")
        return

    parks = d.gpd.read_file(cfg.PARKS_RAW_PATH)
    parks = parks[
        (parks["name"] != cfg.DEFAULT_FEATURE_NAME) & (parks["area_hectares"] > 1.0)
    ]

    if parks.empty:
        d.logging.error("⚠️ Не найдено парков для анализа.")
        return

    # 1. Сеть
    G = get_walk_network()

    # 2. Метрики (800 метров ≈ 10 минут ходьбы)
    metrics = calculate_accessibility_metrics(G, parks, max_walk_meters=800)

    # 3. Вывод метрик в консоль
    print("\n" + "=" * 50)
    print("📊 РЕЗУЛЬТАТЫ АНАЛИЗА ДОСТУПНОСТИ (10 мин ходьбы)")
    print("=" * 50)
    print(f"📍 Общая площадь анализа: {metrics['total_area_ha']:,.0f} га")
    print(
        f"✅ Доступная территория:  {metrics['accessible_area_ha']:,.0f} га ({metrics['accessible_pct']:.1f}%)"
    )
    print(
        f"❌ Парковые пустыни:      {metrics['desert_area_ha']:,.0f} га ({metrics['desert_pct']:.1f}%)"
    )
    print(
        f"📈 Индекс доступности:    {metrics['accessibility_index']:.2f} (идеал = 1.0)"
    )
    print("=" * 50 + "\n")

    # 4. Сохранение данных пустынь
    desert_path = cfg.DATA_RAW_DIR / "park_deserts.geojson"
    save_park_deserts(metrics, desert_path)

    # 5. Визуализация
    plot_accessibility_and_deserts(parks, metrics)

    d.logging.info("✨ Сетевой анализ завершен!")


if __name__ == "__main__":
    run_network_analysis()
