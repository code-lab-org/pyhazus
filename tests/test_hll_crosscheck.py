"""Cross-check of the raster step against Hazus runs published in the Hazus Loss Library.

The library's flood analyses were run with Hazus 4.2.3 or 5.1 (ArcMap). Those versions area-weight on the
depth grid's own projected cells; Hazus 7.2, and this port, reproject to EPSG:4326 and use 0.00001° cells.
So results are compared with tolerances rather than exactly. The block set must be identical, and the
flooded share and mean depth per block must agree closely.

Prepare fixtures with ``tools/hll_fixture.py`` and set ``HAZUS_HLL_FIXTURES`` to their parent directory.
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("geopandas")

ROOT = os.environ.get("HAZUS_HLL_FIXTURES")
SCENARIOS = []
if ROOT:
    for fixture in sorted(p for p in Path(ROOT).iterdir() if (p / "scenarios.csv").exists()):
        for row in pd.read_csv(fixture / "scenarios.csv", dtype={"ReturnPeriodID": str}).to_dict("records"):
            SCENARIOS.append(pytest.param(fixture, row, id=f"{fixture.name}-{row['StudyCaseName']}"))
if not SCENARIOS:
    SCENARIOS = [pytest.param(None, None, marks=pytest.mark.skip(reason="HAZUS_HLL_FIXTURES not set or empty"))]

# Observed on lbgrandMO (Hazus 5.1): mean |diff| 0.10-0.25 pct points and 0.02-0.07 ft; max 1.9 / 0.41.
MEAN_SHARE, MAX_SHARE = 0.5, 2.5  # percentage points of the block flooded
MEAN_DEPTH, MAX_DEPTH = 0.1, 0.5  # ft, mean depth over the flooded part of the block

_regions = {}


def _region(fixture: Path):
    from hazus.io import read_region

    if fixture not in _regions:
        _regions[fixture] = read_region(fixture / "region")
    return _regions[fixture]


def _per_block(aw: pd.DataFrame) -> pd.DataFrame:
    from hazus.flood.exposure import BIN_CODES, bin_column

    bins = [bin_column(int(b)) for b in BIN_CODES]
    d = aw.assign(CensusBlock=aw["CensusBlock"].astype(str).str.strip()).groupby("CensusBlock")[bins].sum()
    share = d.sum(axis=1)
    depth = (d * (np.asarray(BIN_CODES) / 100.0)).sum(axis=1) / share.where(share > 0)
    return pd.DataFrame({"share": share, "depth": depth})


@pytest.mark.parametrize("fixture, scenario", SCENARIOS)
def test_area_weighting_matches_hazus_loss_library(fixture, scenario, tmp_path):
    from hazus.flood.hazard import FloodRaster, prepare_depth_grid
    from hazus.flood.scenario import area_weighting

    region = _region(fixture)
    grid = prepare_depth_grid(fixture / scenario["depth_grid"], tmp_path / "depth.tif", "feet")
    aw, _ = area_weighting(region["hzCensusBlock"],
                           [FloodRaster(grid, scenario["hazard"], int(scenario["ReturnPeriodID"]))],
                           boundary=region["hzBoundary"].geometry.union_all())

    ref = pd.read_parquet(fixture / "reference" / "flAnAreaWeighted.parquet")
    ref = ref[ref["StudyCaseID"] == scenario["StudyCaseID"]]
    hazus, port = _per_block(ref), _per_block(aw)
    assert set(port.index) == set(hazus.index), (
        f"blocks only in Hazus: {sorted(set(hazus.index) - set(port.index))[:5]}, "
        f"only in the port: {sorted(set(port.index) - set(hazus.index))[:5]}")
    j = hazus.join(port, lsuffix="_hazus", rsuffix="_port")
    ds = (j["share_port"] - j["share_hazus"]).abs()
    dd = (j["depth_port"] - j["depth_hazus"]).abs()
    assert ds.mean() < MEAN_SHARE and ds.max() < MAX_SHARE, f"flooded share: mean {ds.mean():.3f}, max {ds.max():.3f}"
    assert dd.mean() < MEAN_DEPTH and dd.max() < MAX_DEPTH, f"mean depth: mean {dd.mean():.3f}, max {dd.max():.3f}"
