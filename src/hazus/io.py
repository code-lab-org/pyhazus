"""Reading study regions and writing results.

A study region is a directory with one Parquet file per Hazus table, named after the table
(``hzCensusBlock.parquet``, ``flSchemeMapping.parquet``, ...). Tables with geometry (census blocks, the
boundary and facilities) are GeoParquet in EPSG:4326.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pandas as pd


def _is_geoparquet(path: Path) -> bool:
    import pyarrow.parquet as pq

    return b"geo" in (pq.read_schema(path).metadata or {})


def read_region(directory: str | Path) -> dict[str, pd.DataFrame]:
    """All tables of a study-region directory, keyed by table name."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"study region directory not found: {directory}")
    region = {}
    for path in sorted(directory.glob("*.parquet")):
        if _is_geoparquet(path):
            import geopandas as gpd

            region[path.stem] = gpd.read_parquet(path)
        else:
            region[path.stem] = pd.read_parquet(path)
    return region


def write_region(region: Mapping[str, pd.DataFrame], directory: str | Path) -> Path:
    """Write a study region as one (Geo)Parquet file per table."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, df in region.items():
        df.to_parquet(directory / f"{name}.parquet", index=False)
    return directory


def write_results(results: Mapping[str, pd.DataFrame], directory: str | Path, fmt: str = "parquet") -> list[Path]:
    """Write result tables as ``<table>.parquet`` or ``<table>.csv``; returns the files written."""
    if fmt not in ("parquet", "csv"):
        raise ValueError(f"unknown format {fmt!r} (expected 'parquet' or 'csv')")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, df in results.items():
        path = directory / f"{name}.{fmt}"
        if fmt == "csv":
            df.drop(columns="geometry", errors="ignore").to_csv(path, index=False)
        else:
            df.to_parquet(path, index=False)
        written.append(path)
    return written
