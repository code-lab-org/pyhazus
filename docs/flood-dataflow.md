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
| Validation | Single event: at most one riverine + one coastal grid. AAL: ≥ 3 unique return periods for at least one hazard type |

The result is a list of `{ReturnPeriod, HazardType (1=Riverine, 2=Coastal), FullPath, Units=Feet}` sent
to the .NET add-in over ZeroMQ.

## 2. Run Analysis (`Hazus.Logic.Flood.AnalysisLogic.RunAsync`)

For each distinct return period (ascending), for each depth grid of that period:

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
- `PctAffected = Count / TotalBlockCells * 100` per (CensusBlock, CoastalZone, ReClass).
- ReClass ≥ 2400 is forced to 2400.
- Output: `flAnAreaWeighted` (via `flAnGBSCombineAWTemp`).

Note: `PctAffected` is relative to **all block cells inside the study boundary**, not to the dasymetric
area. *(TODO: confirm whether `hzCensusBlock` geometry is already dasymetric in the state DB.)*

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
   and the analysis throws.
6. `ComputeBridgeDamage` interpolation is missing the base term and has the wrong sign. It is not
   called in the 7.2 flood path.

### 2e. Building stock, debris, shelter, AAL (T-SQL)

| Option | Procedure | Parameters |
|---|---|---|
| GBS | `absp_GBSDoIt` | `@AnalysisBits=15, @haztype, @RegDivide=0, @proccode=1` |
| Debris | `CalculateDebris` | `@StudyCaseId, @AnalysisOptID` |
| Shelter | `absp_SHRDoIT` | `@ShIngress=0, @AffectHt=0` |
| AAL | `absp_AALDoIt`, `absp_AALUDFDoIt` | only when ≥ 3 return periods |

AAL coefficients are computed in C# before `absp_AALDoIt`:

```
sort return periods descending; prev = 0
for RP in that order: Coefficient(RP) = 1/RP − prev; prev = 1/RP
```

Example: {500, 100, 50, 25, 10} → {0.002, 0.008, 0.010, 0.020, 0.060}. These are simple exceedance-band
widths, not the TM Table 6-16 constants. How `absp_AALDoIt` combines them is still to be read.

## 3. Still to extract (requires attaching the MDF)

- Full text of `absp_GBSInsertAW`, `absp_GBSDoIt` (and everything it calls), `absp_SHRDoIT`,
  `CalculateDebris`, `absp_AALDoIt`, `absp_AALUDFDoIt`, `flGetDamageXRefId*`.
- Lookup tables: damage-function libraries, default assignments, restoration, economic parameters.
