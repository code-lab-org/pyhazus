"""Vectorized code paths against the per-row code they replaced.

Each reference below is the earlier implementation, kept here as an oracle. The fast paths must give the same
result exactly (values, dtypes and row order) on inputs chosen to hit the edge cases: duplicate rows, missing
return periods, half-way rounding, int64 overflow, NoData, misaligned and partial grids, study boundaries that
cut through tiles, points outside every grid, ambiguous or missing damage-function defaults.
"""

import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hazus import data
from hazus.flood.aal import ECON_COLS, aal_by_soccup, aal_coefficients, aal_coefficients_decimal
from hazus.flood.facility_analysis import (
    ESSENTIAL_FACILITIES,
    UTILITIES,
    FacilityDamageFunctions,
    _strip,
    run_facilities,
)
from hazus.flood.gbs import _occ_column, _occ_positions, _occupancies

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

PART_KEYS = ["StudyCaseId", "CensusBlock", "ControlHazard", "SOccup", "Pre_PostFirm", "AnalysisOptId"]


# --- AAL (absp_AALDoIt) ---

def aal_reference(ecloss: pd.DataFrame, coefficients) -> pd.DataFrame:
    def round_half_away(x: Fraction) -> int:
        r = int(abs(x) + Fraction(1, 2))
        return r if x >= 0 else -r

    df = ecloss.copy()
    rp = pd.to_numeric(df["ReturnPeriodId"].astype(str).str.strip(), errors="coerce")
    df = df[rp.isin(list(coefficients))].assign(ReturnPeriod=rp.astype("Int64"))
    df = df.groupby(PART_KEYS + ["ReturnPeriod"], as_index=False)[ECON_COLS].sum()
    coef = {int(k): Fraction(v) for k, v in coefficients.items()}
    rows = []
    for key, g in df.sort_values("ReturnPeriod", ascending=False).groupby(PART_KEYS, sort=False):
        totals = dict.fromkeys(ECON_COLS, Fraction(0))
        prev = None
        for r in g.itertuples(index=False):
            c = coef[int(r.ReturnPeriod)]
            for col in ECON_COLS:
                v = int(getattr(r, col))
                totals[col] += c * v if prev is None else c * (v + int(getattr(prev, col))) / 2
            prev = r
        rows.append({**dict(zip(PART_KEYS, key)), "ReturnPeriodId": "Annual",
                     **{col: round_half_away(totals[col]) for col in ECON_COLS}})
    return pd.DataFrame(rows, columns=PART_KEYS + ["ReturnPeriodId"] + ECON_COLS)


def ecloss_rows(seed: int, periods, n: int = 2000, scale: int = 1) -> pd.DataFrame:
    """Random flFRGBSEcLossBySOccup rows: duplicates, unknown and padded return periods, negative values."""
    rng = np.random.default_rng(seed)
    labels = [str(p) for p in periods] + [f" {periods[0]} ", "Annual", "999"]
    df = pd.DataFrame({
        "StudyCaseId": 1, "ReturnPeriodId": rng.choice(labels, n),
        "CensusBlock": [f"240010001001{i:03d}" for i in rng.integers(0, 80, n)],
        "SOccup": rng.choice(["RES1", "COM1", "IND2"], n), "ControlHazard": rng.choice(["R", "C"], n),
        "Pre_PostFirm": rng.integers(0, 2, n), "AnalysisOptId": 0})
    for col in ECON_COLS:
        df[col] = rng.integers(-500, 20000, n) * scale
    df.loc[:9, "BuildingLoss"] = 250  # 500-year coefficient 0.002 x 250 = 0.5: exactly half way
    return df


