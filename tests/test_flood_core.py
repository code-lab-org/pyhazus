import numpy as np
import pytest

from hazus.flood.aal import aal_coefficients
from hazus.flood.exposure import depth_bin, to_area_weighted_table, zone_code
from hazus.flood.facilities import (
    depth_column,
    ef_occupancy,
    essential_facility_damage,
    interpolate,
    udf_specific_occupancy_key,
    utility_damage,
)
from hazus.flood.hazard import HazardType


# --- depth binning (analysis_area_weighted.py RemapRange; boundary goes to the lower range) ---

@pytest.mark.parametrize("depth, expected", [
    (-5.0, -1), (-0.5, -1), (0.0, -1), (np.nan, -1),
    (1e-9, 0), (0.25, 0), (0.2500001, 50), (0.75, 50), (0.7500001, 100),
    (1.0, 100), (23.75, 2350), (23.7500001, 2400), (100.0, 2400), (10000.0, 2400),
])
def test_depth_bin_boundaries(depth, expected):
    assert depth_bin(np.array([depth]))[0] == expected


def test_coastal_zone_is_depth_limited():
    d = np.array([0.1, 2.99, 3.0, 5.99, 6.0, 20.0])
    assert zone_code(d, HazardType.COASTAL).tolist() == [2, 2, 3, 3, 4, 4]
    assert zone_code(d, HazardType.RIVERINE).tolist() == [1] * 6


# --- damage-function interpolation ---

WDFLT = {  # Flood TM Figure 9-1, default wastewater facility
    "ft00": 0, "ft01": 5, "ft02": 8, "ft03": 10, "ft04": 17, "ft05": 24,
    "ft06": 30, "ft07": 30, "ft08": 30, "ft09": 30, "ft10": 40,
}


def test_utility_tm_example_eq_9_2():
    # 8.5 ft of water, 3 ft equipment height -> damage at 5.5 ft = 27 %
    r = utility_damage(8.5, equipment_ht=3.0, cost=1000.0, ddf=WDFLT, functional_depth=1.0)
    assert r.dmg_pct == pytest.approx(27.0)
    assert r.loss == pytest.approx(270.0)
    assert r.flooded_depth == pytest.approx(5.5)


def test_utility_clamps_at_ft10():
    r = utility_damage(24.0, equipment_ht=0.0, cost=100.0, ddf=WDFLT, functional_depth=0.0)
    assert r.dmg_pct == pytest.approx(40.0)


@pytest.mark.parametrize("i, col", [(-7, "ft04m"), (-4, "ft04m"), (-1, "ft01m"), (0, "ft00"), (9, "ft09"),
                                     (10, "ft10"), (24, "ft24")])
def test_depth_column(i, col):
    assert depth_column(i) == col


def test_interpolate_whole_foot_returns_column_value():
    ddf = {"ft02": 10.0, "ft03": 20.0}
    assert interpolate(ddf, 2.0) == 10.0
    assert interpolate(ddf, 2.25) == pytest.approx(12.5)


def test_essential_facility_flood_protection_and_restoration():
    ddf = {f"ft0{i}": 10.0 * i for i in range(10)} | {"ft01m": 0.0, "ft02m": 0.0, "ft03m": 0.0, "ft04m": 0.0}
    rest = {"MaxDays": 480, "FunctionalDepth": 0.5}
    protected = essential_facility_damage(5.0, first_floor_ht=1.0, cost=1000.0, structure_ddf=ddf,
                                          content_ddf=ddf, content_ratio_pct=150.0, restoration=rest,
                                          flood_protection=500, return_period=100)
    assert protected.bldg_loss == 0.0
    assert protected.max_days_to_restoration == 480  # set before the protection check, as in Hazus
    hit = essential_facility_damage(5.0, first_floor_ht=1.0, cost=1000.0, structure_ddf=ddf,
                                    content_ddf=ddf, content_ratio_pct=150.0, restoration=rest,
                                    flood_protection=0, return_period=100)
    assert hit.bldg_dmg_pct == pytest.approx(40.0)
    assert hit.bldg_loss == pytest.approx(400.0)
    assert hit.cont_loss == pytest.approx(40.0 * 1000.0 * 1.5 / 100.0)
    assert hit.functionality is True  # raw Hazus flag: flooded depth 4.0 > functional depth 0.5


