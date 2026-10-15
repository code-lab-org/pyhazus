"""Flood hazard inputs: user-supplied depth grids.

Mirrors ``create_flood_scenario.py`` (Hazus 7.2 Python toolbox) and the point reads in
``FloodRasterExtensions.GetMaxValueAndControllingHazardAsync``:

* grids are used in EPSG:4326 (Hazus reprojects anything else with nearest-neighbour resampling);
* depths are in feet (metres are converted with ``* 5000 / 1524``);
* several single-event grids of the same hazard type are merged with a cell-wise maximum;
* a point's depth is the maximum over the grids of a return period, starting from 0.0 / "R".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

FEET_PER_METER = 5000.0 / 1524.0
WGS84 = "EPSG:4326"


class HazardType(str, Enum):
    RIVERINE = "riverine"
    COASTAL = "coastal"

    @property
    def controlling_code(self) -> str:
        """Single-letter code stored by Hazus in ``ControllingHazard`` columns."""
        return "R" if self is HazardType.RIVERINE else "C"


@dataclass(frozen=True)
class FloodRaster:
    """A depth grid in feet and EPSG:4326, as produced by :func:`prepare_depth_grid`."""

    path: Path
    hazard_type: HazardType
    return_period: int = -1  # -1 for a single-event scenario, as in Hazus

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "hazard_type", HazardType(self.hazard_type))


def return_period_ids(rasters: Sequence[FloodRaster]) -> dict[int, str]:
    """``ReturnPeriodId`` for each return period of a scenario, as ``AnalysisLogic.RunAsync`` names them.

    With several return periods each is named after itself. With one, the id is ``"mix0"`` if the grids mix
    riverine and coastal, else the period (``"-1"`` for a single event).
    """
    periods = sorted({int(r.return_period) for r in rasters})
    if len(periods) > 1:
        return {p: str(p) for p in periods}
    mixed = len({HazardType(r.hazard_type) for r in rasters}) > 1
    return {periods[0]: "mix0" if mixed else str(periods[0])}


def hazard_type_code(rasters: Sequence[FloodRaster]) -> int:
    """``FloodHazardTypes`` passed to the SQL: 1 riverine, 2 coastal, 3 riverine and coastal."""
    kinds = {HazardType(r.hazard_type) for r in rasters}
    if len(kinds) > 1:
        return 3
    return 1 if kinds == {HazardType.RIVERINE} else 2


def to_feet(depth: np.ndarray, units: str) -> np.ndarray:
    units = units.lower()
    if units in ("feet", "foot", "ft"):
        return depth
    if units in ("meters", "metres", "meter", "metre", "m"):
        return depth * FEET_PER_METER
    raise ValueError(f"unknown depth units {units!r}")


def prepare_depth_grid(src: str | Path, dst: str | Path, units: str = "feet", *, block: int = 1024) -> Path:
    """Write ``src`` as a float32 GeoTIFF in EPSG:4326 with depths in feet.

    Hazus uses ArcGIS ``ProjectRaster`` with nearest-neighbour resampling. Here the output grid size
    comes from GDAL's default transform, so cell boundaries can differ slightly from ArcGIS. Each output
    cell takes the source cell under its centre, computed exactly (no approximate transformer). The result
    therefore does not depend on how the grid is split.

    The grid is processed in ``block`` × ``block`` output windows, so memory use stays small for large
    (e.g. 1 m lidar-based) depth grids.
    """
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds
    from rasterio.windows import Window, bounds as window_bounds, from_bounds, transform as window_transform

    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(src) as ds:
        same_crs = bool(ds.crs) and ds.crs.to_epsg() == 4326
        if same_crs:
            transform, width, height = ds.transform, ds.width, ds.height
        else:
            transform, width, height = calculate_default_transform(ds.crs, WGS84, ds.width, ds.height, *ds.bounds)
        profile = dict(driver="GTiff", height=height, width=width, count=1, dtype="float32", crs=WGS84,
                       transform=transform, nodata=np.float32(np.nan), compress="deflate", tiled=True,
                       blockxsize=256, blockysize=256, BIGTIFF="IF_SAFER")
        whole = Window(0, 0, ds.width, ds.height)
        with rasterio.open(dst, "w", **profile) as out_ds:
            for row in range(0, height, block):
                for col in range(0, width, block):
                    win = Window(col, row, min(block, width - col), min(block, height - row))
                    if same_crs:
                        out = ds.read(1, window=win, masked=True).astype("float64").filled(np.nan)
                    else:
                        out = np.full((int(win.height), int(win.width)), np.nan)
                        # source cells that can fall under this window's cell centres, plus a margin
                        sb = transform_bounds(WGS84, ds.crs, *window_bounds(win, transform), densify_pts=21)
                        swin = from_bounds(*sb, transform=ds.transform)
                        swin = Window(int(np.floor(swin.col_off)) - 2, int(np.floor(swin.row_off)) - 2,
                                      int(np.ceil(swin.width)) + 5, int(np.ceil(swin.height)) + 5)
                        try:
                            swin = swin.intersection(whole)
                        except rasterio.errors.WindowError:
                            swin = None
                        if swin is not None and swin.width > 0 and swin.height > 0:
                            data = ds.read(1, window=swin, masked=True).astype("float64").filled(np.nan)
                            reproject(source=data, destination=out, src_transform=ds.window_transform(swin),
                                      src_crs=ds.crs, src_nodata=np.nan, dst_transform=window_transform(win, transform),
                                      dst_crs=WGS84, dst_nodata=np.nan, resampling=Resampling.nearest, tolerance=0)
                    out_ds.write(to_feet(out, units).astype("float32"), 1, window=win)
    return dst


def merge_max(paths: Sequence[str | Path], dst: str | Path) -> Path:
    """Cell-wise maximum of several EPSG:4326 grids, ignoring NoData (ArcGIS ``CellStatistics MAXIMUM``).

    The output covers the union of the inputs at the finest input resolution, aligned to the first grid.
    """
    import rasterio
    from rasterio.merge import merge

    srcs = [rasterio.open(p) for p in paths]
    try:
        res = min(min(abs(s.res[0]), abs(s.res[1])) for s in srcs)
        mosaic, transform = merge(srcs, res=res, method="max", nodata=np.nan, dtype="float32")
    finally:
        for s in srcs:
            s.close()
    dst = Path(dst)
    profile = dict(driver="GTiff", height=mosaic.shape[1], width=mosaic.shape[2], count=1, dtype="float32",
                   crs=WGS84, transform=transform, nodata=np.float32(np.nan), compress="deflate", tiled=True)
    with rasterio.open(dst, "w", **profile) as out:
        out.write(mosaic[0], 1)
    return dst


@dataclass(frozen=True)
class DepthGrid:
    """A user-supplied depth grid, before :func:`create_scenario` prepares it."""

    path: Path
    hazard_type: HazardType
    return_period: int = -1
    units: str = "feet"


def create_scenario(grids: Sequence[DepthGrid], out_dir: str | Path) -> list[FloodRaster]:
    """Prepare a scenario's depth grids as ``create_flood_scenario.py`` does.

    Every grid is reprojected to EPSG:4326 and converted to feet (:func:`prepare_depth_grid`).

    * Single event (all return periods -1): grids of the same hazard type are merged with a cell-wise maximum.
    * Return periods (AAL): return periods must be unique per hazard type. A hazard type with one or two return
      periods is only accepted if the other hazard type has at least three.
    """
    out_dir = Path(out_dir)
    grids = [DepthGrid(Path(g.path), HazardType(g.hazard_type), int(g.return_period), g.units) for g in grids]
    if not grids:
        raise ValueError("a flood scenario needs at least one depth grid")
    aal = any(g.return_period != -1 for g in grids)
    if aal:
        if any(g.return_period <= 0 for g in grids):
            raise ValueError("every depth grid of a return-period scenario needs a return period > 0")
        periods = {h: [g.return_period for g in grids if g.hazard_type is h] for h in HazardType}
        for h, rps in periods.items():
            if len(rps) != len(set(rps)):
                raise ValueError(f"{h.value} return periods must be unique")
        n_r, n_c = len(periods[HazardType.RIVERINE]), len(periods[HazardType.COASTAL])
        if 0 < n_r < 3 and n_c < 3 or 0 < n_c < 3 and n_r < 3:
            raise ValueError("a return-period (AAL) scenario needs at least three depth grids of one hazard type")

    prepared: dict[tuple[HazardType, int], list[Path]] = {}
    for i, g in enumerate(grids):
        dst = out_dir / g.hazard_type.value / f"{i:02d}_{g.path.stem}.tif"
        prepared.setdefault((g.hazard_type, g.return_period), []).append(prepare_depth_grid(g.path, dst, g.units))

    rasters = []
    for (hazard, rp), paths in prepared.items():
        path = paths[0] if len(paths) == 1 else merge_max(paths, out_dir / hazard.value / f"max_{hazard.value}.tif")
        rasters.append(FloodRaster(path, hazard, rp))
    return rasters


def read_point(ds, x: float, y: float) -> float | None:
    """Value of the cell containing (x, y), or None outside the grid or on NoData (Esri ``MapToPixel``)."""
    row, col = ds.index(x, y)
    if row < 0 or col < 0 or row >= ds.height or col >= ds.width:
        return None
    value = ds.read(1, window=((row, row + 1), (col, col + 1)), masked=True)[0, 0]
    if np.ma.is_masked(value) or not np.isfinite(value):
        return None
    return float(value)


def max_depth_at_points(rasters: Iterable[FloodRaster], xs: Sequence[float],
                        ys: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """Per-point maximum depth over ``rasters`` and the controlling hazard ("R" or "C").

    As in Hazus, the running maximum starts at 0.0 with hazard "R", so points outside every grid get
    depth 0.0 rather than "not flooded".
    """
    import rasterio

    xs = np.asarray(xs, dtype="float64")
    ys = np.asarray(ys, dtype="float64")
    depth = np.zeros(len(xs), dtype="float64")
    hazard = np.full(len(xs), "R", dtype=object)
    for fr in rasters:
        with rasterio.open(fr.path) as ds:
            rows, cols = rasterio.transform.rowcol(ds.transform, xs, ys, op=np.floor)
            rows = np.asarray(rows).astype("int64")  # floats with op=np.floor in recent rasterio
            cols = np.asarray(cols).astype("int64")
            inside = (rows >= 0) & (cols >= 0) & (rows < ds.height) & (cols < ds.width)
            values = np.full(len(xs), np.nan)
            for k in np.flatnonzero(inside):  # one cell per point; the grid is never read whole
                cell = ds.read(1, window=((rows[k], rows[k] + 1), (cols[k], cols[k] + 1)), masked=True)
                values[k] = np.ma.filled(cell.astype("float64"), np.nan)[0, 0]
        higher = np.nan_to_num(values, nan=-np.inf) > depth
        depth[higher] = values[higher]
        hazard[higher] = fr.hazard_type.controlling_code
    return depth, hazard
