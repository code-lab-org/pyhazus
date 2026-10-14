"""Study-region creation: ``hazus.inventory.region`` vs. a region Hazus built with its own aggregation SQL.

Set ``HAZUS_STATE_REGION_DB`` to such a region (``tools/make_region.py``); its state database must be attached
under the state abbreviation. The state is exported with ``export_state``, a region is built in Python for
the same counties, and every flood table is compared with Hazus's.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import sqlref  # noqa: E402

REGION_DB = os.environ.get("HAZUS_STATE_REGION_DB")
pytestmark = [
    pytest.mark.skipif(not REGION_DB, reason="HAZUS_STATE_REGION_DB not set"),
    pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available"),
]
pytest.importorskip("geopandas")

KEYS = {"hzCounty": ["CountyFips"], "hzTract": ["Tract"], "hzCensusBlock": ["CensusBlock"],
        "hzGenBldgScheme": ["BldgSchemesId", "Occupancy"], "flSchemeMapping": ["CensusBlock"]}
# hzCounty Income*/Emp*/Output*: absp_IELMParamsEmpInc (indirect economic loss), not needed for flood
NOT_PORTED = {"hzCounty": ("Income", "Emp", "Output")}
UTILITY_FLOOD = {"flPotableWaterFlty", "flWasteWaterFlty", "flOilFlty", "flNaturalGasFlty", "flElectricPowerFlty"}


@pytest.fixture(scope="module")
def regions(tmp_path_factory):
    from hazus.inventory.region import create_region
    from hazus.inventory.sqlserver import FLOOD_TABLES, export_region, export_state
    from hazus.io import read_region

    out = tmp_path_factory.mktemp("regions")
    with sqlref.connect(REGION_DB) as conn:
        export_region(conn, out / "hazus", [*FLOOD_TABLES, "hzCounty"])
        with conn.cursor() as cur:  # Hazus depreciates by the year the region was created
            cur.execute("SELECT YEAR(create_date) FROM sys.databases WHERE name = %s", (REGION_DB,))
            year = cur.fetchone()[0]
    hazus = read_region(out / "hazus")
    state = hazus["hzCounty"]["State"].str.strip().iloc[0]
    with sqlref.connect(state) as conn:
        export_state(conn, out / "state")
    ours = create_region(read_region(out / "state"), counties=hazus["hzCounty"]["CountyFips"].str.strip(), year=year)
    return hazus, ours


def _normal(df: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame(df.drop(columns="geometry", errors="ignore")).copy()
    for c in df.columns:
        if pd.api.types.is_bool_dtype(df[c]):
            df[c] = df[c].astype("float64")
        elif pd.api.types.is_numeric_dtype(df[c]):
            df[c] = pd.to_numeric(df[c]).astype("float64")
        else:
            df[c] = df[c].astype("string").str.rstrip()
    return df.sort_values(list(df.columns), na_position="last").reset_index(drop=True)


def _rows_differ(ours: pd.DataFrame, hazus: pd.DataFrame) -> list[str]:
    cols = [c for c in hazus.columns if c in ours.columns and c != "geometry"]
    a, b = _normal(ours[cols]), _normal(hazus[cols])
    if len(a) != len(b):
        return [f"{len(a)} rows vs Hazus {len(b)}"]
    problems = []
    for c in cols:
        x, y = a[c], b[c]
        if pd.api.types.is_float_dtype(x):
            bad = ~np.isclose(x.to_numpy(float), y.to_numpy(float), rtol=0, atol=0, equal_nan=True)
        else:
            bad = (x.fillna("<NA>") != y.fillna("<NA>")).to_numpy()
        if bad.any():
            problems.append(f"{c}: {bad.sum()} rows differ, e.g. ours {x[bad].head(3).tolist()} vs {y[bad].head(3).tolist()}")
    return problems


def test_region_tables_match_hazus(regions):
    hazus, ours = regions
    problems = []
    for name, ref in hazus.items():
        if name in ("hzBoundary",) or name in UTILITY_FLOOD:
            continue
        if name not in ours:
            problems.append(f"{name}: not created")
            continue
        missing = [c for c in ref.columns if c not in ours[name].columns and c != "geometry"
                   and not c.startswith(NOT_PORTED.get(name, ("\0",)))]
        if missing:
            problems.append(f"{name}: missing columns {missing}")
        problems += [f"{name}: {p}" for p in _rows_differ(ours[name], ref)]
        if "geometry" in ref.columns and name in KEYS:
            a = ours[name].set_index(ours[name][KEYS[name]].astype(str).agg("|".join, axis=1).str.strip())
            b = ref.set_index(ref[KEYS[name]].astype(str).agg("|".join, axis=1).str.strip())
            same = a.geometry.reindex(b.index).geom_equals(b.geometry)
            if not same.all():
                problems.append(f"{name}: {(~same).sum()} geometries differ")
    assert not problems, "\n".join(problems)


def test_utility_flood_attributes(regions):
    """Ours are the class defaults. Hazus's utility triggers scramble them, so only its own-class rows can match."""
    from hazus.flood.facility_analysis import UTILITIES, flood_defaults

    hazus, ours = regions
    scrambled = 0
    for kind in UTILITIES.values():
        if kind.flood not in hazus:
            continue
        expected = flood_defaults(kind, ours[kind.base])
        assert not _rows_differ(ours[kind.flood], expected), kind.flood
        ref = hazus[kind.flood].set_index(kind.id_col)
        exp = expected.set_index(kind.id_col).reindex(ref.index)
        cols = ["EquipmentHt", "UtilDamageFnId"]
        own = (_normal(ref[cols].reset_index()).set_index(kind.id_col)
               == _normal(exp[cols].reset_index()).set_index(kind.id_col)).all(axis=1)
        scrambled += int((~own).sum())
    print(f"Hazus utilities with another class's defaults: {scrambled}")


def test_boundary_close_to_hazus(regions):
    hazus, ours = regions
    a = ours["hzBoundary"].geometry.union_all()
    b = hazus["hzBoundary"].geometry.union_all()
    assert a.symmetric_difference(b).area / b.area < 1e-4
