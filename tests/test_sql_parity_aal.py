"""AAL differential test: Python port vs. the original Hazus ``absp_AALDoIt``."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).parent))
import sqlref  # noqa: E402
import synth  # noqa: E402
from test_sql_parity import compare_tables  # noqa: E402
from test_sql_parity_gbs import BT_KEYS, ECON, GOCC_KEYS, SOCC_KEYS, TOT_KEYS  # noqa: E402

pytestmark = pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available")

RPS = [10, 25, 50, 100, 500]


@pytest.mark.parametrize("haz, keep", [(1, "R"), (2, "C")])
def test_aal_matches_sql(haz, keep):
    from hazus.flood.aal import aal_coefficients_decimal, aal_gbs
    from hazus.flood.gbs import run_gbs

    region = synth.make_region(seed=9, n_blocks=12)
    parts = []
    for k, rp in enumerate(RPS):  # deeper flooding for rarer events; some blocks dry for frequent ones
        aw = synth.make_area_weighted(region, seed=100 + k, return_period=str(rp), flood_share=0.4 + 0.12 * k)
        parts.append(aw[aw["ControllingHazard"] == keep])
    aw = pd.concat(parts, ignore_index=True)
    coefs = aal_coefficients_decimal(RPS)

    sqlref.load_study_region(region, aw)
    sqlref.run_gbs(haz)
    ref = sqlref.run_aal(coefs)

    gbs = run_gbs(region, aw, haz)
    got = aal_gbs(gbs["flFRGBSEcLossBySOccup"], coefs, region)
    checks = {"flFRGBSEcLossBySOccup": SOCC_KEYS, "flFRGBSEcLossByGOccup": GOCC_KEYS,
              "flFRGBSEcLossByTotal": TOT_KEYS, "flFRGBSEcLossByGBldgType": BT_KEYS}
    problems = []
    for name, keys in checks.items():
        problems += [f"{name}: {p}" for p in compare_tables(got[name], ref[name], keys, [], ECON)]
    assert not problems, "\n".join(problems)
    assert len(ref["flFRGBSEcLossBySOccup"]) > 0
