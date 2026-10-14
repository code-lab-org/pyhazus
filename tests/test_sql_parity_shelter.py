"""Shelter differential test: Python port vs. the original Hazus ``absp_SHRDoIT``."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).parent))
import sqlref  # noqa: E402
import synth  # noqa: E402
from test_sql_parity import compare_tables  # noqa: E402

pytestmark = pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available")


def _shelter_inputs(region, aw, rp):
    """flAnShelterResult rows as absp_GBSInsertAW writes them: Σ PctAffected / 100 per block and hazard."""
    from hazus.flood.gbs import unpivot_area_weighted
    from hazus.flood.shelter import shelter_result

    long = unpivot_area_weighted(aw)
    parts = [shelter_result(g, study_case_id=1, return_period=rp, control_hazard=ch)
             for ch, g in long.groupby("ControllingHazard")]
    return pd.concat(parts, ignore_index=True)


@pytest.mark.parametrize("haz, rp, keep", [(1, "-1", "R"), (2, "-1", "C"), (3, "mix0", None)])
def test_shelter_matches_sql(haz, rp, keep):
    from hazus.flood.shelter import run_shelter

    region = synth.make_region(seed=5, n_blocks=20)
    aw = synth.make_area_weighted(region, seed=5, return_period=rp)
    if keep:
        aw = aw[aw["ControllingHazard"] == keep]
    sr = _shelter_inputs(region, aw, rp)
    sqlref.load_study_region(region, aw)
    ref = sqlref.run_shelter(sr, haz)
    assert not sqlref.log_errors(ref["flLog"])
    got = run_shelter(region, sr)
    problems = compare_tables(got["flFRShelter"], ref["flFRShelter"],
                              ["StudyCaseId", "ReturnPeriodId", "CensusBlock"], ["DisplacedPop", "ShortTermNeeds"])
    assert not problems, "\n".join(problems)
    assert (ref["flFRShelter"]["ShortTermNeeds"].astype(float) > 0).any(), "test data should produce shelter needs"