@pytest.mark.parametrize("periods, coefficients, scale", [
    ((10, 25, 50, 100, 500), "decimal", 1),
    ((7, 13, 333), "decimal", 1),
    ((2, 5, 10, 25, 50, 100, 200, 500, 1000), "decimal", 1),
    ((10, 100, 500), "float", 1),  # binary fractions: Python integers, not int64
    ((10, 100, 500), "decimal", 10 ** 12),  # int64 would overflow
])
def test_aal_matches_fraction_loop(periods, coefficients, scale):
    coef = aal_coefficients_decimal(periods) if coefficients == "decimal" else aal_coefficients(periods)
    ecloss = ecloss_rows(len(periods), periods, scale=scale)
    expected = aal_reference(ecloss, coef)
    pd.testing.assert_frame_equal(aal_by_soccup(ecloss, coef), expected, check_exact=True)
    assert len(expected) > 100


def test_aal_without_matching_return_periods():
    ecloss = ecloss_rows(0, (10, 100, 500))
    coef = aal_coefficients_decimal((20, 200, 2000))
    pd.testing.assert_frame_equal(aal_by_soccup(ecloss, coef), aal_reference(ecloss, coef), check_exact=True)


# --- GBS per-block x occupancy lookups ---

def occ_column_reference(region, name, suffix, blocks: pd.Series, occ: pd.Series) -> np.ndarray:
    t = region[name].copy()
    t["CensusBlock"] = t["CensusBlock"].astype(str).str.rstrip()
    long = t.melt(id_vars="CensusBlock", var_name="col", value_name="v")
    key = pd.DataFrame({"CensusBlock": blocks.to_numpy(), "col": (occ + suffix).to_numpy()})
    return key.merge(long, on=["CensusBlock", "col"], how="left")["v"].to_numpy(dtype="float64")


@pytest.mark.parametrize("name, suffix", [("hzExposureOccupB", "I"), ("hzSqFootageOccupB", "F")])
def test_occ_column_matches_melt_merge(name, suffix):
    rng = np.random.default_rng(3)
    region = synth.make_region(seed=3, n_blocks=12)
    t = region[name].drop(columns=f"RES2{suffix}").astype({f"COM1{suffix}": "float64"})
    t.loc[2, f"COM1{suffix}"] = np.nan
    t["CensusBlock"] = t["CensusBlock"] + "  "  # padded, as char(15) can be
    region[name] = t
    blocks = pd.Series(list(rng.choice(list(t["CensusBlock"].str.strip()) + ["UNKNOWN"], 500)))
    occid = rng.choice(list(_occupancies()["SortOrder"]) + [999], 500)
    occs, occ_pos = _occ_positions(occid)
    codes, uniques = pd.factorize(blocks)
    got = _occ_column(region, name, suffix, occs, uniques, codes, occ_pos)
    soccup = pd.Series(occid).map(_occupancies().set_index("SortOrder")["SOccup"])
    expected = occ_column_reference(region, name, suffix, blocks, soccup)
    np.testing.assert_array_equal(got, expected)
    assert np.isnan(expected).any() and not np.isnan(expected).all()


# --- area weighting (Analysis Area Weighted) ---

