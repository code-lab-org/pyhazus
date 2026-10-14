"""Essential-facility and utility driver (``hazus.flood.facility_analysis``) on synthetic inventory tables."""

import numpy as np
import pandas as pd
import pytest

from hazus.flood.facility_analysis import (
    ESSENTIAL_FACILITIES,
    UTILITIES,
    FacilityDamageFunctions,
    essential_facility_results,
    run_facilities,
    utility_results,
)

pytest.importorskip("pyarrow")

CARE = ESSENTIAL_FACILITIES["care"]
EOC = ESSENTIAL_FACILITIES["emergency"]
WATER = UTILITIES["potable_water"]


@pytest.fixture(scope="module")
def functions():
    return FacilityDamageFunctions()


def care_tables(ids, classes, costs, ffh, bldg_fn, cont_fn, protection):
    base = pd.DataFrame({"CareFltyId": ids, "EfClass": classes, "Cost": costs,
                         "Longitude": [0.0] * len(ids), "Latitude": [0.0] * len(ids)})
    flood = pd.DataFrame({"CareFltyId": ids, "FirstFloorHt": ffh, "BldgDamageFnId": bldg_fn,
                          "ContDamageFnId": cont_fn, "FloodProtection": protection})
    return base, flood


def water_tables(ids, classes, costs, equip, fn, protection):
    base = pd.DataFrame({"PotableWaterFltyId": ids, "UtilFcltyClass": classes, "Cost": costs,
                         "Longitude": [0.0] * len(ids), "Latitude": [0.0] * len(ids)})
    flood = pd.DataFrame({"PotableWaterFltyId": ids, "EquipmentHt": equip, "UtilDamageFnId": fn,
                          "FloodProtection": protection})
    return base, flood


def test_default_ids_follow_hazard_and_depth():
    xref = pd.DataFrame({"SOccupId": ["PWTL", "PWTL", "PWTL"], "UtilFltyDmgFnId": [7, 10, 11],
                         "DefaultFn": [True, False, False], "Selected": [False, True, True],
                         "HazardR": [True, False, False], "HazardCA": [False, True, False],
                         "HazardCV": [False, False, True]})
    f = FacilityDamageFunctions({"flUtilFltyDmgXRef": xref})
    assert f.default_id("util", "R", 9.0, "PWTL ") == 7
    assert f.default_id("util", "C", 6.0, "PWTL") == 10  # CV only when deeper than 6 ft
    assert f.default_id("util", "C", 6.5, "PWTL") == 11
    assert f.default_id("util", "R", 1.0, "XXXX") is None


def test_default_id_ambiguous_raises():
    xref = pd.DataFrame({"SOccupId": ["PWTL", "PWTL"], "UtilFltyDmgFnId": [7, 10], "DefaultFn": [True, True],
                         "Selected": [False, False], "HazardR": [True, True], "HazardCA": [True, True],
                         "HazardCV": [True, True]})
    with pytest.raises(LookupError):
        FacilityDamageFunctions({"flUtilFltyDmgXRef": xref}).default_id("util", "R", 1.0, "PWTL")


