# Hello GIS product and architecture brief

Hello GIS helps families explore residential areas in Madrid, Spain, by walking access to everyday destinations. Results describe accessibility to mapped destinations under documented routing assumptions. OSM coverage and tagging affect the results.

## Implemented portfolio MVP

The [v0.1.0 Madrid portfolio MVP](../README.md) scores straight-line proximity to five amenity categories using an existing residential H3 grid and a local OSM extract. It produces a map, a two-page report, and local analytical layers.

## Park network prototype

The [park accessibility module](../src/hello_gis/network_analysis.py) reads park GeoJSON, uses OSMnx and NetworkX to find nodes reachable from park access proxies within 800 m of network distance, and derives coverage polygons from node buffers. It writes park-desert GeoJSON and renders a [PNG map](../outputs/park_accessibility_analysis.png). The [methodology review](NETWORK_ANALYSIS_REVIEW.md) describes the assumptions and limits of this run.

The separate [Madrid ETL](../src/hello_gis/etl_osm_to_s3.py) extracts `parks`, `pois` and `metro` from OSM, loads geometry into DuckDB Spatial and exports GeoParquet to Filebase. Earlier manual checks confirmed valid, nonempty geometries and GeoParquet readable by GeoPandas in `OGC:CRS84`, including mixed geometry types. The routing module currently consumes GeoJSON. Integration through a DuckDB or GeoParquet adapter remains future work for network analysis.

## Target pedestrian-network architecture

1. **Study area and spatial unit.** Use Madrid as the first study area and H3 resolution 11 as the reporting grid. Document the exact Madrid boundary and allow the walking graph to extend beyond it where routes need to cross it. Use each H3 center as a representative route origin. Record how the center connects to the network, including the connection distance and failures to connect.
2. **Destinations.** Compute accessibility separately for six categories: `parks`, `metro`, `pharmacies`, `schools`, `kindergartens` and `hospitals`. Link each destination to the ordinary pedestrian network through mapped access points where available. When access points are missing, use a documented fallback such as an accessible boundary point or a snapped feature point when appropriate. Store the access-point source, flag approximations, and record failed or excessive snaps.
3. **Walking model.** Use an ordinary pedestrian network for the future network assessment, with edge costs in metres and pedestrian access rules. Measure routes from each representative origin toward the destination access points. Keep thresholds configurable and record them with the results. The original brief specified 2,000 m, while the park prototype uses 800 m. The target thresholds by category remain to be validated. Use a suitable metric CRS for planar distances and other metre-based operations.
4. **Residential H3 mask.** Derive the mask from mapped residential buildings and include buildings with unknown use as candidates, carrying an uncertainty flag into the affected cells. Document the building-to-cell assignment rule. In cells with mixed evidence, preserve confirmed residential evidence and the uncertainty flag separately.
5. **Integrated rating and cartography.** Combine the six category metrics into five ordered rating classes for cells eligible under the residential mask. Apply gray hatching where building use is uncertain, over the rating color where a rating can be computed. Show cells with no identified residential or unknown-use candidate buildings in solid gray without a rating. The legend should explain that solid gray reflects the available mapped evidence. Keep missing destination or routing data distinct from measured low accessibility. The scoring formula, class breaks and missing-data rules require validation before publication of ratings.

Keep DuckDB Spatial and GeoParquet as the analytical data path. Record OSM extract date, graph build, CRS, thresholds, access-point provenance and scoring version with the outputs. Acceptance for the network assessment includes a reproducible Madrid map and a PDF describing the method, inputs, results and limitations.

## Future improvements

Add a stroller-routing profile when the available data and validation support stairs, curb ramps, surfaces and other relevant constraints. Later phases can add an interactive map and API, scheduled OSM refresh, deployment and more cities. See the [roadmap](ROADMAP.md).
