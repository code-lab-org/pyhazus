"""Differential tests against the original Hazus T-SQL.

Skipped unless a SQL Server with the Hazus databases is reachable (HAZUS_SA_PASSWORD etc.; see
``tools/export_db.py``).
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import sqlref  # noqa: E402

pytestmark = pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available")

DMG_KEYS = ["ZoneID", "FirmID", "BlockTypeID", "HazardTypeID", "FoundationID", "STORYID", "OCCID", "DepthID", "DISID"]
DMG_VALS = ["PctDmgStr", "PctDmgCont", "PctDmgInv"]


def _norm_keys(df: "pd.DataFrame", keys: list[str]) -> "pd.DataFrame":
    import pandas as pd

    out = df.copy()
    for k in keys:
        if pd.api.types.is_bool_dtype(out[k]):
            out[k] = out[k].astype(int)
        elif pd.api.types.is_numeric_dtype(out[k]):
            out[k] = out[k].astype("int64")
        else:
            out[k] = out[k].astype(str).str.rstrip()
    return out


def compare_tables(got, ref, keys, float_cols=(), int_cols=(), rtol=1e-6, atol=1e-6, int_tol=0):
    """Join on ``keys`` and compare values; returns a list of human-readable mismatches."""
    import pandas as pd

    problems = []
    got = _norm_keys(got, keys)
    ref = _norm_keys(ref, keys)
    m = got.merge(ref, on=keys, how="outer", suffixes=("_py", "_sql"), indicator=True)
    only_py = m[m["_merge"] == "left_only"]
    only_sql = m[m["_merge"] == "right_only"]
    if len(only_py):
        problems.append(f"{len(only_py)} rows only in Python, e.g. {only_py[keys].head(3).to_dict('records')}")
    if len(only_sql):
        problems.append(f"{len(only_sql)} rows only in SQL, e.g. {only_sql[keys].head(3).to_dict('records')}")
    both = m[m["_merge"] == "both"]
    for c in float_cols:
        a = pd.to_numeric(both[f"{c}_py"], errors="coerce").to_numpy(float)
        b = pd.to_numeric(both[f"{c}_sql"], errors="coerce").to_numpy(float)
        bad = ~np.isclose(a, b, rtol=rtol, atol=atol, equal_nan=True)
        if bad.any():
            ex = both.loc[bad, keys + [f"{c}_py", f"{c}_sql"]].head(3).to_dict("records")
            problems.append(f"{c}: {bad.sum()} mismatches, e.g. {ex}")
    for c in int_cols:
        a = pd.to_numeric(both[f"{c}_py"], errors="coerce").to_numpy(float)
        b = pd.to_numeric(both[f"{c}_sql"], errors="coerce").to_numpy(float)
        bad = ~((np.abs(a - b) <= int_tol) | (np.isnan(a) & np.isnan(b)))
        if bad.any():
            ex = both.loc[bad, keys + [f"{c}_py", f"{c}_sql"]].head(3).to_dict("records")
            problems.append(f"{c}: {bad.sum()} mismatches, e.g. {ex}")
    return problems


@pytest.mark.parametrize("haz_type", [1, 2, 3])
def test_dmg_final_matches_sql(haz_type):
    from hazus.flood.ddf import build_dmg_final

    ref = sqlref.dmg_final(haz_type)
    got = build_dmg_final(haz_type)
    assert len(got) == len(ref)
    ref = ref.sort_values(DMG_KEYS + DMG_VALS, ignore_index=True)
    got = got.sort_values(DMG_KEYS + DMG_VALS, ignore_index=True)
    for c in DMG_KEYS:
        np.testing.assert_array_equal(got[c].to_numpy(np.int64), ref[c].to_numpy(np.int64), err_msg=c)
    for c in DMG_VALS:
        np.testing.assert_allclose(got[c].to_numpy(np.float64), ref[c].astype(float).to_numpy(), rtol=1e-6,
                                   atol=1e-6, err_msg=c)
