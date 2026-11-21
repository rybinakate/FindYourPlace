# Park accessibility prototype review

## Implementation and evidence

[network_analysis.py](../src/hello_gis/network_analysis.py) loads the parks GeoJSON and filters out default-named parks and those of 1 ha or less. It uses an existing GraphML cache when available. To build a graph, it takes the city boundary's bounding box, requests OSMnx `network_type="all"` with simplification enabled, and recalculates edge lengths. The graph and park data are projected to EPSG:25830, configured in [config.py](../src/hello_gis/config.py).

For each park, graph nodes within a 50 m buffer of its geometry serve as access proxies. If none are found, the algorithm uses the graph node nearest the park centroid. It runs NetworkX Dijkstra from each proxy with an 800 m cutoff and edge weight `length`, then unions 100 m buffers around the reachable nodes. It clips coverage to the bounding rectangle of the retained parks expanded by 1 km, computes its complement and calculates area metrics before optional geometry simplification. The workflow exports park deserts as GeoJSON and renders a PNG.

The [example map](../outputs/park_accessibility_analysis.png) comes from a prior run. The reported coverage of 23.9% and park deserts of 76.1% describe that run's study rectangle and assumptions. This stage reviewed code and documentation without rerunning the calculations.

## Limitations of the node-buffer method

- Buffers extend in straight lines around reachable nodes. They can cross barriers such as rivers, fences or rail corridors and include locations that require a detour or exceed the 800 m route budget.
- Sparse graph nodes can leave gaps next to genuinely reachable edge segments. The represented area depends on node density, graph simplification and the chosen buffer radius.
- The graph traversal starts at park proxies. Directionality and pedestrian restrictions affect whether a route in the opposite direction, from a home to a park, is possible.
- Nodes near or inside a park can lack legal access. The centroid fallback can snap across a barrier. The current snap carries no maximum connection distance or verified path to an entrance.
- The coverage denominator comes from park bounds plus 1 km. It can include areas outside the municipality and nonresidential land. The percentages measure planar area and provide no estimate of the share of homes or people with access.
- The cutoff is distance in metres. The map's approximate time label depends on an assumed walking speed.

## Validation before integration

Review pedestrian edge permissions and compare edge lengths before and after the post-simplification recalculation. Check routes against a small fixture with one-way constraints, barriers and disconnected components. Compare node buffers against known reachable edge segments to document sensitivity. Benchmark multi-source traversal only after verifying equivalent seeds, costs and direction.

The DuckDB and GeoParquet park layer includes points, polygons and multipolygons. A read-only adapter should preserve geometry and CRS, document how the prototype's name and area filters apply, and compare its outputs with the GeoJSON workflow. Record graph version, OSM snapshot, access-point provenance and failed connections. The [target H3 architecture](PROJECT_BRIEF.md) defines the center-to-access-point model and residential rating planned for the MVP.
