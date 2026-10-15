"""Debris differential test: Python port vs. the original Hazus ``CalculateDebris``."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).parent))
import sqlref  # noqa: E402
import synth  # noqa: E402
from test_sql_parity import compare_tables  # noqa: E402

pytestmark = pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available")

TONS = ["PreFinishTons", "PostAFinishTons", "PostVFinishTons", "PreStructureTons", "PostAStructureTons",
        "PostVStructureTons", "PreFoundationTons", "PostAFoundationTons", "PostVFoundationTons"]


LAKES = {"state_fips": "36", "state_abbr": "NY", "coastal_share": 0.3, "lake_share": 0.5}


@pytest.mark.parametrize("rp, keep, options, lake_hazard", [
    ("-1", "R", {}, "C"), ("-1", "C", {}, "C"), ("mix0", None, {}, "C"),
    ("-1", "R", LAKES, "R"), ("-1", "C", LAKES, "C")])
def test_debris_matches_sql(rp, keep, options, lake_hazard):
    from hazus.flood.debris import calculate_debris

    region = synth.make_region(seed=3, n_blocks=15, **options)
    aw = synth.make_area_weighted(region, seed=3, return_period=rp, lake_hazard=lake_hazard)
    if keep:
        aw = aw[aw["ControllingHazard"] == keep]
    sqlref.load_study_region(region, aw)
    ref = sqlref.run_debris()
    got = calculate_debris(region, aw)
    problems = compare_tables(got, ref, ["CensusBlock", "StudyCaseId", "ReturnPeriodId", "TypeOfHaz"], TONS,
                              atol=0.0051)
    assert not problems, "\n".join(problems)
    assert len(ref) and (ref[TONS].astype(float).sum().sum() > 0)
