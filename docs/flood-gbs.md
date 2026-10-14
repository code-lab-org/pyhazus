# Hazus 7.2 flood: general building stock (GBS / ABI) computation

Reconstructed from the T-SQL in the study-region template DB (`Hazus_model_Primary.mdf`), exported to
`work/sql/HazusModel/`. The procedure names below are the Hazus ones.

## 0. Runtime constants in 7.2

The 7.2 C# never writes `flStudyCaseParms`, so every parameter-driven branch takes its default:

| Parameter | Value in 7.2 | Effect |
|---|---|---|
| `Hazard_Type` | unset | `absp_DmgDoIt` uses the hazard type passed in (1 R, 2 C, 3 R&C, from the depth grids); study-region post-processing uses 3 |
| `FFESET` | unset → 0 | first-floor heights from `flFFEValues` with `FFESet = 0` |
| `RIV_ANALYSIS_TYPE` (`@IsSuite`) | NULL | the R&C "suite" and overlap branches (`absp_CBAreaAffected`, `flCBControl`) never run |
| `RIV_RP`, `COAST_RP` | NULL | `@MIXCASE = 'Y'`; irrelevant because of the above |
| `AUTO_REPOSITORY_CHECK` | NULL | no repository export |

`flAnGBSCBAreaPct` is filled only by the legacy `absp_GBSSyncAccessMSSQL` (Access import), so the overlap
correction is dead code in 7.2.

Call order from `absp_GBSDoIt(@AnalysisBits=15, @haztype, @RegDivide=0, @proccode=1)`:

1. `absp_StudyRgnCreationPostProcessGbs`: once per study region; runs `absp_DmgDoIt` (3) and `absp_DistDoIt`
2. `absp_DmgDoIt @haztype`: rebuilds `flDmgFinal`
3. `absp_GBSSetXResults`: copies the exposure rows of the flooded blocks into `flGBSXResults*`
4. `absp_GBSBasePrep`: builds `flGBSFinal`
5. `flFRGBSBaseTabT = absf_GBSBaseTabT(@StudyCaseID)`
6. bit 1 `absp_GBSBldgContDmg`, bit 2 `absp_GBSEconLoss`, bit 4 `absp_GBSDmgCounts`, bit 8 `absp_GBSDepEconLoss`

## 1. Damage functions → `flDmgFinal` (`absp_DmgFnNorm`, `absp_DmgBaseSplit`, `absp_DmgFinal`)

**Normalise.** Each structure, content and inventory damage function becomes `(FnID, Depth, Val)`:

- `Depth` runs from −400 to 2400 in steps of 50, in hundredths of a foot.
- At whole feet, `Val` is the column value (`ft04m` … `ft24`).
- At half feet, `Val` is the mean of the two neighbouring columns.

**Base.** For each hazard type `h` (1 R, 2 A, 3 CA, 4 CV), build the Cartesian product of three things:

- `flDepth`: depths −4 … 24 ft in 0.5 ft steps; `ID` = depth × 100
- `flFoundation` ⋈ `flFFEKey(FirmID, BlockTypeID, HazardTypeID = h, FoundationID → FFEID)` ⋈ `flFFEValues(FFESet = 0)`
- `flSOccupStoriesXRef` ⋈ `flSOccupIDBase`

The derived columns are:

- `ZoneID` = 2 if `h = 4`, else 1
- `SOCCUPID` = `SOccupID` (e.g. `R1`) + `Story` + `BaseText` (`B` = basement, foundation 4; else `N`)
- `DISID` = `int((Depth − First_Fl_Ht) × 100)`, the depth in the structure

**Final.** The xref tables (`flBldg{Struct,Cont,Inv}DmgXRef`) map `SOCCUPID` and hazard flags (`HazardR`,
`HazardCA`, …) to a function ID. The rows used are those with `Selected = 1`, falling back to `DefaultFn = 1`
for any `SOccupID` without a selected row. Each block of rows is joined to the normalised functions at
`Depth = DISID`.

| Function | Base | Rows | WithInv flags (str / cont / inv) | NoInv flags (str / cont) |
|---|---|---|---|---|
| `absf_DmgPctRiv…` | R | `DISID >= -400` | `HazardR` / `HazardR` / `HazardR` | `HazardR` / `HazardR` |
| `absf_DmgPctCoastA…` | A | `DISID >= -4` | `HazardR` / `HazardR` / `HazardR` | `HazardR` / `HazardR` |
| `absf_DmgPctCoastCA…` | CA | `DISID >= -4` | `HazardCV` / `HazardCA` / `HazardCA` | `HazardCV` / `HazardCA` |
| `absf_DmgPctCoastV…` | V | `DISID >= -4` | `HazardCA` / `HazardCA` / `HazardCA` | `HazardCV` / `HazardCV` |
| `absf_DmgPct…Zero` | as above | `DISID < 0` | all 0 | — |

