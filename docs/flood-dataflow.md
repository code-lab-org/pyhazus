# Hazus 7.2 flood analysis: reconstructed dataflow

Sources examined (all from the 7.2 admin image; see `docs/extraction.md`):

- Python toolbox inside `HazusPro.proConfigX` → `Toolboxes/toolboxes/*.py` (plain text, ArcPy)
- Decompiled .NET: `Hazus.Logic.dll` (`Hazus.Logic.Flood.AnalysisLogic`), `Hazus.DataAccess.dll`
  (`StudyRegionRepository`)
- T-SQL stored procedures in the study-region template `StudyAreaTemplate/Hazus_model_Primary.mdf`
  (not encrypted — `--WITH ENCRYPTION` is commented out)

**Gate result:** the flood path never touches `LegacyConnector.exe` or any `*_x86.dll`. The legacy
COM layer is used only by hurricane and earthquake.

## 1. Scenario creation (`create_flood_scenario.py`, ArcPy)

| Step | Behaviour |
|---|---|
| Inputs | One or more depth grids, each tagged `Riverine` or `Coastal`, units `Feet` or `Meters`; AAL mode adds a return period per grid |
| Reprojection | Any grid whose CRS is not EPSG:4326 is projected to WGS84 geographic, using the projected lower-left corner as the registration point |
| Units | Meters → feet via `raster * 5000 / 1524` (exact 1/0.3048) |
| Merge | Single-event mode: multiple grids of the same hazard type are merged with a cell-wise **MAXIMUM** (`CellStatistics`, ignore NoData) |
| Validation | AAL: return periods must be unique per hazard type, and a hazard type with one or two return periods is only accepted if the other has at least three |

The result is a list of `{ReturnPeriod, HazardType (1=Riverine, 2=Coastal), FullPath, Units=Feet}` sent
to the .NET add-in over ZeroMQ.

## 2. Run Analysis (`Hazus.Logic.Flood.AnalysisLogic.RunAsync`)

For each distinct return period (ascending), for each depth grid of that period:

- **`ReturnPeriodId`:** the period itself when there are several. With one period it is `"mix0"` when the
  grids mix riverine and coastal, else the grid's period (`"-1"` for a single event). Facility flood
  protection uses `int(ReturnPeriodId)`, or 0 for `"mix0"`, so protection never applies to a single event.
- **Hazard type** passed to the SQL: 1 riverine, 2 coastal, 3 both (`FloodRasterExtensions.GetHazardType`).

### 2a. Area weighting (`analysis_area_weighted.py`, ArcPy)

1. Rasterize `hzCensusBlock` (field `CensusBlock`) in the depth grid's CRS (EPSG:4326) at cell size
   **0.00001°**, snapped to the depth grid → `blkgrid.tif`.
2. Resample the depth grid to 0.00001°, snapped to `blkgrid`, masked to `hzBoundary`.
3. Reclassify depth (ft) into half-foot bins, coded in hundredths of a foot:
   `[-1,0) → NoData`, `[0,0.25) → 0`, `[0.25,0.75) → 50`, … `[23.25,23.75) → 2350`, `≥ 23.75 → 2400`.
   Each bin is `round_half_up(depth * 2) * 50`, capped at 2400.
4. Zone raster:
   - Riverine: every cell with depth in `[0, 10000]` → `1`
   - Coastal (depth-limited V-zone rule): `[0,3) → 2`, `[3,6) → 3`, `≥ 6 → 4`
5. `Combine(blkgrid, reclassed, zoned)` → unique combinations with cell `Count`, exported to SQL table
   `COASTALSECONDJOIN`; the block raster's value table (`Value`, `Count`, `CensusBloc`) → `BLKGRID`.

### 2b. SQL join (`StudyRegionRepository.CreateAreaWeightedJoinAsync`)

```sql
select a.ObjectID, Cast(a.Count_ as int) as [Count], blkgrid as BlkGrid, b.CensusBloc as CensusBlock,
       zoned as CoastalZone, reclassed as ReClass, Cast(b.Count_ as int) as TotalCount
into flAnGBSSecondJoin
from COASTALSECONDJOIN a left join BLKGRID b ON a.blkgrid = b.Value
```

### 2c. `absp_GBSInsertAW` (T-SQL)

- Zone code → letter: `1 → 'R'`, `2 → 'A'`, `3 → 'CA'`, `4 → 'CV'`, else `'N'`.
- `PctAffected = Count / TotalBlockCells * 100` per (CensusBlock, CoastalZone, ReClass), in float64.
- ReClass ≥ 2400 is forced to 2400.
- Output: `flAnAreaWeighted` (via `flAnGBSCombineAWTemp`), pivoted to `ft00 … ft2400`.
- It also writes `flAnShelterResult` with `PctArea = Σ PctAffected / 100` per block (all zones together).
- Before inserting, it deletes the rows of the same study case, return period and controlling hazard. If two
  grids share a return period and hazard, only the last one reaches the building stock, debris and shelter.
  Scenario creation normally prevents this by merging single-event grids.

