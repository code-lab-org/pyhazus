# Hazus 7.2 study-region creation, reconstructed

Hazus builds a study region entirely in T-SQL. `syHazus..AggregateByCounty` (or `…ByTract`,
`…ByCensusBlock`) attaches the state database, copies the region template, runs `AggregateAll` in the copy,
builds the boundary, and runs `AggregatePostProcess`. `hazus.inventory.region.create_region` reproduces the
part the flood model needs. It reads a state inventory exported once from SQL Server
(`hazus inventory export-state`) and needs no SQL Server itself.

```
hazus inventory export-state DC states/DC          # once per state; needs the database attached in SQL Server
hazus region create states/DC regions/DC --counties 11001
hazus flood run regions/DC results/ --riverine depth.tif
```

## Steps

| Hazus | What it does | Port |
|---|---|---|
| `AggregateAll` | Counties → tracts in them → blocks in them, unless tracts or blocks are listed | `select` |
| `_hzCounty/_hzTract/_hzCensusBlock_Aggregate`, `_hz*B_Aggregate` | Copy rows of the selection | `create_region` |
| `_hzGenBldgSchemes/_hzGenBldgScheme_Aggregate` | Building schemes named by the selected tracts | ″ |
| `_flSchemeInfo/Riverine/Coastal_Aggregate` | The whole state's flood mapping schemes | ″ |
| `_flSchemeMapping_Aggregate` | Scheme of each selected block | ″ |
| `_hz<Facility>_Aggregate` | Facilities in the selected tracts, then their flood rows | ″ |
| `absp_StudyRgnCreationPostProcessScheme` | `EntryDate` NULL or 0 → 2009 | ″ |
| `absp_CLBlocks` | Coastal (C) and Great Lakes (L) blocks: third letter of `SchemeId` ← block type | ″ |
| `absp_BldgDepExpOccupB`, `absp_ContDepExpOccupB` | Depreciated exposure | `depreciated_exposure` |
| `AggregateStudyRegionBoundary` | `UnionAggregate(Shape).Reduce(0.00005).STBuffer(0.00001)` of counties, tracts or TIGER blocks | `study_region_boundary` |

Not ported, because the flood model does not read the results: the earthquake, hurricane and tsunami tables,
vehicles, agriculture, transportation, pipelines, the indirect-economic-loss parameters
(`absp_IELMParamsEmpInc`, which fills `hzCounty.Income*/Emp*/Output*`), and the general-building-type
rollups of `absp_PopTabWrapper`. Hazus also copies `flSchemeGLakes`, but only the scheme-editing procedures
read it. Great Lakes (L) blocks take their building distributions from the `<ST>L` schemes of
`flGBSDist*`. `tests/test_sql_parity_gbs.py` and `tests/test_sql_parity_debris.py` check them on New York
regions.

## Behaviours that matter for exact results

- **Copies change types.** The state databases use other column types than the region template, for example
  `float` exposure, `numeric(38,8)` coordinates and costs, and `nvarchar` ids. Every INSERT converts to the
  template's types:
  - float or numeric to `int` truncates. A state exposure of 3989.99543 becomes 3989.
  - To `real`, values round to float32.
  - To `decimal(11,6)` or `money`, values round to 6 or 4 decimals.

  `to_region_types` applies the template's schema (`hazus/data/hazus72/_region_schema.parquet`). It also
  evaluates the template's computed columns, such as `TotalExp`, `RESI` and `TotalSqft`. Those on `real`
  columns are added up in float32.
- **Depreciation depends on the creation date.** The building age is `DATEPART(year, GETDATE()) -
  MedianYearBuilt`, so the same region built in 2025 and in 2026 has different `flDepExposure*`.
  `create_region(year=…)` defaults to the current year. A block is skipped if its age falls outside
  `flDepFunction` (0–100) or its income ratio is negative or missing.
  - Building values: `ROUND((100 − dep%)/100 × value, 0)`. RES1 mixes the Good / Average / Poor curves by the
    `hzRES1ConstructionWt` weights of the block's income-ratio band; the band limits are compared as REAL.
  - Content values: `building × ContentValPct / 100` in integer arithmetic, so they are truncated.
- **Facility flood attributes are class defaults.** The `abst_INSfl*` insert triggers overwrite each copied
  flood row with the `fl<Table>Dflt` row of the facility's class. The state's own values survive only for
  classes without a default row. The utility triggers never join the updated table, so Hazus assigns
  defaults in arbitrary order. In the Delaware region, 7 of 19 electric-power facilities got another class's
  defaults. The port gives every facility its own class's defaults.

## Verification

`tests/test_sql_parity_region.py` exports a state, creates the region in Python, and compares every flood
table with the region Hazus built for the same counties (`tools/make_region.py`). DC (5,090 blocks) and
Delaware (17,490 blocks) match exactly. The exceptions:

- the 7 scrambled Delaware utilities;
- the indirect-economic-loss county columns;
- the boundary, which agrees with Hazus's to within 0.01 % of its area.

`tests/test_region.py` covers the same rules on a synthetic state without SQL Server.