The NoInv variants cover occupancies with `flGBSOccupInv.Inv = 0` and set inventory damage to 0.

The xref flags are written this way in the SQL, and the exact `flDmgFinal` parity test confirms them. They
look like copy-paste slips:

- coastal A zones use the *riverine* functions;
- CA structure uses the V-zone functions;
- V with inventory uses the CA functions, while V without inventory uses the V functions.

`hazus.flood.ddf` reproduces them.

`absp_DmgFinal(@HazType)` inserts with `ZoneID` 1 for R, A and CA, and 2 for V:

- `@HazType = 1`: Riv WithInv + Riv NoInv
- `@HazType = 2`: all coastal
- `@HazType = 3`: Riv (no Zero) + all coastal

## 2. Distributions and mapping schemes

- `flGBSDistNonRes(SchemeID, BlockTypeID, SOccupID, StoryID, FIRMID, FoundationID, HazardZoneID, DecadeID, Pct)`
  covers occupancies 9–33 (RES4 … EDU2).
- `flGBSDistRes` has the same columns without `DecadeID` and covers occupancies 1–8 (RES1 … RES3F).
- The third letter of `SchemeID` encodes the block type: `R` → 1, `C` → 2, `L` → 3. Each block's
  `flSchemeMapping.SchemeId` therefore fixes its block type.
- `Pct` sums to 0.92–1.0 over (story, foundation). Hazus does **not** renormalise it.
- `flDecadeDescription` maps `hzDemographicsB.MedianYearBuilt` to `DecadeID`. The IDs repeat
  (1950s → 2, 1960s → 3, 1970s → 2, 1980+ → 3); Hazus uses the data as is.
- `absp_GBSUpdateDistPercent` rewrites distributions for user-defined schemes, i.e. `SchemeID[3:8] != '00001'`.
  Not ported yet: the default schemes do not need it.

## 3. `absp_GBSBasePrep` → `flGBSFinal`

**Area-weighted rows.** Unpivot `flAnAreaWeighted` into rows of
`(RP, ControllingHazard, CoastalZone, CensusBlock, Depth ∈ {0, 50, …, 2400}, PctAffected)`. Map
`ControllingHazard` (`R`/`C`) to `BlockTypeID` through `flBlockType`, and `CoastalZone` (`R`/`A`/`CA`/`CV`) to
`HazTypeID` 1–4 through `flHazType`. Keep rows with `PctAffected > 0`.

**Damage × distribution, summed over foundation and story.**

```
NonRes: key (Scheme, Zone, Firm, BlockType, HazardType, OCC, DepthID, Decade, DmgStrBin)
        PctDmgStrC = Σ Pct × PctDmgStr / 100   (also Cont, Inv);   DistPct = Σ Pct
Res:    the same without Decade
DmgStrBin from the *unsummed* PctDmgStr: 0 → 1, (0,11) → 2, (10,21) → 3, …, (90,101) → 11
```

**Pre/post-FIRM split per block** (`#GBSPrePostBldg` / `#GBSPrePostPct`):

- The `hzDemographicsB` `Built*` counts are interpolated around `flSchemeMapping.EntryDate`:
  `PreBldg = round(0.01 + (EntryDate − decadeStart) / 10 × BuiltDecade) + earlier decades`.
- `PrePct = round(100 × Pre / Total)` and `PostPct = 100.1 − PrePct`. *The 100.1 is in the source.*
- If `UDPrePct` is set: `PrePct = UDPrePct`, `PostPct = 100 − UDPrePct`.
- `TotalBldg = 0` gives `PrePct = 100`, `PostPct = 0`.

**Merge.**

- `TypeOfHaz` = 1 for R, 2 for A/CA/CV.
- Join on (Scheme, Depth, HazardType), plus Decade for NonRes. Group by
  (block, Scheme, Zone, Firm, BlockType, TypeOfHaz, OCC, Depth, Decade, DmgStrBin, PrePct, PostPct).
- Sum `PctAffected`, `PctDmg*C` and `DistPct`. Because the merged group can contain several coastal hazard
  types for the same block and depth, this sum-then-multiply produces cross terms. Reproduce it exactly.

**Amounts.** `E(occ)` is the block's exposure column for the occupancy. Pre amounts use `PrePct`, post
amounts use `PostPct`; an amount is 0 when that percentage is 0.

