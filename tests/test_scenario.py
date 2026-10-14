"""End-to-end flood scenario (``hazus.flood.scenario``) on synthetic depth grids."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

pytest.importorskip("rasterio")
pytest.importorskip("geopandas")

from hazus.flood.hazard import FloodRaster, hazard_type_code, return_period_ids  # noqa: E402
from hazus.flood.scenario import area_weighting, run_scenario  # noqa: E402


def test_return_period_ids_and_hazard_type(tmp_path):
    r = lambda rp, h="riverine": FloodRaster(tmp_path / "x.tif", h, rp)  # noqa: E731
    assert return_period_ids([r(-1)]) == {-1: "-1"}
    assert return_period_ids([r(-1), r(-1, "coastal")]) == {-1: "mix0"}
    assert return_period_ids([r(100), r(10, "coastal")]) == {10: "10", 100: "100"}
    assert hazard_type_code([r(-1)]) == 1
    assert hazard_type_code([r(-1, "coastal")]) == 2
    assert hazard_type_code([r(-1), r(-1, "coastal")]) == 3


def test_run_scenario_multi_return_period(tmp_path):
    region, rasters = synth.make_scenario(tmp_path)
    out = run_scenario(region, rasters)

    aw = out["flAnAreaWeighted"]
    assert set(aw["ReturnPeriodID"]) == {"10", "100", "500"}
    by_rp = aw.set_index(["ReturnPeriodID", "CensusBlock"])
    first = region["hzCensusBlock"]["CensusBlock"].iloc[0]
    assert by_rp.loc[("10", first), "ft100"] == pytest.approx(100.0)
    assert by_rp.loc[("100", first), "ft300"] == pytest.approx(100.0)
    assert by_rp.loc[("500", first), "ft600"] == pytest.approx(100.0)
    assert aw["CensusBlock"].nunique() == 3  # left half flooded

    loss = out["flFRGBSEcLossByTotal"]
    assert "Annual" in set(loss["ReturnPeriodId"])
    totals = loss.groupby("ReturnPeriodId")["BuildingLoss"].sum()
    assert totals["10"] <= totals["100"] <= totals["500"]
    assert totals["500"] > 0
    assert len(out["flReturnPeriod"]) == 3

    care = out["flFRCareFlty"].set_index("ReturnPeriodId")
    assert list(care.index) == ["10", "100", "500"]
    assert care.loc["500", "BldgDmgPct"] == pytest.approx(30.0 + 5.0 * 0.5)  # 5.5 ft above the first floor
    for name in ["flGBSFinal", "flFRGBSDmgSOccupB", "flFRDebris", "flFRShelter"]:
        assert name in out and len(out[name]) > 0, name


def test_last_grid_of_same_period_and_hazard_wins(tmp_path):
    region, rasters = synth.make_scenario(tmp_path, depths={-1: 2.0})
    deeper = tmp_path / "deeper.tif"
    synth.write_grid(deeper, np.full((5, 30), 9.0))
    blocks = region["hzCensusBlock"]
    aw, sr = area_weighting(blocks, rasters + [FloodRaster(deeper, "riverine", -1)])
    assert (aw["ft900"] == 100.0).all() and len(aw) == len(blocks)
    assert (aw["ReturnPeriodID"] == "-1").all()
    out = run_scenario(region, rasters + [FloodRaster(deeper, "riverine", -1)])
    assert out["flFRCareFlty"].iloc[0]["BldgDmgPct"] == pytest.approx(43.0 + 0.5 * 4.0)  # max: 9 - 0.5 = 8.5 ft


def test_create_scenario_merges_single_event_grids(tmp_path):
    from hazus.flood.hazard import DepthGrid, create_scenario

    a, b = tmp_path / "a.tif", tmp_path / "b.tif"
    grid = np.full((5, 10), np.nan)
    grid[:, :5] = 1.0
    synth.write_grid(a, grid)
    grid = np.full((5, 10), np.nan)
    grid[:, 3:] = 2.0  # metres
    synth.write_grid(b, grid)
    rasters = create_scenario([DepthGrid(a, "riverine"), DepthGrid(b, "riverine", units="meters")], tmp_path / "w")
    assert len(rasters) == 1 and rasters[0].return_period == -1
    import rasterio

    with rasterio.open(rasters[0].path) as ds:
        merged = ds.read(1)
    assert merged[0, 0] == pytest.approx(1.0)
    assert merged[0, 4] == pytest.approx(2.0 * 5000 / 1524)  # overlap: maximum
    assert merged[0, 9] == pytest.approx(2.0 * 5000 / 1524)


def test_create_scenario_validates_return_periods(tmp_path):
    from hazus.flood.hazard import DepthGrid, create_scenario

    g = lambda rp, h="riverine": DepthGrid(tmp_path / "x.tif", h, rp)  # noqa: E731
    with pytest.raises(ValueError, match="unique"):
        create_scenario([g(10), g(10), g(100)], tmp_path)
    with pytest.raises(ValueError, match="three"):
        create_scenario([g(10), g(100)], tmp_path)
    with pytest.raises(ValueError, match="> 0"):
        create_scenario([g(10), g(100), g(-1)], tmp_path)


def test_cli_run_matches_api(tmp_path):
    import pandas as pd

    from hazus.cli import main
    from hazus.io import read_region, write_region

    region, rasters = synth.make_scenario(tmp_path, depths={-1: 2.5})
    write_region(region, tmp_path / "region")
    assert main(["flood", "run", str(tmp_path / "region"), str(tmp_path / "out"),
                 "--riverine", str(rasters[0].path), "--format", "csv", "--skip", "shelter"]) == 0
    got = pd.read_csv(tmp_path / "out" / "flFRGBSEcLossByTotal.csv", dtype={"CensusBlock": str})
    expected = run_scenario(read_region(tmp_path / "region"), rasters)["flFRGBSEcLossByTotal"]
    assert got["BuildingLoss"].sum() == expected["BuildingLoss"].sum() > 0
    assert not (tmp_path / "out" / "flFRShelter.csv").exists()
    assert (tmp_path / "out" / "flFRCareFlty.csv").exists()