def area_weighted_reference(blocks, flood, *, block_id="CensusBlock", boundary=None, tile=2048):
    import rasterio
    from rasterio.features import rasterize

    from hazus.flood.exposure import CELL_SIZE, ZONE_LETTERS, _box, depth_bin, snapped_grid, zone_code
    from hazus.flood.hazard import HazardType

    blocks = blocks.reset_index(drop=True)
    hazard = HazardType(flood.hazard_type)
    sindex = blocks.sindex
    with rasterio.open(flood.path) as ds:
        grid = snapped_grid(tuple(blocks.total_bounds), ds.transform, CELL_SIZE)
        dl, dt = ds.transform.c, ds.transform.f
        drx, dry = abs(ds.transform.a), abs(ds.transform.e)
        nodata = ds.nodata
        r0, r1, c0, c1 = grid.window_for_bounds(ds.bounds.left, ds.bounds.bottom, ds.bounds.right, ds.bounds.top)
        parts = []
        for tr in range(r0, r1, tile):
            for tc in range(c0, c1, tile):
                h, w = min(tile, r1 - tr), min(tile, c1 - tc)
                tx0, ty0 = grid.left + tc * grid.cell, grid.top - tr * grid.cell
                hits = sindex.query(_box(tx0, ty0 - h * grid.cell, tx0 + w * grid.cell, ty0))
                if len(hits) == 0:
                    continue
                transform = grid.sub_transform(tr, tc)
                blk = rasterize(((blocks.geometry.values[i], int(i) + 1) for i in hits), out_shape=(h, w),
                                transform=transform, fill=0, dtype="int32")
                if not blk.any():
                    continue
                if boundary is not None:
                    inside = rasterize([(boundary, 1)], out_shape=(h, w), transform=transform, fill=0,
                                       dtype="uint8").astype(bool)
                else:
                    inside = blk > 0
                xs = tx0 + (np.arange(w) + 0.5) * grid.cell
                ys = ty0 - (np.arange(h) + 0.5) * grid.cell
                dcols = np.floor((xs - dl) / drx).astype("int64")
                drows = np.floor((dt - ys) / dry).astype("int64")
                col_ok = (dcols >= 0) & (dcols < ds.width)
                row_ok = (drows >= 0) & (drows < ds.height)
                if not col_ok.any() or not row_ok.any():
                    continue
                wc0, wc1 = dcols[col_ok].min(), dcols[col_ok].max() + 1
                wr0, wr1 = drows[row_ok].min(), drows[row_ok].max() + 1
                band = ds.read(1, window=((wr0, wr1), (wc0, wc1))).astype("float64")
                if nodata is not None and not np.isnan(nodata):
                    band[band == nodata] = np.nan
                depth = np.full((h, w), np.nan)
                full = band[np.ix_(np.clip(drows - wr0, 0, wr1 - wr0 - 1), np.clip(dcols - wc0, 0, wc1 - wc0 - 1))]
                ok2d = np.outer(row_ok, col_ok)
                depth[ok2d] = full[ok2d]
                bins = depth_bin(depth)
                valid = (blk > 0) & inside & (bins >= 0)
                if not valid.any():
                    continue
                zones = zone_code(depth[valid], hazard)
                key = (blk[valid].astype("int64") << 16) | (zones.astype("int64") << 8) | (bins[valid] // 50)
                uniq, counts = np.unique(key, return_counts=True)
                parts.append(pd.DataFrame({"key": uniq, "Count": counts}))
    if not parts:
        return pd.DataFrame(columns=["CensusBlock", "CoastalZone", "ReClass", "Count", "TotalCount", "PctAffected"])
    agg = pd.concat(parts).groupby("key", as_index=False)["Count"].sum()
    keys = agg["key"].to_numpy(dtype="int64")
    idx = (keys >> 16) - 1
    flooded = np.unique(idx)
    totals = np.zeros(len(flooded), dtype="int64")
    for k, i in enumerate(flooded):
        geom = blocks.geometry.values[i]
        b0, b1, a0, a1 = grid.window_for_bounds(*geom.bounds)
        if b1 > b0 and a1 > a0:
            totals[k] = int(rasterize([(geom, 1)], out_shape=(b1 - b0, a1 - a0), transform=grid.sub_transform(b0, a0),
                                      fill=0, dtype="uint8").sum())
    out = pd.DataFrame({
        "CensusBlock": blocks[block_id].to_numpy()[idx],
        "CoastalZone": [ZONE_LETTERS.get(int(z), "N") for z in (keys >> 8) & 0xFF],
        "ReClass": ((keys & 0xFF) * 50).astype("int32"),
        "Count": agg["Count"].to_numpy(),
        "TotalCount": pd.Series(totals, index=flooded).loc[idx].to_numpy(),
    })
    out["PctAffected"] = out["Count"] / out["TotalCount"] * 100.0
    return out.sort_values(["CensusBlock", "CoastalZone", "ReClass"], ignore_index=True)


def jittered_blocks(n_side: int = 10, side: float = 0.001, seed: int = 3):
    """Census blocks that tile the region: quadrilaterals on a jittered lattice."""
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Polygon

    rng = np.random.default_rng(seed)
    gx = -77.0 + np.arange(n_side + 1)[None, :] * side + rng.uniform(-0.3, 0.3, (n_side + 1, n_side + 1)) * side
    gy = 38.8 + np.arange(n_side + 1)[:, None] * side + rng.uniform(-0.3, 0.3, (n_side + 1, n_side + 1)) * side
    polys = [Polygon([(gx[r, c], gy[r, c]), (gx[r, c + 1], gy[r, c + 1]), (gx[r + 1, c + 1], gy[r + 1, c + 1]),
                      (gx[r + 1, c], gy[r + 1, c])]) for r in range(n_side) for c in range(n_side)]
    return gpd.GeoDataFrame({"CensusBlock": [f"B{i:03d}" for i in range(len(polys))]}, geometry=polys, crs="EPSG:4326")


def depth_grid(path, bounds, res, seed, *, nodata=np.nan, scale=1.0, offset=(0.0, 0.0), shape=None):
    """Depth (ft) falling from west to east with noise; <= 0 and 2 % of cells at random are NoData."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    left, top = bounds[0] + offset[0], bounds[3] + offset[1]
    h, w = shape or (int(np.ceil((bounds[3] - bounds[1]) / res)), int(np.ceil((bounds[2] - bounds[0]) / res)))
    rng = np.random.default_rng(seed)
    d = (np.linspace(12, -3, w)[None, :] + rng.normal(0, 1.5, (h, w))) * scale
    d[(d <= 0) | (rng.random((h, w)) < 0.02)] = nodata
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32", crs="EPSG:4326",
                       transform=from_origin(left, top, res, res), nodata=nodata, tiled=True, blockxsize=256,
                       blockysize=256) as ds:
        ds.write(d.astype("float32"), 1)
    return path


AW_CASES = {  # name: (grid options, hazard, boundary, tile)
    "coarse": (dict(res=0.0001, seed=1), "riverine", None, 257),
    "finer_than_block_grid_misaligned": (dict(res=0.0000073, seed=2, offset=(0.0003, -0.0002)), "coastal", None, 2048),
    "nodata_value_whole_boundary": (dict(res=0.000031, seed=4, nodata=-9999.0, scale=1.7), "coastal", "whole", 300),
    "partial_boundary": (dict(res=0.0000413, seed=5, offset=(-0.001, 0.0011)), "riverine", "partial", 300),
    "grid_inside_region": (dict(res=0.00005, seed=6, offset=(0.004, -0.004), shape=(60, 70)), "coastal", None, 517),
}


@pytest.mark.parametrize("case", list(AW_CASES))
def test_area_weighting_matches_reference(case, tmp_path):
    from shapely.geometry import box

    from hazus.flood.exposure import area_weighted_depths
    from hazus.flood.hazard import FloodRaster

    opts, hazard, boundary, tile = AW_CASES[case]
    blocks = jittered_blocks()
    bounds = tuple(blocks.total_bounds)
    union = blocks.geometry.union_all()
    boundary = {None: None, "whole": union.buffer(0.00001),
                "partial": union.intersection(box(bounds[0], bounds[1], (bounds[0] + bounds[2]) / 2 + 0.0007,
                                                  bounds[3])).buffer(0.00001)}[boundary]
    flood = FloodRaster(depth_grid(tmp_path / "d.tif", bounds, **opts), hazard, 100)
    expected = area_weighted_reference(blocks, flood, boundary=boundary, tile=tile)
    got = area_weighted_depths(blocks, flood, boundary=boundary, tile=tile)
    pd.testing.assert_frame_equal(got, expected, check_exact=True)
    assert len(expected) > 20


def test_area_weighting_shares_block_counts(tmp_path):
    from hazus.flood.exposure import area_weighted_depths
    from hazus.flood.hazard import FloodRaster

    blocks = jittered_blocks()
    bounds = tuple(blocks.total_bounds)
    counts: dict = {}
    for k, res in enumerate([0.0001, 0.0001, 0.00003]):  # the last grid snaps the block grid differently
        flood = FloodRaster(depth_grid(tmp_path / f"d{k}.tif", bounds, res=res, seed=k), "riverine", 100)
        shared = area_weighted_depths(blocks, flood, block_counts=counts)
        pd.testing.assert_frame_equal(shared, area_weighted_depths(blocks, flood), check_exact=True)
    assert len(counts) == 2


# --- facilities ---

def default_id_reference(xref, id_col, hazard, depth, occupancy):
    flag = "HazardR" if hazard == "R" else "HazardCV" if hazard == "C" and depth > 6 else "HazardCA"
    rows = xref[(xref["SOccupId"].map(_strip) == _strip(occupancy)) & xref[flag].astype(bool)
                & (xref["Selected"].astype(bool) | xref["DefaultFn"].astype(bool))]
    if len(rows) > 1:
        raise LookupError
    return int(rows[id_col].iloc[0]) if len(rows) else None


def outcome(fn, *args):
    try:
        return fn(*args)
    except LookupError:
        return LookupError


XREFS = {"struct": ("flEssntFltyStructDmgXRef", "BldgDmgFnId"), "cont": ("flEssntFltyContDmgXRef", "ContDmgFnId"),
         "util": ("flUtilFltyDmgXRef", "UtilFltyDmgFnId")}


def test_default_ids_match_table_filter():
    custom = pd.DataFrame({"SOccupId": ["PWTL", "PWTL ", "PWTL", "WWTL"], "UtilFltyDmgFnId": [7, 10, 11, 12],
                           "DefaultFn": [1, 0, 0, 0], "Selected": [0, 1, 0, 1], "HazardR": [1, 0, 0, 1],
                           "HazardCA": [0, 1, np.nan, 0], "HazardCV": [0, 0, 1, 1]})
    for tables in ({}, {"flUtilFltyDmgXRef": custom}):
        f = FacilityDamageFunctions(tables)
        for kind, (name, id_col) in XREFS.items():
            xref = tables.get(name, data.table(name))
            for occ in [*set(xref["SOccupId"]), " PWTL", "NOPE"]:
                for hazard, depth in [("R", 1.0), ("C", 6.0), ("C", 6.5)]:
                    assert outcome(f.default_id, kind, hazard, depth, occ) == \
                        outcome(default_id_reference, xref, id_col, hazard, depth, occ), (kind, occ, hazard, depth)


def test_restoration_matches_table_filter():
    rs = data.table("flRsFnEssntFlty").assign(EfClass=lambda t: t["EfClass"].map(_strip))
    f = FacilityDamageFunctions()

    def reference(ef_class, d):
        rows = rs[(rs["EfClass"] == _strip(ef_class)) & (rs["Min_Depth"] <= d) & (rs["Max_Depth"] > d)]
        if rows.empty:
            return None
        r = rows.iloc[0]
        return {"MaxDays": None if pd.isna(r["Max_Restor_Days"]) else int(r["Max_Restor_Days"]),
                "FunctionalDepth": None if pd.isna(r["FunctionalDepth"]) else float(np.float32(r["FunctionalDepth"]))}

    depths = [*np.arange(-6.0, 30.0, 0.25), -4, 0, 1, 4, 8, 12, 24, np.nan]
    for ef_class in [*set(rs["EfClass"]), " EFHS ", "NOPE"]:
        for d in depths:
            assert f.restoration(ef_class, d) == reference(ef_class, d), (ef_class, d)


def max_depth_reference(rasters, xs, ys):
    import rasterio

    xs, ys = np.asarray(xs, dtype="float64"), np.asarray(ys, dtype="float64")
    depth = np.zeros(len(xs))
    hazard = np.full(len(xs), "R", dtype=object)
    for fr in rasters:
        with rasterio.open(fr.path) as ds:
            rows, cols = rasterio.transform.rowcol(ds.transform, xs, ys, op=np.floor)
            rows, cols = np.asarray(rows).astype("int64"), np.asarray(cols).astype("int64")
            inside = (rows >= 0) & (cols >= 0) & (rows < ds.height) & (cols < ds.width)
            values = np.full(len(xs), np.nan)
            for k in np.flatnonzero(inside):
                cell = ds.read(1, window=((rows[k], rows[k] + 1), (cols[k], cols[k] + 1)), masked=True)
                values[k] = np.ma.filled(cell.astype("float64"), np.nan)[0, 0]
        higher = np.nan_to_num(values, nan=-np.inf) > depth
        depth[higher] = values[higher]
        hazard[higher] = fr.hazard_type.controlling_code
    return depth, hazard


@pytest.fixture
def facility_grids(tmp_path):
    from hazus.flood.hazard import FloodRaster

    res = 0.00001  # 1300 x 700 cells: several 512-cell read windows
    bounds = (-77.0, 38.8, -77.0 + 1300 * res, 38.8 + 700 * res)
    river = depth_grid(tmp_path / "r.tif", bounds, res, seed=7, nodata=-9999.0)
    coast = depth_grid(tmp_path / "c.tif", bounds, res * 1.3, seed=8, offset=(0.004, 0.0), scale=1.4)
    return bounds, res, [FloodRaster(river, "riverine", 100), FloodRaster(coast, "coastal", 100)]


def test_point_depths_match_cell_reads(facility_grids):
    from hazus.flood.hazard import max_depth_at_points

    (left, bottom, right, top), res, rasters = facility_grids
    rng = np.random.default_rng(9)
    xs = np.r_[rng.uniform(left - 0.002, right + 0.006, 3000), left + res * np.arange(0, 1300, 97)]
    ys = np.r_[rng.uniform(bottom - 0.002, top + 0.002, 3000), np.full(14, top - 3 * res)]  # cell edges too
    depth, hazard = max_depth_at_points(rasters, xs, ys)
    ref_depth, ref_hazard = max_depth_reference(rasters, xs, ys)
    np.testing.assert_array_equal(depth, ref_depth)
    np.testing.assert_array_equal(hazard, ref_hazard)
    assert set(hazard) == {"R", "C"} and (depth == 0).any() and (depth > 0).any()


def test_facility_kinds_sampled_together_match_each_alone(facility_grids):
    (left, bottom, right, top), _, rasters = facility_grids
    rng = np.random.default_rng(10)
    care, school, water = ESSENTIAL_FACILITIES["care"], ESSENTIAL_FACILITIES["school"], UTILITIES["potable_water"]
    region = {}
    for kind, n in ((care, 40), (school, 0), (water, 30)):
        ids = [f"{kind.id_col[:2]}{i:04d}" for i in range(n)]
        cls = {"UtilFcltyClass": "PWTL"} if kind is water else {"EfClass": rng.choice(["EFHS", "EFS1"], n)}
        region[kind.base] = pd.DataFrame({kind.id_col: ids, **cls, "Cost": 1000.0,
                                          "Longitude": rng.uniform(left, right, n),
                                          "Latitude": rng.uniform(bottom, top, n)})
    functions = FacilityDamageFunctions()
    together = run_facilities(region, rasters, functions=functions)
    for kind in (care, school, water):
        alone = run_facilities({kind.base: region[kind.base]}, rasters, functions=functions)
        pd.testing.assert_frame_equal(together[kind.result], alone[kind.result], check_exact=True)


# --- depth-grid preparation ---

def test_prepare_depth_grid_does_not_depend_on_threads(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from hazus.flood.hazard import prepare_depth_grid

    rng = np.random.default_rng(11)
    d = (np.linspace(3, -1, 400)[None, :] + rng.normal(0, 0.3, (300, 400))).astype("float32")
    d[d <= 0] = -9999.0
    src = tmp_path / "utm.tif"
    with rasterio.open(src, "w", driver="GTiff", height=300, width=400, count=1, dtype="float32", crs="EPSG:26918",
                       transform=from_origin(320000, 4300000, 1.0, 1.0), nodata=-9999.0) as ds:
        ds.write(d, 1)
    grids = [prepare_depth_grid(src, tmp_path / f"out{t}.tif", "meters", block=128, threads=t) for t in (1, None)]
    with rasterio.open(grids[0]) as a, rasterio.open(grids[1]) as b:
        assert a.transform == b.transform
        np.testing.assert_array_equal(a.read(1), b.read(1))