| Column | Formula |
|---|---|
| `Sq{Pre,Post}ADmgStrC` | `SqFt(occ) × (Pre/PostPct/100) × (PctAffected/100) × DistPct` |
| `BLDG{Pre,Post}ADmgStrC` | `Exposure(occ) × … × PctDmgStrC` |
| `CONTD{Pre,Post}ADmgContC` | `ContentExposure(occ) × … × PctDmgContC` |
| `CT{Pre,Post}ADmgStrC` | `BldgCount(occ) × … × DistPct` |
| `DEPB…`, `DEPC…` | depreciated exposure equivalents |
| `INV{Pre,Post}ADmgInvC` | `SqFt × … × PctDmgInvC × AnnualSalesPerSqFt × BusinessInvPctofSales / 100` |

The inventory amount applies only to COM1, COM2, IND1–6 and AGR1; it is 0 for residential occupancies.

Every row is computed for both FIRM IDs. Downstream, `FirmID = 1` rows use the Pre columns and `FirmID = 2`
rows the Post columns.

## 3a. Numeric semantics needed for exact parity

All of these were verified against SQL Server 2022.

- Arithmetic on `REAL` operands is evaluated **and rounded** in float32 at every step, including
  `real × int`, `real × 0.01` and `100.0 × real`. Mixing in a `FLOAT` operand widens the result to float64.
- `ROUND(real, 0)` returns `FLOAT`. Hence `PrePct`/`PostPct` in `#GBSPrePostPct` are float64 (63.1 is the
  double 63.1), and the exposure amounts in `absp_GBSBasePrep` are float64 throughout.
- `flGBSDist*.Pct` is `REAL`, so `Pct × (PctDmg / 100)` is float32. The `SUM`s are float64.
- `flGBSFinal` stores every amount as `REAL`. The downstream `SUM`s are float64 over float32 values.
- Floats inserted into `INT` result columns (`flFRGBSEcLossBySOccup`, `…DmgCountSOccupB`,
  `…DepEcLossBySOccup`) are **truncated** toward zero.
- Products follow T-SQL's strict left-to-right evaluation order. Reassociating them changes float32
  roundings, which shows up after truncation.
- `ROUND(float, d)`, and storing a float in a `NUMERIC(p, d)` column, round the float's **exact binary
  value** half away from zero. `ROUND(2.675, 2)` is 2.67 and `ROUND(0.49999999999999994, 0)` is 0, because
  those doubles lie just below the half. `floor(x × 10^d + 0.5)` gets both wrong (`gbs.sql_round`).
- `int * 1.0 / int` is a **numeric** division, not a float one. Its result (scale 12 for `int` operands) is
  **truncated**: `2 * 1.0 / 3` is 0.666666666666. Shelter's income shares are computed this way, and
  truncating them can turn an exact integer into the integer below.

`hazus.flood.gbs` follows these rules. `tests/test_sql_parity_gbs.py` checks all 14 result tables with zero
integer tolerance against the original procedures on synthetic regions (riverine, coastal, both).
`tests/test_sql_parity_state.py` does the same on a region that Hazus built from a FEMA state database.
The last two rules showed up only with real data.

## 4. Downstream result tables

- `absp_GBSBldgContDmg`: square feet per `DmgStrBin` → `flFRGBSDmgSOccupB`
  - `ControlHazard` = `R` if `HazardTypeID = 1`, else `C`
  - `Pre_PostFirm` = 0 for pre, 1 for post
  - general-occupancy rollup (`absp_GBSFRDmg`, `LEFT(SOccup, 3)`) and general building type rollup
- `absp_GBSEconLoss` → `flFRGBSEcLossBySOccup`:
  - building, content and inventory losses come from the amounts above
  - business interruption terms use `flFRGBSBaseTabT.SqFt × DistPct × 0.01 × PctAffected × 0.01 × Pre/PostPct`
    with the TM Eq. 6-2 … 6-7 rates
  - restoration time comes from `absf_GBSRsFnGBST` by **flood depth bin**:
    Neg < 0 ≤ Upto1 < 100 ≤ Upto4 < 400 ≤ Upto8 < 800 ≤ Upto12 < 1200 ≤ Upto24
  - relocation and rental losses apply only when `Sq…/DistPct > 0.1`
  - `EmploymentLoss` omits the Pre/PostPct factor
  - building and content losses are finally capped at `Exposure` / `ExpCont` from `absf_GBSBaseTabT`; those
    are `int` columns, so the values are truncated
- `absp_GBSDmgCounts` → `flFRGBSDmgCountSOccupB`: building counts per damage bin, built like the
  square-foot table from the `CT…DmgStrC` amounts of `flGBSFinal`, then truncated into `INT` columns before the
  general-occupancy and building-type rollups
- `absp_GBSDepEconLoss` → `flFRGBSDepEcLossBySOccup`: building and content losses on the depreciated
  exposure (`flDepExposureOccupB`, `flDepExposureContentOccupB`), summed per block × occupancy × hazard ×
  pre/post-FIRM and truncated into `INT` columns
