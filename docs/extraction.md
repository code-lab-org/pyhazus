# Extracting the Hazus 7.0 / 7.2 installers

All commands run from the repository root on Windows. The extraction tool is stdlib-only Python 3.12.
None of these steps install or register Hazus.

## 1. Unpack the InstallShield launchers

```
python -I tools/isextract.py "Hazus 7.0/setup.7.0.exe" --list
python -I tools/isextract.py "Hazus 7.0/setup.7.0.exe" extracted/raw/7.0
python -I tools/isextract.py "Hazus 7.2/Hazus.7.2.patch.exe" extracted/raw/7.2
```

| Installer | Embedded files | Decoded size |
|---|---|---|
| `setup.7.0.exe` | `Hazus Pro.msi`, `Setup.ini`, `0x0409.ini`, prerequisite `.prq` files (.NET 4.8, SQL Server 2022 Express, VC++ 2019 x86) | MSI 1,669,429,248 bytes |
| `Hazus.7.2.patch.exe` | `7.2.msp`, `Setup.ini`, `0x0409.ini` | MSP 1,094,934,528 bytes |

The SQL Server prerequisite installs a named instance `HAZUSPRO`, with `NT AUTHORITY\Authenticated Users`
as sysadmin.

## 2. Expand the MSI as an administrative image, then apply the 7.2 patch to a copy

```
msiexec /a "extracted\raw\7.0\Hazus Pro.msi" /qn TARGETDIR="<abs>\extracted\admin70" /l*v extracted\logs\admin70.log
robocopy extracted\admin70 extracted\admin72 /E
msiexec /a "extracted\admin72\Hazus Pro.msi" /p "extracted\raw\7.2\7.2.msp" /qn /l*v extracted\logs\admin72.log
```

The 7.0 image holds 3,369 files (1.75 GB). Layout:

- `program files/HazusPro/`
  - `HazusPro.proConfigX`: a zip containing the .NET 8 add-in and the Python toolbox
  - legacy 32-bit COM DLLs (`adti*`, `ddti*`, `pdti*`, `hu*`, `ffeq_x86`, `Utilities_x86`)
  - `LegacyConnector.exe`
- `CommonAppData/HazusPro/`
  - `syHazus_data.exe`: a 7z SFX holding `syHazus_Primary.mdf`, 3.4 GB
  - `flTmpDB_Primary.mdf`
  - `StudyAreaTemplate/Hazus_model_Primary.mdf`: the study-region template DB with the `absp_*` procedures
  - `HU/Windfield/`: hurricane event sets and 3,121 wind-field files
  - `Symbology/`

The 7.2 patch changes 32 files and adds 14, including:

- `syHazus_71_P0.sql`: 15,360 `huDamLossFun` rows, i.e. hurricane only
- `flTmpDB_71_P0.sql`: version bump only
- the ShakeMap layers
- updated legacy DLLs and `HazusPro.proConfigX`

## 3. Unpack the add-in and the system database

```
7z x "extracted\admin72\program files\HazusPro\HazusPro.proConfigX" -oextracted\proconfig72
7z x "extracted\admin72\CommonAppData\HazusPro\syHazus_data.exe" -oextracted\db72
```

`proconfig72/Toolboxes/toolboxes/` contains the plain-text ArcPy toolbox. `analysis_area_weighted.py`,
`create_flood_scenario.py` and `run_analysis.py` define the flood GIS steps.

## 4. Decompile the .NET assemblies (reference only; not committed)

```
dotnet tool install -g ilspycmd --version 8.2.0.7535
set DOTNET_ROLL_FORWARD=Major
ilspycmd -p -o decompiled\7.2\Hazus.Logic extracted\proconfig72\Install\Hazus.Logic.dll
```

## 5. Export the databases

Attach `Hazus_model_Primary.mdf`, `flTmpDB_Primary.mdf` and `syHazus_Primary.mdf` to SQL Server 2022
(Docker `mcr.microsoft.com/mssql/server:2022-latest`). Then export the tables to Parquet and the module
definitions to `.sql` with `tools/export_db.py`. The procedure text is not encrypted.
`tools/build_data.py` copies the lookup tables the library needs into `src/hazus/data/hazus72/`.

## 6. Real study regions from FEMA state downloads

The state inventory downloads (`<ST>.zip`) each contain one SQL Server database, `<ST>.mdf`, plus a
hurricane wind file. Hazus builds a study region from it entirely in T-SQL:

1. `syHazus..AggregateByCounty` (or `…ByState`, `…ByTract`, `…ByCensusBlock`) reads `syStateSel` /
   `syCountySel`.
2. It calls `AggregateAll` in a fresh copy of the region template. That procedure creates synonyms for the
   state tables, copies each one through `_<table>_Aggregate`, builds the boundary and runs the
   post-processing (`absp_StudyRgnCreationPostProcess*`), which computes the depreciated exposure
   `flDepExposureOccupB` / `flDepExposureContentOccupB`.

`tools/make_region.py` runs the same steps in the Docker SQL Server, with the flags Hazus uses (flood and
earthquake always; hurricane and tsunami where the state has them):

```
python tools/make_region.py DC data/DC.zip --name DC_Region
hazus inventory export DC_Region work/regions/DC
```

It streams the database straight from the zip into the container, so the host needs no extra disk space.
For DC (5,090 blocks) the aggregation takes about 2 minutes.

`tests/test_sql_parity_state.py` uses such a region when `HAZUS_STATE_REGION_DB` names it. It exports the
region, runs the Python pipeline on synthetic depth grids, and compares the results with the Hazus SQL
run inside the region database.
