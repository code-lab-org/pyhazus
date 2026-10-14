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


def to_feet(depth: np.ndarray, units: str) -> np.ndarray:
    units = units.lower()
    if units in ("feet", "foot", "ft"):
        return depth
    if units in ("meters", "metres", "meter", "metre", "m"):
        return depth * FEET_PER_METER
    raise ValueError(f"unknown depth units {units!r}")


def prepare_depth_grid(src: str | Path, dst: str | Path, units: str = "feet") -> Path:
    """Write ``src`` as a float32 GeoTIFF in EPSG:4326 with depths in feet.

    Hazus uses ArcGIS ``ProjectRaster`` with nearest-neighbour resampling. Here the output grid size
    comes from GDAL's default transform, so cell boundaries can differ slightly from ArcGIS.
    """
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    src, dst = Path(src), Path(dst)
    with rasterio.open(src) as ds:
        data = ds.read(1, masked=True).astype("float64")
        if ds.crs and ds.crs.to_epsg() == 4326:
            transform, width, height = ds.transform, ds.width, ds.height
            out = data.filled(np.nan)
        else:
            transform, width, height = calculate_default_transform(ds.crs, WGS84, ds.width, ds.height, *ds.bounds)
            out = np.full((height, width), np.nan, dtype="float64")
            reproject(
                source=data.filled(np.nan),
                destination=out,
                src_transform=ds.transform,
                src_crs=ds.crs,
                src_nodata=np.nan,
                dst_transform=transform,
                dst_crs=WGS84,
                dst_nodata=np.nan,
                resampling=Resampling.nearest,
            )
    out = to_feet(out, units).astype("float32")
    profile = dict(driver="GTiff", height=height, width=width, count=1, dtype="float32", crs=WGS84,
                   transform=transform, nodata=np.float32(np.nan), compress="deflate", tiled=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dst, "w", **profile) as out_ds:
        out_ds.write(out, 1)
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
            band = ds.read(1, masked=True)
            rows, cols = rasterio.transform.rowcol(ds.transform, xs, ys, op=np.floor)
            rows = np.asarray(rows)
            cols = np.asarray(cols)
            inside = (rows >= 0) & (cols >= 0) & (rows < ds.height) & (cols < ds.width)
            values = np.full(len(xs), np.nan)
            vals = band[rows[inside], cols[inside]]
            values[inside] = np.ma.filled(vals.astype("float64"), np.nan)
        higher = np.nan_to_num(values, nan=-np.inf) > depth
        depth[higher] = values[higher]
        hazard[higher] = fr.hazard_type.controlling_code
    return depth, hazard
