"""Study-region creation (``hazus.inventory.region``) on a small synthetic state inventory."""

import numpy as np
import pandas as pd
import pytest

gpd = pytest.importorskip("geopandas")
from shapely.geometry import box  # noqa: E402

from hazus.inventory.region import OCCUPANCIES, create_region, select, to_region_types  # noqa: E402


def make_state():
    """Two counties, three tracts, five blocks (one coastal), one hospital and one water plant."""
    st = {}
    st["hzCounty"] = gpd.GeoDataFrame({"CountyFips": ["24001", "24003"], "State": ["MD", "MD"]},
                                      geometry=[box(0, 0, 0.01, 0.01), box(0.01, 0, 0.02, 0.01)], crs="EPSG:4326")
    st["hzTract"] = gpd.GeoDataFrame({"Tract": ["24001000100", "24001000200", "24003000100"],
                                      "CountyFips": ["24001", "24001", "24003"],
                                      "BldgSchemesId": ["MD1", "MD1", "MD2"]},
                                     geometry=[box(0, 0, 0.005, 0.01), box(0.005, 0, 0.01, 0.01),
                                               box(0.01, 0, 0.02, 0.01)], crs="EPSG:4326")
    blocks = ["240010001001000", "240010001001001", "240010002001000", "240030001001000", "240030001001001"]
    st["hzCensusBlock"] = gpd.GeoDataFrame({
        "CensusBlock": blocks, "Tract": [b[:11] for b in blocks], "BldgSchemesId": ["MD1"] * 3 + ["MD2"] * 2,
        "BlockType": ["R", "C", "R", "R", "R"], "IncomeRatio": [0.9, 2.5, 0.3, 1.3, 0.6]},
        geometry=[box(i * 0.004, 0, i * 0.004 + 0.003, 0.003) for i in range(5)], crs="EPSG:4326")
    st["hzDemographicsB"] = pd.DataFrame({"CensusBlock": blocks, "MedianYearBuilt": [1970, 1990, 1850, 2000, 1980],
                                          "Population": [100, 50, 10, 70, 30]})
    expo = {f"{o}I": np.zeros(5) for o in OCCUPANCIES}
    expo["RES1I"] = np.array([1000.7, 500.0, 300.0, 800.0, 200.0])
    expo["COM1I"] = np.array([3989.99543, 0.0, 0.0, 100.0, 50.0])  # float in the state, int in the region
    for name in ("hzExposureOccupB", "hzExposureContentOccupB", "hzBldgCountOccupB"):
        st[name] = pd.DataFrame({"CensusBlock": blocks, **expo})
    st["hzSqFootageOccupB"] = pd.DataFrame({"CensusBlock": blocks, **{f"{o}F": np.full(5, 1.1) for o in OCCUPANCIES}})
    st["hzGenBldgSchemes"] = pd.DataFrame({"BldgSchemesId": ["MD1", "MD2", "MD3"]})
    st["hzGenBldgScheme"] = pd.DataFrame({"GenBldgSchemeID": [1, 2, 3], "BldgSchemesId": ["MD1", "MD2", "MD3"],
                                          "Occupancy": ["RES1", "RES1", "RES1"], "WPct": [90, 80, 70],
                                          "CPct": [10, 20, 30], "SPct": [0, 0, 0], "MPct": [0, 0, 0], "HPct": [0, 0, 0]})
    st["flSchemeInfo"] = pd.DataFrame({"SchemeId": ["MDR00001", "MDC00001"], "SchemeName": ["r", "c"]})
    st["flSchemeMapping"] = pd.DataFrame({"CensusBlock": blocks, "SchemeId": ["MDR00001"] * 5,
                                          "EntryDate": [1975, 0, None, 1980, 1985]})
    st["hzCareFlty"] = gpd.GeoDataFrame({"CareFltyId": ["MD000001", "MD000002"], "EfClass": ["EFHS", "EFXX"],
                                         "Tract": ["24001000100", "24003000100"], "Cost": [100.123456, 5.0]},
                                        geometry=gpd.points_from_xy([0.001, 0.015], [0.001, 0.001]), crs="EPSG:4326")
    st["flCareFlty"] = pd.DataFrame({"CareFltyId": ["MD000001", "MD000002"], "FirstFloorHt": [9.0, 9.0],
                                     "BldgDamageFnId": ["1", "1"], "ContDamageFnId": ["1", "1"],
                                     "FloodProtection": [0, 0]})
    st["hzPotableWaterFlty"] = pd.DataFrame({"PotableWaterFltyId": ["W1"], "UtilFcltyClass": ["PWTL"],
                                             "Tract": ["24001000200"], "Cost": [10.0]})
    st["flPotableWaterFlty"] = pd.DataFrame({"PotableWaterFltyId": ["W1"], "EquipmentHt": [9.0],
                                             "UtilDamageFnId": ["999"], "FloodProtection": [0]})
    return st