def test_essential_facility_rows(functions):
    # EFHS: structure fn 474 (ft03 20, ft04 25), content fn 309 (ft03 10, ft04 20), COM6 contents 150 %.
    base, flood = care_tables(["MD000001", "MD000002", "MD000003"], ["EFHS", "EFHS", "EFHS"],
                              [1000.0, 1000.0, 1000.0], [1.0, 1.0, 1.0], ["474", "999999", None],
                              ["309", "309", "309"], [0, 0, 500])
    out = essential_facility_results(CARE, base, flood, [4.5, 4.5, 4.5], ["R", "R", "R"], return_period="100",
                                     functions=functions)
    assert list(out.columns) == ["StudyCaseId", "ReturnPeriodId", "CareFltyId", "ControlHazard", "BldgDmgPct",
                                 "BldgLoss", "ContDmgPct", "ContLoss", "Functionality",
                                 "MaxTimetoFullRestoration", "AnalysisOptId"]
    hit = out.iloc[0]
    assert hit["BldgDmgPct"] == pytest.approx(22.5)  # flooded 3.5 ft
    assert hit["BldgLoss"] == pytest.approx(225.0)
    assert hit["ContDmgPct"] == pytest.approx(15.0)
    assert hit["ContLoss"] == pytest.approx(15.0 * 1000.0 * 1.5 / 100.0)
    assert hit["MaxTimetoFullRestoration"] == 540
    assert bool(hit["Functionality"]) is True
    # unknown fn id falls back to the xref default (474), identical result
    assert out.iloc[1][["BldgDmgPct", "ContLoss"]].tolist() == out.iloc[0][["BldgDmgPct", "ContLoss"]].tolist()
    # protected beyond the return period: no damage, restoration days still set
    prot = out.iloc[2]
    assert prot["BldgLoss"] == 0.0 and prot["ContLoss"] == 0.0
    assert prot["MaxTimetoFullRestoration"] == 540
    assert (out["ReturnPeriodId"] == "100").all() and (out["AnalysisOptId"] == 0).all()


def test_single_event_ignores_flood_protection(functions):
    base, flood = care_tables(["A"], ["EFHS"], [1000.0], [1.0], ["474"], ["309"], [500])
    out = essential_facility_results(CARE, base, flood, [4.5], ["R"], return_period="-1", functions=functions)
    assert out.iloc[0]["BldgDmgPct"] == pytest.approx(22.5)


def test_dry_elevated_facility_keeps_initial_restoration(functions):
    # depth 0 (outside every grid), first floor 5 ft up: no restoration row (Hazus would crash).
    base, flood = care_tables(["A"], ["EFHS"], [1000.0], [5.0], ["474"], ["309"], [0])
    out = essential_facility_results(CARE, base, flood, [0.0], ["R"], functions=functions)
    row = out.iloc[0]
    assert row["MaxTimetoFullRestoration"] == 1
    assert row["BldgLoss"] == 0.0
    assert bool(row["Functionality"]) is False  # -5 ft is not above the functional depth


def test_emergency_center_flood_table_id_column_case(functions):
    base = pd.DataFrame({"EocId": ["E1"], "EfClass": ["EFEO"], "Cost": [100.0]})
    flood = pd.DataFrame({"Eocid": ["E1"], "FirstFloorHt": [0.0], "BldgDamageFnId": ["0"],
                          "ContDamageFnId": ["0"], "FloodProtection": [0]})
    out = essential_facility_results(EOC, base, flood, [2.0], ["C"], functions=functions)
    assert out.iloc[0]["EocId"] == "E1" and out.iloc[0]["ControlHazard"] == "C"


def test_missing_flood_row_raises(functions):
    base, flood = care_tables(["A"], ["EFHS"], [1000.0], [1.0], ["474"], ["309"], [0])
    with pytest.raises(LookupError):
        essential_facility_results(CARE, base, flood.iloc[:0], [1.0], ["R"], functions=functions)


def test_utility_rows(functions):
    # PWTL: fn 7 (ft00 0, ft01 5, ft02 8, ft03 10), functional depth 4.
    base, flood = water_tables(["W1", "W2", "W3"], ["PWTL", "PWTL", "PWTL"], [2000.0] * 3,
                               [3.0, 3.0, 3.0], ["7", "abc", "7"], [0, 0, 0])
    out = utility_results(WATER, base, flood, [5.5, 5.5, 2.5], ["R", "R", "R"], functions=functions)
    assert list(out.columns) == ["StudyCaseId", "ReturnPeriodId", "PotableWaterFltyId", "DmgPct", "Loss",
                                 "Functionality", "AnalysisOptId"]
    assert out.iloc[0]["DmgPct"] == pytest.approx(9.0)
    assert out.iloc[0]["Loss"] == pytest.approx(180.0)
    assert bool(out.iloc[0]["Functionality"]) is False  # 2.5 ft above equipment, not > 4
    assert out.iloc[1]["DmgPct"] == out.iloc[0]["DmgPct"]  # unparsable id -> xref default 7
    # below equipment height: utility functions have no ft01m column, so 0 is interpolated towards ft00
    assert out.iloc[2]["DmgPct"] == 0.0


