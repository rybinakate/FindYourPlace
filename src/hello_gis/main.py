"""
Entry point for Hello-GIS.
Coordinates the data download and visualization modules.
"""

from . import deps as d
from .fetch_data import fetch_parks
from .visualize import main as visualize_main


def calculate_area(geom: d.BaseGeometry) -> float:
    """
    Calculate the area of a geometry.
    Demonstrate the typed imports from deps.py.
    """
    return float(geom.area)


def run_full_pipeline() -> None:
    """
    Run the full pipeline, from downloading data to creating visualizations.
    """
    d.logging.basicConfig(level=d.logging.INFO, format="%(levelname)s: %(message)s")
    print("🚀 Running the full Hello-GIS pipeline...\n")

    # 1. Download and prepare data
    fetch_parks()
    print()

    # 2. Create the map and charts
    visualize_main()


def main() -> None:
    """Run the application."""
    # Demonstrate a geometry operation (optional)
    test_polygon = d.Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    area = calculate_area(test_polygon)
    print(f"📐 Area of the 1 × 1 test polygon: {area}\n")

    # Run the main pipeline
    run_full_pipeline()


if __name__ == "__main__":
    main()
