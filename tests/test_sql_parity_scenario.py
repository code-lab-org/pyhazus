"""End-to-end differential test: ``run_scenario`` on depth grids vs. the Hazus SQL fed with the same
``flAnAreaWeighted`` / ``flAnShelterResult`` rows (building stock, debris, shelter and AAL)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).parent))
import sqlref  # noqa: E402
import synth  # noqa: E402
from test_sql_parity import compare_tables  # noqa: E402
from test_sql_parity_debris import TONS  # noqa: E402
from test_sql_parity_gbs import CHECKS, ECON  # noqa: E402

pytestmark = [pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available"),
              pytest.mark.filterwarnings("ignore::UserWarning")]
pytest.importorskip("rasterio")
pytest.importorskip("geopandas")


@pytest.mark.parametrize("hazard, depths", [("riverine", {10: 1.0, 100: 3.0, 500: 6.0}),
                                            ("coastal", {25: 2.0, 100: 4.5, 500: 8.0})])
def test_scenario_matches_sql(tmp_path, hazard, depths):
    from hazus.flood.aal import aal_coefficients_decimal
    from hazus.flood.hazard import hazard_type_code
    from hazus.flood.scenario import run_scenario

    region, rasters = synth.make_scenario(tmp_path, seed=6, n_blocks=8, depths=depths, hazard=hazard)
    out = run_scenario(region, rasters)
    haz = hazard_type_code(rasters)

    sqlref.load_study_region(region, out["flAnAreaWeighted"])
    ref_gbs = sqlref.run_gbs(haz)
    assert not sqlref.log_errors(ref_gbs["flLog"])
    ref_debris = sqlref.run_debris()
    ref_shelter = sqlref.run_shelter(out["flAnShelterResult"], haz)
    ref_aal = sqlref.run_aal(aal_coefficients_decimal(depths))

    problems = []
    for name, (keys, float_cols, int_cols) in CHECKS.items():
        got = out[name][out[name]["ReturnPeriodId"] != "Annual"]
        problems += [f"{name}: {p}" for p in compare_tables(got, ref_gbs[name], keys, float_cols, int_cols)]
    problems += [f"flFRDebris: {p}" for p in compare_tables(
        out["flFRDebris"], ref_debris, ["CensusBlock", "StudyCaseId", "ReturnPeriodId", "TypeOfHaz"], TONS,
        atol=0.0051)]
    problems += [f"flFRShelter: {p}" for p in compare_tables(
        out["flFRShelter"], ref_shelter["flFRShelter"], ["StudyCaseId", "ReturnPeriodId", "CensusBlock"],
        ["DisplacedPop", "ShortTermNeeds"])]
    for name, ref in ref_aal.items():
        keys = CHECKS[name][0]
        got = out[name][out[name]["ReturnPeriodId"] == "Annual"]
        problems += [f"{name} (AAL): {p}" for p in compare_tables(got, ref, keys, [], ECON)]
    assert not problems, "\n".join(problems)
    assert len(ref_aal["flFRGBSEcLossByTotal"]) > 0
    assert ref_gbs["flFRGBSEcLossByTotal"]["BuildingLoss"].astype(float).sum() > 0
