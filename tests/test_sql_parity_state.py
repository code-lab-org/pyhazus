"""Real-data differential test on a study region that Hazus itself built from a FEMA state database.

Set ``HAZUS_STATE_REGION_DB`` to the name of such a region in the SQL Server, for example one made by running
``syHazus..AggregateByCounty`` on an attached state database (see ``docs/extraction.md``). The test exports
the region with ``hazus.inventory``, runs ``run_scenario`` on synthetic depth grids for three return
periods, then runs the Hazus SQL inside that region database on the same ``flAnAreaWeighted`` /
``flAnShelterResult`` rows and compares building stock, debris, shelter and AAL.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).parent))
import sqlref  # noqa: E402
import synth  # noqa: E402
from test_sql_parity import compare_tables  # noqa: E402
from test_sql_parity_debris import TONS  # noqa: E402
from test_sql_parity_gbs import CHECKS, ECON  # noqa: E402

REGION_DB = os.environ.get("HAZUS_STATE_REGION_DB")
pytestmark = [
    pytest.mark.skipif(not REGION_DB, reason="HAZUS_STATE_REGION_DB not set"),
    pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available"),
]
pytest.importorskip("rasterio")
pytest.importorskip("geopandas")

RETURN_PERIODS = {10: 0.6, 100: 1.0, 500: 1.6}  # return period -> depth scale


def depth_grids(center, hazard: str, out_dir: Path, res: float = 0.0001, size=(0.08, 0.06)):
    """Wavy synthetic depth grids in a ``size`` window (degrees) around ``center``, dry where the field is
    negative. Depths reach about 12 ft, so a coastal grid has A, CA and V zones."""
    from hazus.flood.hazard import FloodRaster

    x0, y1 = center[0] - size[0] / 2, center[1] + size[1] / 2
    w, h = int(size[0] / res), int(size[1] / res)
    xs = x0 + (np.arange(w) + 0.5) * res
    ys = y1 - (np.arange(h) + 0.5) * res
    gx, gy = np.meshgrid(xs, ys)
    field = 1.5 + 6.0 * np.sin(2 * np.pi * (gx - x0) / (size[0] / 2.5)) * np.cos(
        2 * np.pi * (gy - y1) / (size[1] / 2.5))
    rasters = []
    for rp, scale in RETURN_PERIODS.items():
        d = field * scale
        d[d <= 0] = np.nan
        path = out_dir / f"{hazard}_{rp}.tif"
        synth.write_grid(path, d, res=res, origin=(x0, y1))
        rasters.append(FloodRaster(path, hazard, rp))
    return rasters


@pytest.fixture(scope="module")
def region(tmp_path_factory):
    from hazus.inventory.sqlserver import export_region
    from hazus.io import read_region

    out = tmp_path_factory.mktemp("region")
    with sqlref.connect(REGION_DB) as conn:
        export_region(conn, out)
    return read_region(out)


@pytest.mark.parametrize("hazard", ["riverine", "coastal"])
def test_state_region_matches_sql(region, hazard, tmp_path, monkeypatch):
    from hazus.flood.aal import aal_coefficients_decimal
    from hazus.flood.hazard import hazard_type_code
    from hazus.flood.scenario import AnalysisOptions, run_scenario

    blocks = region["hzCensusBlock"]
    of_type = blocks[blocks["BlockType"].str.strip() == ("C" if hazard == "coastal" else "R")]
    if of_type.empty:
        pytest.skip(f"the region has no {hazard} blocks")
    points = of_type.geometry.representative_point()
    monkeypatch.setattr(sqlref, "DB", REGION_DB)
    rasters = depth_grids((points.x.median(), points.y.median()), hazard, tmp_path)

    out = run_scenario(region, rasters, options=AnalysisOptions(essential_facilities=False, utilities=False))
    flooded = out["flAnAreaWeighted"]
    assert flooded["CensusBlock"].nunique() > 50, "the synthetic grids should flood a good number of blocks"

    sqlref.load_area_weighted(flooded)
    ref_gbs = sqlref.run_gbs(hazard_type_code(rasters))
    assert not sqlref.log_errors(ref_gbs["flLog"])
    ref_debris = sqlref.run_debris()
    ref_shelter = sqlref.run_shelter(out["flAnShelterResult"], hazard_type_code(rasters))
    ref_aal = sqlref.run_aal(aal_coefficients_decimal(RETURN_PERIODS))

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
        got = out[name][out[name]["ReturnPeriodId"] == "Annual"]
        problems += [f"{name} (AAL): {p}" for p in compare_tables(got, ref, CHECKS[name][0], [], ECON)]
    assert not problems, "\n".join(problems)
    assert ref_gbs["flFRGBSEcLossByTotal"]["BuildingLoss"].astype(float).sum() > 0
