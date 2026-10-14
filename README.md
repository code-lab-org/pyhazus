# pyhazus

A standalone, cross-platform Python implementation of the FEMA Hazus loss estimation methodology,
reconstructed from the Hazus 7.0 / 7.2 installers and the FEMA technical manuals. It has no ArcGIS Pro,
SQL Server or Windows COM dependency at runtime.

Status: early development. **Flood** (Hazus 7.2) is ported:

- study-region creation from FEMA state inventories;
- depth grids, scenario creation and area weighting;
- general building stock: damage, economic loss, damage counts and depreciated loss (14 result tables);
- essential facilities and utilities;
- debris, shelter, and average annualized loss.

Each step is checked against the original Hazus T-SQL, run in SQL Server as a reference oracle (see
[Verification](#verification)).

## Install

```bash
pip install -e ".[geo,dev]"
```

`geo` adds rasterio, geopandas and shapely (needed for depth grids). `tools` adds pymssql, needed only to
export Hazus databases from SQL Server.

## Use

A study region is a directory with one Parquet file per Hazus table (`hzCensusBlock.parquet`,
`flSchemeMapping.parquet`, `hzCareFlty.parquet`, ...). Census blocks, the boundary and facilities are
GeoParquet in EPSG:4326.

Create one from a FEMA state inventory (the Hazus state download, `<ST>.zip`):

1. Export the state once from SQL Server. This is the only step that needs SQL Server; see
   [docs/extraction.md](docs/extraction.md).

   ```bash
   hazus inventory export-state DC states/DC
   ```

2. Create regions without SQL Server, as Hazus's aggregation does (see [docs/region.md](docs/region.md)):

   ```bash
   hazus region create states/DC region/ --counties 11001
   ```

An existing Hazus study-region database can also be exported directly with
`hazus inventory export MyRegion region/`.

Run a single-event scenario:

```bash
hazus flood run region/ results/ --riverine depth.tif --units meters
```

Run a scenario with return periods (three or more give AAL):

```bash
hazus flood run region/ results/ --riverine d10.tif@10 --riverine d100.tif@100 --riverine d500.tif@500
```

Results are written as one file per Hazus result table (`flFRGBSEcLossByTotal`, `flFRCareFlty`,
`flFRDebris`, ...), as Parquet or, with `--format csv`, CSV.

From Python:

```python
from hazus.flood import DepthGrid, create_scenario, run_scenario
from hazus.io import read_region

region = read_region("region/")
rasters = create_scenario([DepthGrid("depth.tif", "riverine", units="meters")], "work/")
results = run_scenario(region, rasters)
results["flFRGBSEcLossByTotal"]
```

## Fidelity

The port reproduces Hazus 7.2 as it behaves, including its numeric semantics and its bugs: float32
arithmetic, truncation into integer columns, T-SQL evaluation order, and the quirks listed in
[docs/flood-dataflow.md](docs/flood-dataflow.md) and [docs/flood-gbs.md](docs/flood-gbs.md). Where Hazus
would stop with an unhandled error, the port raises a clear exception instead, or documents its choice.

## Verification

`pytest` runs unit tests everywhere. The SQL differential tests (`tests/test_sql_parity*.py`) run when a
SQL Server with the Hazus databases attached is reachable (`HAZUS_SA_PASSWORD` set; see
[docs/extraction.md](docs/extraction.md)). They load synthetic study regions, run the original stored
procedures, and compare every result table with the Python output. Integer results must match exactly.
`tests/test_sql_parity_state.py` does the same on a study region that Hazus built from a FEMA state
database.

Area weighting (depth grid → flooded share of each block) runs in ArcGIS in Hazus, so there is no SQL to
compare with. `tests/test_hll_crosscheck.py` compares it instead with Hazus 5.1 runs published in the
[Hazus Loss Library](https://hazards.fema.gov/hazus-loss-library/). On the Little Blue River package
(7 scenarios), it floods the same blocks. Flooded shares agree within 0.25 percentage points on average,
and mean depths within 0.07 ft. Hazus 5.1 area-weights on the depth grid's own projected cells rather than
7.2's 0.00001° cells, so these comparisons use tolerances.

## Layout

- `src/hazus/`: the library (`import hazus`), with the Hazus 7.2 lookup tables in `src/hazus/data/`
- `tools/`: one-time tooling to unpack the FEMA installers and export their databases
- `docs/`: reconstructed methodology notes
- `tests/`: unit tests and SQL differential tests

The FEMA installers, extracted payloads and decompiled sources are not part of this repository
(see `.gitignore`).