def test_utility_deep_clamps_at_ft10(functions):
    base, flood = water_tables(["W1"], ["PWTL"], [2000.0], [0.0], ["7"], [0])
    out = utility_results(WATER, base, flood, [30.0], ["R"], functions=functions)
    assert out.iloc[0]["DmgPct"] == pytest.approx(40.0)
    assert bool(out.iloc[0]["Functionality"]) is True


def test_unknown_utility_class_raises(functions):
    base, flood = water_tables(["W1"], ["NOPE"], [1.0], [0.0], ["7"], [0])
    with pytest.raises(LookupError):
        utility_results(WATER, base, flood, [1.0], ["R"], functions=functions)


def test_run_facilities_samples_rasters(tmp_path, functions):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from hazus.flood.hazard import FloodRaster

    path = tmp_path / "d.tif"
    depth = np.full((10, 10), 4.5, dtype="float32")
    depth[:, 5:] = -9999.0
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(0.0, 0.001, 0.0001, 0.0001), nodata=-9999.0) as ds:
        ds.write(depth, 1)
    base, flood = care_tables(["A", "B"], ["EFHS", "EFHS"], [1000.0, 1000.0], [1.0, 1.0], ["474", "474"],
                              ["309", "309"], [0, 0])
    base["Longitude"] = [0.00025, 0.00075]  # A in the wet half, B on nodata
    base["Latitude"] = [0.0005, 0.0005]
    region = {"hzCareFlty": base, "flCareFlty": flood}
    out = run_facilities(region, [FloodRaster(path, "riverine", 100)], functions=functions)
    care = out["flFRCareFlty"]
    assert set(out) == {"flFRCareFlty"}
    assert care.set_index("CareFltyId").loc["A", "BldgDmgPct"] == pytest.approx(22.5)
    assert care.set_index("CareFltyId").loc["B", "BldgDmgPct"] == 0.0  # evaluated at depth 0
    assert (care["ReturnPeriodId"] == "100").all()


def test_flood_defaults_by_class():
    from hazus.flood.facility_analysis import flood_defaults

    care = pd.DataFrame({"CareFltyId": ["C1", "C2", "C3"], "EfClass": ["EFHS", "EFMC ", "NOPE"]})
    d = flood_defaults(CARE, care).set_index("CareFltyId")
    assert d.loc["C1", "BldgDamageFnId"] == "474" and d.loc["C1", "FirstFloorHt"] == 3.0
    assert d.loc["C2", "BldgDamageFnId"] == "475"
    assert pd.isna(d.loc["C3", "FirstFloorHt"])
    water = pd.DataFrame({"PotableWaterFltyId": ["W1", "W2"], "UtilFcltyClass": ["PWTL", "PPPL"]})
    d = flood_defaults(WATER, water).set_index("PotableWaterFltyId")
    assert d.loc["W1", "UtilDamageFnId"] == "7" and d.loc["W2", "UtilDamageFnId"] == "17"  # each its own class
    assert (d["FoundationType"] == "7").all()


def test_run_facilities_without_flood_table_uses_defaults(tmp_path, functions):
    pytest.importorskip("rasterio")
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    import synth
    from hazus.flood.hazard import FloodRaster

    path = tmp_path / "d.tif"
    synth.write_grid(path, np.full((5, 5), 6.5))
    base, _ = care_tables(["A"], ["EFHS"], [1000.0], [0.0], ["0"], ["0"], [0])
    base["Longitude"], base["Latitude"] = 0.00025, 0.00025
    out = run_facilities({"hzCareFlty": base}, [FloodRaster(path, "riverine")], functions=functions)
    assert out["flFRCareFlty"].iloc[0]["BldgDmgPct"] == pytest.approx(22.5)  # default first floor 3 ft: 3.5 ft
