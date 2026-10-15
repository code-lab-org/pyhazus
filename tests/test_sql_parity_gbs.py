"""GBS differential tests: Python port vs. the original Hazus ``absp_GBSDoIt`` on synthetic study regions."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).parent))
import sqlref  # noqa: E402
import synth  # noqa: E402
from test_sql_parity import compare_tables  # noqa: E402

pytestmark = pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available")

BINS = synth_bins = ["NoDmg", "Dmg1to10", "Dmg11to20", "Dmg21to30", "Dmg31to40", "Dmg41to50", "Dmg51to60",
                     "Dmg61to70", "Dmg71to80", "Dmg81to90", "Dmg91to100"]
ECON = ["BuildingLoss", "ContentsLoss", "InventoryLoss", "RelocationCost", "IncomeLoss", "RentalIncomeLoss",
        "WageLoss", "OutputLoss", "EmploymentLoss"]
SOCC_KEYS = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "SOccup", "ControlHazard", "Pre_PostFirm"]
GOCC_KEYS = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "GenOccup", "Pre_PostFirm"]
TOT_KEYS = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "Pre_PostFirm"]
BT_KEYS = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "BldgType", "Pre_PostFirm"]

CHECKS = {
    "flFRGBSDmgSOccupB": (SOCC_KEYS, [f"SqFt{b}" for b in BINS], []),
    "flFRGBSDmgGOccupB": (GOCC_KEYS, [f"SqFt{b}" for b in BINS], []),
    "flFRGBSDmgGBldgType": (BT_KEYS, [f"SqFt{b}" for b in BINS], []),
    "flFRGBSEcLossBySOccup": (SOCC_KEYS, [], ECON),
    "flFRGBSEcLossByGOccup": (GOCC_KEYS, [], ECON),
    "flFRGBSEcLossByTotal": (TOT_KEYS, [], ECON),
    "flFRGBSEcLossByGBldgType": (BT_KEYS, [], ECON),
    "flFRGBSDmgCountSOccupB": (SOCC_KEYS, [], [f"Count{b}" for b in BINS]),
    "flFRGBSDmgCountGOccupB": (GOCC_KEYS, [], [f"Count{b}" for b in BINS]),
    "flFRGBSDmgCountGBldgType": (BT_KEYS, [], [f"Count{b}" for b in BINS]),
    "flFRGBSDepEcLossBySOccup": (SOCC_KEYS, [], ["BuildingLoss", "ContentsLoss"]),
    "flFRGBSDepEcLossByGOccup": (GOCC_KEYS, [], ["BuildingLoss", "ContentsLoss"]),
    "flFRGBSDepEcLossByTotal": (TOT_KEYS, [], ["BuildingLoss", "ContentsLoss"]),
    "flFRGBSDepEcLossByGBldgType": (BT_KEYS, [], ["BuildingLoss", "ContentsLoss"]),
}

LAKES = {"state_fips": "36", "state_abbr": "NY", "coastal_share": 0.3, "lake_share": 0.5}
SCENARIOS = {  # haz type, return period, kept hazard, region options, hazard of Great Lakes blocks
    "riverine": (1, "-1", "R", {}, "C"),
    "coastal": (2, "-1", "C", {}, "C"),
    "both": (3, "mix0", None, {}, "C"),
    "lakes-riverine": (1, "-1", "R", LAKES, "R"),
    "lakes-coastal": (2, "-1", "C", LAKES, "C"),
}


@pytest.fixture(scope="module", params=list(SCENARIOS))
def gbs_case(request):
    from hazus.flood.gbs import run_gbs

    haz, rp, keep, options, lake_hazard = SCENARIOS[request.param]
    region = synth.make_region(seed=7, n_blocks=10, **options)
    aw = synth.make_area_weighted(region, seed=7, return_period=rp, lake_hazard=lake_hazard)
    if keep:
        aw = aw[aw["ControllingHazard"] == keep]
    sqlref.load_study_region(region, aw)
    ref = sqlref.run_gbs(haz)
    got = run_gbs(region, aw, haz)
    return request.param, got, ref


@pytest.mark.parametrize("name", list(CHECKS))
def test_gbs_table_matches_sql(gbs_case, name):
    scenario, got, ref = gbs_case
    keys, float_cols, int_cols = CHECKS[name]
    problems = compare_tables(got[name], ref[name], keys, float_cols, int_cols)
    assert not problems, f"[{scenario}] {name}:\n" + "\n".join(problems)
