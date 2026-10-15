"""Synthetic Hazus study regions for differential tests.

Each table uses exact Hazus table and column names, so the same frames can be loaded into the Hazus SQL
database and passed to the Python port.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hazus.data import table

BUILT_COLS = ["BuiltBefore40", "Built40to49", "Built50to59", "Built60to69", "Built70to79", "Built80to89",
              "Built90to99", "Built00to09", "Built10to13", "BuiltAfter13"]
GEN_OF = {"RES": "RESI", "COM": "COMI", "IND": "INDI", "AGR": "AGRI", "REL": "RELI", "GOV": "GOVI", "EDU": "EDUI"}
BIN_COLS = ["ft00"] + [f"ft{b}" for b in range(50, 2401, 50)]


def occupancies() -> list[str]:
    cls = table("ClSOccupancy").sort_values("SortOrder")
    return [s.strip() for s in cls["SOccup"]]


def _occ_table(blocks, occs, values, suffix, total_col):
    df = pd.DataFrame({"CensusBlock": blocks})
    for i, o in enumerate(occs):
        df[f"{o}{suffix}"] = values[:, i]
    for gen, col in GEN_OF.items():
        cols = [f"{o}{suffix}" for o in occs if o.startswith(gen)]
        df[col[:-1] + suffix] = df[cols].sum(axis=1)
    df[total_col] = df[[f"{o}{suffix}" for o in occs]].sum(axis=1)
    return df


def make_region(seed: int = 0, n_blocks: int = 8, state_fips: str = "24", state_abbr: str = "MD",
                coastal_share: float = 0.5, lake_share: float = 0.0) -> dict[str, pd.DataFrame]:
    """A random study region. ``lake_share`` of the blocks become Great Lakes blocks (type L, scheme
    ``<ST>L00001``); use a state with Great Lakes schemes in ``flGBSDist*``, e.g. NY (36)."""
    rng = np.random.default_rng(seed)
    occs = occupancies()
    county = f"{state_fips}001"
    tract = f"{county}000100"
    blocks = [f"{tract}{1000 + i:04d}" for i in range(n_blocks)]
    btype = np.where(rng.random(n_blocks) < coastal_share, "C", "R")
    if lake_share:  # separate generator: regions without lake blocks stay exactly as before
        btype[np.random.default_rng(seed + 77).random(n_blocks) < lake_share] = "L"

    t = {}
    t["hzTract"] = pd.DataFrame({"Tract": [tract], "CountyFips": [county], "Tract6": [tract[5:]], "TractArea": [1.0]})
    t["hzCensusBlock"] = pd.DataFrame({
        "CensusBlock": blocks, "Tract": tract, "BldgSchemesId": [f"{state_abbr}1"] * n_blocks,
        "BlockType": btype, "BlockArea": rng.uniform(0.01, 0.5, n_blocks).round(4)})

    built = rng.integers(0, 40, size=(n_blocks, len(BUILT_COLS)))
    built[rng.random(n_blocks) < 0.1] = 0  # some blocks with no buildings
    demo = pd.DataFrame(built, columns=BUILT_COLS)
    demo.insert(0, "CensusBlock", blocks)
    demo["MedianYearBuilt"] = rng.integers(1935, 2015, n_blocks)
    demo["Population"] = rng.integers(0, 2000, n_blocks)
    demo["Households"] = rng.integers(0, 800, n_blocks)
    for c in ["IncLess10", "Inc10to20", "Inc20to30", "Inc30to40", "Inc40to50", "Inc50to60", "Inc60to75",
              "Inc75to100", "IncOver100", "MaleLess18", "Male18to64", "MaleOver64", "FemaleLess18",
              "Female18to64", "FemaleOver64"]:
        demo[c] = rng.integers(0, 200, n_blocks)
    t["hzDemographicsB"] = demo

    schemes = [f"{state_abbr}{b}00001" for b in btype]
    entry = rng.integers(1960, 2020, n_blocks)
    ud = np.where(rng.random(n_blocks) < 0.2, rng.integers(0, 100, n_blocks), -1)
    t["flSchemeMapping"] = pd.DataFrame({"CensusBlock": blocks, "SchemeId": schemes, "EntryDate": entry,
                                         "UDPrePct": pd.array([v if v >= 0 else pd.NA for v in ud], dtype="Int64")})

    present = rng.random((n_blocks, len(occs))) < 0.6
    sqft = (rng.uniform(1, 400, (n_blocks, len(occs))) * present).round(1)
    exp = (sqft * rng.uniform(80, 200, (n_blocks, len(occs)))).astype(int)  # $ thousands
    cont = (exp * rng.uniform(0.5, 1.5, (n_blocks, len(occs)))).astype(int)
    cnt = np.where(present, rng.integers(1, 60, (n_blocks, len(occs))), 0)
    dep = (exp * rng.uniform(0.5, 0.9, (n_blocks, len(occs)))).astype(int)
    depc = (cont * rng.uniform(0.5, 0.9, (n_blocks, len(occs)))).astype(int)
    t["hzSqFootageOccupB"] = _occ_table(blocks, occs, sqft * 1000, "F", "TotalSqft")
    t["hzExposureOccupB"] = _occ_table(blocks, occs, exp, "I", "TotalExp")
    t["hzExposureContentOccupB"] = _occ_table(blocks, occs, cont, "I", "TotalExp")
    t["hzBldgCountOccupB"] = _occ_table(blocks, occs, cnt, "I", "TotalCount")
    t["flDepExposureOccupB"] = _occ_table(blocks, occs, dep, "I", "TotalExp")
    t["flDepExposureContentOccupB"] = _occ_table(blocks, occs, depc, "I", "TotalExp")

    found = ["Basement", "Crawl", "Fill", "Pier", "Pile", "Slab", "SolidWall"]

    def dist100(n):
        x = np.floor(rng.dirichlet(np.ones(len(found)), n) * 100).astype(int)
        x[:, 0] += 100 - x.sum(axis=1)
        return x

    riv, coast = [], []
    for o in occs:
        pre, post, post_v = dist100(1)[0], dist100(1)[0], dist100(1)[0]
        for k, f in enumerate(found):
            riv.append({"SchemeId": f"{state_abbr}R00001", "SOccup": o, "FoundationType": f, "PreFirmDist": pre[k],
                        "PreFirmHt": 2, "PostFirmDist": post[k], "PostFirmHt": 4, "HazardType": "R"})
            coast.append({"SchemeId": f"{state_abbr}C00001", "SOccup": o, "FoundationType": f, "PreFirmDist": pre[k],
                          "PreFirmHt": 2, "PostFirmDistAZone": post[k], "PostFirmHtAZone": 4,
                          "PostFirmDistVZone": post_v[k], "PostFirmHtVZone": 8, "HazardType": "C"})
    t["flSchemeRiverine"] = pd.DataFrame(riv)
    t["flSchemeCoastal"] = pd.DataFrame(coast)

    w = rng.dirichlet(np.ones(5), len(occs))
    pct = np.floor(w * 100).astype(int)
    pct[:, 0] += 100 - pct.sum(axis=1)
    t["hzGenBldgScheme"] = pd.DataFrame({
        "GenBldgSchemeId": [f"{state_abbr}1{o}" for o in occs], "BldgSchemesId": f"{state_abbr}1",
        "Occupancy": occs, "Total": 100, "WPct": pct[:, 0], "CPct": pct[:, 1], "SPct": pct[:, 2],
        "MPct": pct[:, 3], "HPct": pct[:, 4]})
    return t


def make_area_weighted(region: dict[str, pd.DataFrame], seed: int = 0, study_case_id: int = 1,
                       return_period: str = "-1", flood_share: float = 0.8, lake_hazard: str = "C") -> pd.DataFrame:
    """Random ``flAnAreaWeighted`` rows: per block, one zone (R) or 1-3 coastal zones, a few depth bins.

    Great Lakes blocks are flooded as ``lake_hazard`` ("C": coastal zones, or "R": riverine, as in the
    Hazus Loss Library's Lake Ontario analyses)."""
    rng = np.random.default_rng(seed + 1000)
    rows = []
    cb = region["hzCensusBlock"]
    for blk, bt in zip(cb["CensusBlock"], cb["BlockType"]):
        if rng.random() > flood_share:
            continue
        hazard = lake_hazard if bt == "L" else bt
        zones = ["R"] if hazard == "R" else list(rng.choice(["A", "CA", "CV"], size=rng.integers(1, 4), replace=False))
        budget = 100.0
        for z in zones:
            row = dict.fromkeys(BIN_COLS, 0.0)
            for col in rng.choice(BIN_COLS[:30], size=rng.integers(1, 5), replace=False):
                v = float(rng.uniform(0, budget / 3))
                row[col] += v
                budget -= v
            rows.append({"StudyCaseID": study_case_id, "CensusBlock": blk, "ReturnPeriodID": return_period,
                         "ControllingHazard": hazard, "CoastalZone": z, **row,
                         "Proc_Order": 0, "AnalysisOptId": 0})
    return pd.DataFrame(rows)


def write_grid(path, depth: np.ndarray, *, res: float = 0.0001, origin=(0.0, 0.0005)) -> None:
    """A float32 EPSG:4326 depth grid (ft) with NaN as NoData, top-left corner at ``origin``."""
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(path, "w", driver="GTiff", height=depth.shape[0], width=depth.shape[1], count=1,
                       dtype="float32", crs="EPSG:4326", transform=from_origin(*origin, res, res),
                       nodata=np.nan) as ds:
        ds.write(depth.astype("float32"), 1)


def make_scenario(tmp_path, seed: int = 4, n_blocks: int = 6, depths=None, hazard: str = "riverine"):
    """A region whose blocks are 0.0005-degree squares in a row along the equator, with one care facility in
    the first block, plus depth grids that flood the left half of the blocks.

    ``depths`` maps return period → uniform depth (ft) of the flooded half.
    """
    import geopandas as gpd
    from shapely.geometry import box

    from hazus.flood.hazard import FloodRaster

    depths = depths or {10: 1.0, 100: 3.0, 500: 6.0}
    region = make_region(seed=seed, n_blocks=n_blocks, coastal_share=0.0 if hazard == "riverine" else 1.0)
    side = 0.0005
    cb = region["hzCensusBlock"]
    region["hzCensusBlock"] = gpd.GeoDataFrame(
        cb, geometry=[box(i * side, 0.0, (i + 1) * side, side) for i in range(len(cb))], crs="EPSG:4326")
    region["hzCareFlty"] = pd.DataFrame({"CareFltyId": ["MD000001"], "EfClass": ["EFHS"], "Cost": [5000.0],
                                         "Longitude": [side / 2], "Latitude": [side / 2]})
    region["flCareFlty"] = pd.DataFrame({"CareFltyId": ["MD000001"], "FirstFloorHt": [0.5],
                                         "BldgDamageFnId": ["474"], "ContDamageFnId": ["309"],
                                         "FloodProtection": [0]})
    cells = int(round(side / 0.0001))
    width = cells * n_blocks
    rasters = []
    for rp, d in depths.items():
        grid = np.full((cells, width), np.nan)
        grid[:, : width // 2] = d
        path = tmp_path / f"depth_{rp}.tif"
        write_grid(path, grid)
        rasters.append(FloodRaster(path, hazard, rp))
    return region, rasters