Port: `hazus.flood.exposure.area_weighted_rows` and `hazus.flood.shelter.shelter_result`.
`tests/test_sql_parity_aw.py` loads synthetic `COASTALSECONDJOIN` / `BLKGRID` tables, runs the C# join and
`absp_GBSInsertAW`, and matches both tables to 1e-12.

`PctAffected` is relative to the block's cells inside the study boundary. Those cells come from
`hzCensusBlock`, whose geometry is already **dasymetric**: the state databases keep the full TIGER block
shapes separately in `hzCensusBlock_TIGER`. In the DC state database, the 5,090 dasymetric blocks cover
62 % of the TIGER block area, and no block shape is identical to its TIGER shape. So `PctAffected` is the
flooded share of the block's developed area.

### 2d. Site-specific facilities (C#)

Essential facilities, utilities and UDFs are handled in C# (`ComputeEFDamage`, `ComputeUtilityDamage`,
`ComputeUDFDamage`):

- Depth at the facility point = max over the return period's grids
  (`GetMaxValueAndControllingHazardAsync`); also returns the controlling hazard.
- Skip if depth < 0. Cap depth at 24 ft.
- `floodedDepth = depth − FirstFloorHt` (EF, UDF) or `depth − EquipmentHt` (utilities).
- If `ReturnPeriod > 0` and `FloodProtection > ReturnPeriod` → zero damage.
- Damage function columns: `ft04m…ft01m, ft00…ft09, ft10…ft24`. Linear interpolation between
  `floor(floodedDepth)` and `ceil(floodedDepth)`; below −4 ft clamps to `ft04m`; utilities clamp ≥ 10
  to `ft10`.
- If the facility's damage-function ID is not found, it is resolved through the T-SQL procedures
  `flGetDamageXRefIdEssntFltyStruct` / `…Cont` / `flGetDamageXRefIdUtilFlty`
  (inputs: controlling hazard, depth, occupancy).
- EF: `BldgLoss = pct * Cost / 100`;
  `ContLoss = contPct * Cost * ContentRatio(occupancy) / 100 / 100`.
- EF → occupancy for the content ratio:
  - `EFHS/EFHM/EFHL/MDFLT → COM6`
  - `EFMC → COM7`
  - `EFEO/EFFS/EFPS/EDFLT/FDFLT/PDFLT → GOV2`
  - `EFS1/SDFLT → EDU1`
  - `EFS2 → EDU2`
- Restoration time: row of the EF restoration table with `MinDepth <= floodedDepth < MaxDepth`.
- Arithmetic: `FirstFloorHt` and `EquipmentHt` are read into C# `float` (float32). The interpolation is float32.
  Losses are computed in double and stored as float.
- `GetIntervalValue` returns 0 for a column the function does not have. Utility functions start at `ft00`, so
  below the equipment height they interpolate towards 0.
- The fallback procedures pick the xref row with `SOccupId = class` and `HazardR` (riverine), `HazardCV`
  (coastal deeper than 6 ft) or `HazardCA`, among rows that are `Selected` or `DefaultFn`. The depth they get
  is the flood depth, not the depth above the first floor. Dapper's `QuerySingleOrDefault` throws if more
  than one row matches.
- `AnalysisOptId` is never set on these results, so it is always 0.

Port: `hazus.flood.facilities` (one facility) and `hazus.flood.facility_analysis` (whole tables →
`flFR*`). `tests/test_sql_parity_facilities.py` checks the fallback lookup against the three procedures for
every occupancy.

**Facility flood attributes.** The `abst_INSfl*` insert triggers overwrite every new flood-table row
(`flCareFlty`, …) with the defaults for the facility's class from `fl<table>Dflt`, whatever values were
inserted. The essential-facility triggers do this correctly. The utility triggers
(`abst_INSflPotableWaterFlty` and the other four) never join the updated table, so the `UPDATE … FROM`
cross-joins it:

- adding one utility facility resets *every* row of that utility's flood table to the new facility's class
  defaults;
- a multi-row insert assigns defaults in arbitrary order. In a check against the SQL, two facilities of
  different classes came out swapped.

`facility_analysis.flood_defaults` gives each facility its own class defaults. `run_facilities` uses them
when a region has no flood table.

**Quirks to reproduce, or deliberately fix behind a flag:**

