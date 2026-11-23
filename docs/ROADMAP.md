# Roadmap and acceptance criteria

## Completed baseline

- The [v0.1.0 portfolio MVP](../README.md) measures straight-line proximity to five categories. The local check suite and CI use synthetic fixtures.

- The Madrid park prototype is present in the repository with an [example map](../outputs/park_accessibility_analysis.png). Static review confirmed the GeoJSON entry point, OSMnx and NetworkX workflow, and GeoJSON and PNG output paths. See the [prototype review](NETWORK_ANALYSIS_REVIEW.md).
- Prior manual checks validated 8,699 park, 4,719 POI and 260 metro geometries in DuckDB and opened all three exported Filebase datasets as GeoParquet with GeoPandas. This publication pass relied on those earlier checks and static inspection.

## Future Madrid pedestrian-network assessment

1. **Define inputs and fixtures.** Document the Madrid boundary, OSM tag definitions for the six categories, data provenance and candidate thresholds. Add small local fixtures for mixed geometry, unknown building use and missing destination data. Verify geometry and CRS, deduplication, and GeoParquet metadata without production credentials.
2. **Connect data and validate routes.** Add a read-only DuckDB or GeoParquet adapter for the existing GeoDataFrame interface. Validate a small Madrid sample on an ordinary pedestrian graph. Compare known routes, pedestrian restrictions, directionality, distance costs, disconnected components and access-point snaps. Track fallback access points in QA.
3. **Build the grid and mask.** Generate H3 resolution 11 cells. Use the cell center as the representative route origin. Overlay building evidence and verify cases with residential, unknown-use, mixed, and no candidate buildings across cell boundaries.
4. **Rate and map the six categories.** Compute distances for parks, metro, pharmacies, schools, kindergartens and hospitals. Validate scoring, missing-data rules and five class breaks on documented examples. Verify gray hatching for building-use uncertainty and solid gray without a rating for cells with no identified housing candidates.
5. **Package the results.** Produce a geospatial result, map and PDF with a legend, boundary, criteria, source dates, methodology and limitations. Document a reproducible command and its input versions.

Acceptance for the network assessment requires reproducing the Madrid outputs from documented inputs and passing fixture-based checks for network distances, origin and destination snaps, residential mask, uncertainty and rating classes. The [project brief](PROJECT_BRIEF.md) defines the agreed architecture.

## Future stage — weekly ETL with GitHub Actions

1. Run the ETL weekly via `schedule` and allow manual runs via `workflow_dispatch`. Supply `FILEBASE_KEY` and `FILEBASE_SECRET` from GitHub Secrets.
2. Download a fresh Madrid OSM PBF and record its source and date. Prepare the expected GeoParquet layers and validate the full set, including schemas, geometry metadata, CRS and row counts.
3. Publish the files and their manifest under a versioned Filebase prefix. Verify the complete uploaded release before switching the active release pointer.
4. If a run fails before promotion, keep the previous successful release and its active pointer. Serialize promotion to prevent scheduled and manual runs from racing.
5. Plan a separate future workflow to recompute H3 accessibility from a validated data release. Record the input release ID with the H3 outputs.

## Future improvements

- Add stroller routing after validating the relevant pedestrian infrastructure tags and route constraints.
- Add a map interface and API that reproduce the published analytical results.
- Add containerized deployment.
- Expand to other cities after verifying that tagging, coverage and routing assumptions transfer.