def test_selection_levels():
    st = make_state()
    c, t, b = select(st, counties=["24001"])
    assert t == {"24001000100", "24001000200"} and len(b) == 3
    c, t, b = select(st, tracts=["24003000100"])
    assert c == {"24003"} and len(b) == 2
    c, t, b = select(st, blocks=["240010001001001"])
    assert (c, t, b) == ({"24001"}, {"24001000100"}, {"240010001001001"})


def test_create_region_by_county():
    region = create_region(make_state(), counties=["24001"], year=2025)
    assert list(region["hzCensusBlock"]["CensusBlock"]) == ["240010001001000", "240010001001001", "240010002001000"]
    # copies take the region template's types: float exposure -> int (truncated), IncomeRatio -> REAL
    assert region["hzExposureOccupB"]["COM1I"].tolist() == [3989, 0, 0]
    assert region["hzExposureOccupB"]["RES1I"].tolist() == [1000, 500, 300]
    assert region["hzExposureOccupB"]["TotalExp"].tolist() == [4989, 500, 300]  # computed column
    assert region["hzCensusBlock"]["IncomeRatio"].iloc[0] == float(np.float32(0.9))
    assert region["hzCareFlty"]["Cost"].tolist() == [100.1235]  # money: 4 decimals
    # building schemes of the selected tracts only
    assert set(region["hzGenBldgScheme"]["BldgSchemesId"]) == {"MD1"}
    # post-processing: EntryDate 0 / NULL -> 2009; the coastal block's scheme gets a C
    sm = region["flSchemeMapping"].set_index("CensusBlock")
    assert sm.loc["240010001001001", "EntryDate"] == 2009 and sm.loc["240010002001000", "EntryDate"] == 2009
    assert sm.loc["240010001001001", "SchemeId"] == "MDC00001"
    assert sm.loc["240010001001000", "SchemeId"] == "MDR00001"
    # facilities by tract, flood attributes from the class defaults
    assert region["hzCareFlty"]["CareFltyId"].tolist() == ["MD000001"]
    fl = region["flCareFlty"].iloc[0]
    assert fl["FirstFloorHt"] == 3.0 and fl["BldgDamageFnId"] == "474"
    assert region["flPotableWaterFlty"].iloc[0]["UtilDamageFnId"] == "7"
    # boundary: the county, simplified and buffered by 0.00001 degrees
    bnd = region["hzBoundary"].geometry.iloc[0]
    assert bnd.contains(box(0, 0, 0.01, 0.01)) and bnd.area == pytest.approx(0.0001, rel=0.01)


def test_facility_class_without_default_keeps_state_values():
    region = create_region(make_state(), counties=["24003"], year=2025)
    fl = region["flCareFlty"].iloc[0]  # EFXX has no row in flCareFltyDflt: the trigger leaves it alone
    assert fl["FirstFloorHt"] == 9.0 and fl["BldgDamageFnId"] == "1"


def test_depreciation_depends_on_creation_year():
    from hazus import data

    dep = data.table("flDepFunction").set_index("Age")
    r25 = create_region(make_state(), counties=["24001"], year=2025)["flDepExposureOccupB"].set_index("CensusBlock")
    r26 = create_region(make_state(), counties=["24001"], year=2026)["flDepExposureOccupB"].set_index("CensusBlock")
    # block 1: built 1970, COM1 3989 (after truncation); income ratio 0.9 -> RES1 weights 0.75 Average, 0.25 Good
    com1 = (100 - dep.loc[55, "COM1"]) / 100 * 3989
    assert r25.loc["240010001001000", "COM1I"] == int(np.floor(com1 + 0.5))
    assert r26.loc["240010001001000", "COM1I"] != r25.loc["240010001001000", "COM1I"]
    res1 = (1 - (0.75 * dep.loc[55, "RES1Average"] + 0.25 * dep.loc[55, "RES1Good"]) / 100) * 1000
    assert r25.loc["240010001001000", "RES1I"] == int(np.floor(res1 + 0.5))
    # built 1850: 175 years old, beyond flDepFunction's 0-100 -> no depreciated row (as in Hazus)
    assert "240010002001000" not in r25.index
    content = create_region(make_state(), counties=["24001"], year=2025)["flDepExposureContentOccupB"]
    pct = data.table("hzPctContentOfStructureValue")
    ratio = int(pct.set_index(pct["Occupancy"].str.strip()).loc["COM1", "ContentValPct"])
    assert content.set_index("CensusBlock").loc["240010001001000", "COM1I"] == r25.loc["240010001001000", "COM1I"] * ratio // 100


def test_to_region_types_matches_columns_case_insensitively():
    df = pd.DataFrame({"schemeid": ["MDR00001"], "SOCCUP": ["RES1"], "FoundationType": ["4"], "PreFirmDist": [50.4],
                       "OBJECTID": [7]})
    out = to_region_types("flSchemeRiverine", df)
    assert "OBJECTID" not in out.columns and {"SchemeId", "SOccup", "FoundationType"} <= set(out.columns)
