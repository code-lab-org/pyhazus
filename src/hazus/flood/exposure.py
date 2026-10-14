"""Area-weighted depth distribution per census block (Hazus ``Analysis Area Weighted`` + ``absp_GBSInsertAW``).

The Hazus 7.2 ArcPy tool does the following:

1. Rasterizes the study region's census blocks at 0.00001 degree, snapped to the depth grid.
2. Resamples the depth grid onto that block grid (nearest neighbour), masked to the study boundary.
3. Reclassifies depth into half-foot bins coded in hundredths of a foot (0, 50, ..., 2400).
4. Assigns a zone: 1 for riverine; for coastal grids 2/3/4 for depth [0,3), [3,6), >= 6 ft.
5. Counts cells per (block, bin, zone).

``absp_GBSInsertAW`` then turns the counts into ``PctAffected = count / block_cells * 100`` and pivots
them to the ``ft00 … ft2400`` columns of ``flAnAreaWeighted``.

This module does the same with rasterio, processing the grid tile by tile.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hazus.flood.hazard import FloodRaster, HazardType

CELL_SIZE = 0.00001  # degrees, as hard-coded in analysis_area_weighted.py
BIN_CODES = np.arange(0, 2401, 50)  # 0, 50, ..., 2400 (hundredths of a foot)
ZONE_LETTERS = {1: "R", 2: "A", 3: "CA", 4: "CV"}  # absp_GBSInsertAW mapping


def depth_bin(depth_ft: np.ndarray) -> np.ndarray:
    """Hazus half-foot depth bin (hundredths of a foot), or -1 where the cell is not counted.

    ArcGIS ``RemapRange`` gives a shared boundary to the lower range, so the Hazus remap
    ``[-1,0]→NoData, (0,0.25]→0, (0.25,0.75]→50, …, (23.75,10000]→2400`` becomes
    ``50 * clip(ceil((d - 0.25) / 0.5), 0, 48)`` for ``d > 0``. Depths ``<= 0`` (and NaN) are not counted.
    """
    d = np.asarray(depth_ft, dtype="float64")
    k = np.ceil((d - 0.25) / 0.5)
    k = np.clip(np.nan_to_num(k, nan=0.0), 0, 48)
    out = (k * 50).astype("int32")
    out[~(d > 0)] = -1
    return out


def zone_code(depth_ft: np.ndarray, hazard_type: HazardType) -> np.ndarray:
    """1 riverine; coastal depth-limited zones 2 (< 3 ft), 3 (3–6 ft), 4 (>= 6 ft)."""
    d = np.asarray(depth_ft, dtype="float64")
    if HazardType(hazard_type) is HazardType.RIVERINE:
        return np.ones(d.shape, dtype="int8")
    return np.where(d < 3, 2, np.where(d < 6, 3, 4)).astype("int8")


@dataclass(frozen=True)
class Grid:
    """North-up grid: ``left``/``top`` corner, square ``cell`` size, ``width`` x ``height`` cells."""

    left: float
    top: float
    cell: float
    width: int
    height: int

    @property
    def transform(self):
        from rasterio.transform import Affine

        return Affine(self.cell, 0.0, self.left, 0.0, -self.cell, self.top)

    def window_for_bounds(self, minx: float, miny: float, maxx: float, maxy: float) -> tuple[int, int, int, int]:
        """Row/col window (row0, row1, col0, col1) covering the bounds, clipped to the grid."""
        c0 = max(0, int(math.floor((minx - self.left) / self.cell)))
        c1 = min(self.width, int(math.ceil((maxx - self.left) / self.cell)))
        r0 = max(0, int(math.floor((self.top - maxy) / self.cell)))
        r1 = min(self.height, int(math.ceil((self.top - miny) / self.cell)))
        return r0, r1, c0, c1

    def sub_transform(self, row0: int, col0: int):
        from rasterio.transform import Affine

        return Affine(self.cell, 0.0, self.left + col0 * self.cell, 0.0, -self.cell, self.top - row0 * self.cell)


def snapped_grid(bounds: tuple[float, float, float, float], snap_transform, cell: float = CELL_SIZE) -> Grid:
    """Grid covering ``bounds`` whose lower-left corner is snapped to a cell corner of the snap raster."""
    minx, miny, maxx, maxy = bounds
    sx, sy = snap_transform.c, snap_transform.f
    rx, ry = abs(snap_transform.a), abs(snap_transform.e)
    ll_x = sx + round((minx - sx) / rx) * rx
    ll_y = sy - round((sy - miny) / ry) * ry
    width = max(1, int(math.ceil((maxx - ll_x) / cell - 1e-9)))
    height = max(1, int(math.ceil((maxy - ll_y) / cell - 1e-9)))
    return Grid(left=ll_x, top=ll_y + height * cell, cell=cell, width=width, height=height)


def _block_counts(blocks, grid: Grid, block_index: np.ndarray) -> np.ndarray:
    """Total cell count of each block (the ``BLKGRID`` value table), rasterizing each block on its own window."""
    from rasterio.features import rasterize

    counts = np.zeros(len(block_index), dtype="int64")
    geoms = blocks.geometry.values
    for k, i in enumerate(block_index):
        geom = geoms[i]
        r0, r1, c0, c1 = grid.window_for_bounds(*geom.bounds)
        if r1 <= r0 or c1 <= c0:
            continue
        burned = rasterize([(geom, 1)], out_shape=(r1 - r0, c1 - c0), transform=grid.sub_transform(r0, c0),
                           fill=0, dtype="uint8")
        counts[k] = int(burned.sum())
    return counts


def area_weighted_depths(blocks, flood: FloodRaster, *, block_id: str = "CensusBlock", boundary=None,
                         cell: float = CELL_SIZE, tile: int = 2048) -> pd.DataFrame:
    """Percent of each census block's cells in each (zone, half-foot depth bin).

    Parameters
    ----------
    blocks:
        GeoDataFrame of the study region's census blocks in EPSG:4326 with a ``block_id`` column
        (Hazus ``hzCensusBlock``).
    flood:
        Depth grid in feet, EPSG:4326 (see :func:`hazus.flood.hazard.prepare_depth_grid`).
    boundary:
        Optional study-region boundary geometry (``hzBoundary``). Defaults to the union of the blocks.

    Returns
    -------
    DataFrame with columns ``CensusBlock, CoastalZone, ReClass, Count, TotalCount, PctAffected``
    (the long form of ``flAnGBSCombineAWTemp``).
    """
    import rasterio
    from rasterio.features import rasterize

    blocks = blocks.reset_index(drop=True)
    if blocks.crs is not None and blocks.crs.to_epsg() != 4326:
        blocks = blocks.to_crs(4326)
    hazard = HazardType(flood.hazard_type)
    sindex = blocks.sindex

    with rasterio.open(flood.path) as ds:
        grid = snapped_grid(tuple(blocks.total_bounds), ds.transform, cell)
        dl, dt = ds.transform.c, ds.transform.f
        drx, dry = abs(ds.transform.a), abs(ds.transform.e)
        dbounds = ds.bounds
        nodata = ds.nodata

        # Only the part of the block grid that the depth grid covers can hold flooded cells.
        r0, r1, c0, c1 = grid.window_for_bounds(dbounds.left, dbounds.bottom, dbounds.right, dbounds.top)
        parts: list[pd.DataFrame] = []
        for tr in range(r0, r1, tile):
            for tc in range(c0, c1, tile):
                h, w = min(tile, r1 - tr), min(tile, c1 - tc)
                tx0 = grid.left + tc * grid.cell
                ty0 = grid.top - tr * grid.cell
                tbox = (tx0, ty0 - h * grid.cell, tx0 + w * grid.cell, ty0)
                hits = sindex.query(_box(*tbox))
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

                # Nearest-neighbour resample: depth cell containing each block-grid cell centre.
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
                rr = np.clip(drows - wr0, 0, wr1 - wr0 - 1)
                cc = np.clip(dcols - wc0, 0, wc1 - wc0 - 1)
                depth_full = band[np.ix_(rr, cc)]
                ok2d = np.outer(row_ok, col_ok)
                depth[ok2d] = depth_full[ok2d]

                bins = depth_bin(depth)
                valid = (blk > 0) & inside & (bins >= 0)
                if not valid.any():
                    continue
                d = depth[valid]
                zones = zone_code(d, hazard)
                key = (blk[valid].astype("int64") << 16) | (zones.astype("int64") << 8) | (bins[valid] // 50)
                uniq, counts = np.unique(key, return_counts=True)
                parts.append(pd.DataFrame({"key": uniq, "Count": counts}))

    if not parts:
        return pd.DataFrame(columns=["CensusBlock", "CoastalZone", "ReClass", "Count", "TotalCount", "PctAffected"])

    agg = pd.concat(parts).groupby("key", as_index=False)["Count"].sum()
    keys = agg["key"].to_numpy(dtype="int64")
    idx = (keys >> 16) - 1
    zone = (keys >> 8) & 0xFF
    reclass = (keys & 0xFF) * 50

    flooded = np.unique(idx)
    totals = pd.Series(_block_counts(blocks, grid, flooded), index=flooded)

    out = pd.DataFrame({
        "CensusBlock": blocks[block_id].to_numpy()[idx],
        "CoastalZone": [ZONE_LETTERS.get(int(z), "N") for z in zone],
        "ReClass": reclass.astype("int32"),
        "Count": agg["Count"].to_numpy(),
        "TotalCount": totals.loc[idx].to_numpy(),
    })
    out["PctAffected"] = out["Count"] / out["TotalCount"] * 100.0
    return out.sort_values(["CensusBlock", "CoastalZone", "ReClass"], ignore_index=True)


def bin_column(reclass: int) -> str:
    """``flAnAreaWeighted`` column name for a bin code: 0 → ``ft00``, 50 → ``ft50``, 2400 → ``ft2400``."""
    return "ft00" if reclass == 0 else f"ft{reclass}"


def to_area_weighted_table(long: pd.DataFrame) -> pd.DataFrame:
    """Pivot :func:`area_weighted_depths` output to the wide ``flAnAreaWeighted`` layout."""
    columns = [bin_column(int(b)) for b in BIN_CODES]
    if long.empty:
        return pd.DataFrame(columns=["CensusBlock", "CoastalZone", *columns])
    wide = long.assign(col=long["ReClass"].map(lambda b: bin_column(int(b)))).pivot_table(
        index=["CensusBlock", "CoastalZone"], columns="col", values="PctAffected", aggfunc="sum", fill_value=0.0)
    wide = wide.reindex(columns=columns, fill_value=0.0)
    wide.columns.name = None
    return wide.reset_index()


def area_weighted_rows(long: pd.DataFrame, *, study_case_id: int, return_period: str, controlling_hazard: str,
                       analysis_opt_id: int = 0) -> pd.DataFrame:
    """``flAnAreaWeighted`` rows that ``absp_GBSInsertAW`` writes for one depth grid.

    ``controlling_hazard`` is the grid's ``"R"`` or ``"C"``. ``return_period`` is ``"-1"`` for a single event.
    """
    wide = to_area_weighted_table(long[long["CensusBlock"].notna()])
    columns = [bin_column(int(b)) for b in BIN_CODES]
    return pd.DataFrame({
        "StudyCaseID": study_case_id, "CensusBlock": wide["CensusBlock"], "ReturnPeriodID": return_period,
        "ControllingHazard": controlling_hazard, "CoastalZone": wide["CoastalZone"],
        **{c: wide[c].astype("float64") for c in columns}, "Proc_Order": 0, "AnalysisOptId": analysis_opt_id,
    })


def _box(minx, miny, maxx, maxy):
    from shapely.geometry import box

    return box(minx, miny, maxx, maxy)