1. The `Functionality` field is inconsistent:
   - It starts as `true`, which is also the value kept for unflooded and flood-protected facilities.
   - It is then set to `floodedDepth > FunctionalDepth`, i.e. `true` when *non*-functional by the TM.
   - `hazus.flood.facilities` mirrors the raw value and also returns `flooded_depth`.
   - Still to check: how the result views and reports read this column.
2. The point depth is a running maximum that starts at **0.0** (`GetMaxValueAndControllingHazardAsync`),
   so `depth < 0` never happens. Facilities outside every grid are evaluated at depth 0, i.e. at
   `−FirstFloorHt` on the damage function. Curves with non-zero values below 0 ft (basements) therefore
   give losses to unflooded facilities.
3. In UDF content loss, the content damage function is fetched but **not used**: content loss uses the
   *structure* interpolated percentage.
4. Utility equipment height is used as stored. The TM's rounding rule (Eq. 9-1) is not applied.
5. If no EF restoration row matches `MinDepth <= floodedDepth < MaxDepth`, the C# dereferences null
   and the analysis throws. The tables start at −4 ft, so this happens to a dry facility (depth 0) whose
   first floor is more than 4 ft up. The port keeps the initial values instead (1 day, functional
   depth 0). Every other null dereference (missing flood row, damage function, content ratio, or utility
   functional depth) raises `LookupError` in the port.
6. `ComputeBridgeDamage` interpolation is missing the base term and has the wrong sign. It is not
   called in the 7.2 flood path.
7. UDF content loss without a content cost uses `ContentValPct / 100` in **integer** arithmetic
   (`short / int`), so a 50 % content ratio gives no content loss. UDF analysis is switched off for flood in
   7.2 anyway.

### 2e. Building stock, debris, shelter, AAL (T-SQL)

| Option | Procedure | Parameters |
|---|---|---|
| GBS | `absp_GBSDoIt` | `@AnalysisBits=15, @haztype, @RegDivide=0, @proccode=1` |
| Debris | `CalculateDebris` | `@StudyCaseId, @AnalysisOptID` |
| Shelter | `absp_SHRDoIT` | `@ShIngress=0, @AffectHt=0` |
| AAL | `absp_AALDoIt`, `absp_AALUDFDoIt` | only when > 2 return periods |

AAL coefficients are computed in C# before `absp_AALDoIt`:

```
sort return periods descending; prev = 0
for RP in that order: Coefficient(RP) = 1/RP − prev; prev = 1/RP
```

Example: {500, 100, 50, 25, 10} → {0.002, 0.008, 0.010, 0.020, 0.060}. These are simple exceedance-band
widths, not the TM Table 6-16 constants. They are stored as `NUMERIC(18,8)`.

`absp_AALDoIt` then works per block × occupancy × hazard × pre/post-FIRM:

- Return periods run from longest to shortest. The longest contributes `coef × loss`; each other contributes
  `coef × (loss + loss_of_next_longer) / 2`.
- The sum is exact decimal arithmetic, rounded half away from zero.
- The result is written as `ReturnPeriodId = 'Annual'` rows of the four economic-loss tables.
- In the general-building-type rollup, the "Wood" row uses the concrete share (`CPct`), so it repeats the
  "Concrete" row (`hazus.flood.aal`, `wood_uses_concrete_pct`).

Shelter (`absp_SHRDoIT`) and debris (`CalculateDebris`) quirks are listed in `hazus.flood.shelter` and
`hazus.flood.debris`:

- shelter needs truncate into `INT` columns, and the income term above $40k omits the population factor;
- debris joins mapping schemes by block type only, so all schemes of the state contribute.

## 3. Port status and verification

| Step | Python | Checked against |
|---|---|---|
| Scenario creation | `hazard.create_scenario`, `prepare_depth_grid`, `merge_max` | unit tests |
| Area weighting (rasters) | `exposure.area_weighted_depths` | unit tests on synthetic rasters |
| `absp_GBSInsertAW` | `exposure.area_weighted_rows`, `shelter.shelter_result` | SQL, 1e-12 |
| Damage functions (`flDmgFinal`) | `ddf.build_dmg_final` | SQL, exact |
| GBS (14 tables) | `gbs.run_gbs` | SQL, zero integer difference |
| Essential facilities, utilities | `facility_analysis.run_facilities` | C# logic port; SQL for the fallback lookups |
| Debris | `debris.calculate_debris` | SQL, to the cent |
| Shelter | `shelter.run_shelter` | SQL, exact |
| AAL | `aal.aal_gbs` | SQL, exact |
| Whole run | `scenario.run_scenario` | SQL end to end (`tests/test_sql_parity_scenario.py`) |

Not ported: user-defined facilities as a table driver (off in 7.2), vehicles, agriculture, bridges, and the
"what-if" / flood-frequency (R&C overlap) options that 7.2 never reaches.