def test_ef_occupancy_and_udf_keys():
    assert ef_occupancy("EFHL") == "COM6"
    assert ef_occupancy("EFS2") == "EDU2"
    assert ef_occupancy("XXXX") == ""
    assert udf_specific_occupancy_key("RES1", 255, "4") == ("Res12", "RES1", "Split Level", True)
    assert udf_specific_occupancy_key("RES3C", 4, "7") == ("Res3", "RES3C", "3to4 Stories", False)
    assert udf_specific_occupancy_key("COM1", 5, "7") == ("NonRes", "COM1", "Mid Rise", False)


# --- AAL coefficients (AnalysisLogic.RunAsync) ---

def test_aal_coefficients():
    c = aal_coefficients([10, 25, 50, 100, 500])
    assert c == pytest.approx({500: 0.002, 100: 0.008, 50: 0.01, 25: 0.02, 10: 0.06})
    assert sum(c.values()) == pytest.approx(0.1)


# --- area weighting on synthetic rasters ---

@pytest.fixture
def synthetic(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    gpd = pytest.importorskip("geopandas")
    from rasterio.transform import from_origin
    from shapely.geometry import box

    # Depth grid: 0.0001 deg cells (10x the Hazus block-grid cell), 20 x 10 cells, origin (0, 0.001).
    res = 0.0001
    depth = np.zeros((10, 20), dtype="float32")
    depth[:, 0:5] = 1.0       # block A, left half  -> bin 100
    depth[:, 5:10] = 0.0      # block A, right half -> not counted
    depth[:, 10:20] = 7.0     # block B             -> bin 700
    path = tmp_path / "depth.tif"
    with rasterio.open(path, "w", driver="GTiff", height=10, width=20, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(0.0, 0.001, res, res), nodata=-9999.0) as ds:
        ds.write(depth, 1)
    blocks = gpd.GeoDataFrame({"CensusBlock": ["A", "B"]},
                              geometry=[box(0.0, 0.0, 0.001, 0.001), box(0.001, 0.0, 0.002, 0.001)],
                              crs="EPSG:4326")
    return path, blocks


def test_area_weighted_riverine(synthetic):
    from hazus.flood.exposure import area_weighted_depths
    from hazus.flood.hazard import FloodRaster

    path, blocks = synthetic
    out = area_weighted_depths(blocks, FloodRaster(path, "riverine"), tile=37)  # odd tile size on purpose
    got = {(r.CensusBlock, r.CoastalZone, r.ReClass): r.PctAffected for r in out.itertuples()}
    assert got == pytest.approx({("A", "R", 100): 50.0, ("B", "R", 700): 100.0})
    assert set(out["TotalCount"]) == {100 * 100}

    wide = to_area_weighted_table(out)
    a = wide.set_index("CensusBlock").loc["A"]
    assert a["ft100"] == pytest.approx(50.0) and a["ft00"] == 0.0 and a["ft2400"] == 0.0


def test_area_weighted_coastal_zones(synthetic):
    from hazus.flood.exposure import area_weighted_depths
    from hazus.flood.hazard import FloodRaster

    path, blocks = synthetic
    out = area_weighted_depths(blocks, FloodRaster(path, "coastal"))
    got = {(r.CensusBlock, r.CoastalZone, r.ReClass): r.PctAffected for r in out.itertuples()}
    assert got == pytest.approx({("A", "A", 100): 50.0, ("B", "CV", 700): 100.0})
