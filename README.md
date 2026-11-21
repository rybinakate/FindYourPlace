# Hello GIS

A GIS prototype for exploring walking access to parks in Madrid, Spain. The repository also documents the design for a residential accessibility map covering six amenity categories.

## Current prototype

- The park workflow fetches and prepares OpenStreetMap data and produces a park map and statistics.
- A separate OSMnx and NetworkX workflow reads park GeoJSON and produces a GeoJSON layer of estimated gaps in park coverage and a PNG map.
- The Madrid ETL module extracts parks, points of interest and metro features, stores geometries in DuckDB Spatial and exports GeoParquet to Filebase. The park accessibility workflow currently reads GeoJSON.

[Example park accessibility map](outputs/park_accessibility_analysis.png) · [Park inventory map](outputs/parks_map.png)

The accessibility map comes from an earlier prototype run. It combines buffers around reachable graph nodes, and its coverage percentages describe the prototype study rectangle. See the [methodology review](docs/NETWORK_ANALYSIS_REVIEW.md) for limitations.

## Local commands

The project uses Python 3.14+, uv and Make. Run `uv sync` to install dependencies. The data and analysis commands may contact OSM services or map tile providers.

| Command | Purpose |
| --- | --- |
| `make run` | Fetch park data and generate park visualizations. |
| `make data` | Prepare park and city-boundary GeoJSON for the network workflow. |
| `make network` | Run park accessibility analysis after preparing the GeoJSON inputs. |
| `make check` | Run mypy and Ruff using the locked uv development dependencies. |
| `make test` | Report that automated tests are pending and exit with a nonzero status. |

The project currently has no automated test coverage. `uv run hello-gis` runs the same workflow as `make run`.

## Design and roadmap

- [Project brief and target GIS architecture](docs/PROJECT_BRIEF.md)
- [Roadmap and acceptance criteria](docs/ROADMAP.md)
- [Park accessibility prototype review](docs/NETWORK_ANALYSIS_REVIEW.md)
