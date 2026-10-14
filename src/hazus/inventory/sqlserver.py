"""Export a Hazus study-region database from SQL Server to a study-region directory.

This is the one step that needs SQL Server: attach the database (a Hazus study region, or the template
``Hazus_model_Primary.mdf`` filled by Hazus) to any SQL Server, for example the Docker image described in
``docs/extraction.md``, then run ``hazus inventory export``. Needs the ``pymssql`` package
(``pip install pyhazus[tools]``).

``geometry`` columns (``Shape``) become GeoParquet geometry in the column's SRID. ``char`` values lose their
trailing padding, and ``decimal``/``money`` values become floats (the C# converts them to ``double`` before
use).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

# Tables the flood model reads from a study region, under the names used by this package.
FLOOD_TABLES = [
    "hzTract", "hzCensusBlock", "hzBoundary", "hzDemographicsB", "flSchemeMapping", "hzGenBldgScheme",
    "flSchemeRiverine", "flSchemeCoastal",
    "hzExposureOccupB", "hzExposureContentOccupB", "hzBldgCountOccupB", "hzSqFootageOccupB",
    "flDepExposureOccupB", "flDepExposureContentOccupB",
    "hzCareFlty", "flCareFlty", "hzPoliceStation", "flPoliceStation", "hzFireStation", "flFireStation",
    "hzEmergencyCtr", "flEmergencyCtr", "hzSchool", "flSchool",
    "hzPotableWaterFlty", "flPotableWaterFlty", "hzWasteWaterFlty", "flWasteWaterFlty", "hzOilFlty", "flOilFlty",
    "hzNaturalGasFlty", "flNaturalGasFlty", "hzElectricPowerFlty", "flElectricPowerFlty",
]
_FACILITY_TABLES = FLOOD_TABLES[FLOOD_TABLES.index("hzCareFlty"):]

# Tables of a FEMA state inventory database that study-region creation reads (hazus.inventory.region).
STATE_TABLES = [
    "hzCounty", "hzTract", "hzCensusBlock", "hzCensusBlock_TIGER", "hzDemographicsB",
    "hzExposureOccupB", "hzExposureContentOccupB", "hzBldgCountOccupB", "hzSqFootageOccupB",
    "hzGenBldgSchemes", "hzGenBldgScheme", "flSchemeInfo", "flSchemeMapping", "flSchemeRiverine",
    "flSchemeCoastal", *_FACILITY_TABLES,
]


def connect(database: str, *, server: str | None = None, port: int | None = None, user: str | None = None,
            password: str | None = None):
    """pymssql connection. Defaults come from ``HAZUS_SQL_SERVER``, ``HAZUS_SQL_PORT``, ``HAZUS_SQL_USER`` and
    ``HAZUS_SA_PASSWORD`` (as for ``tools/export_db.py``)."""
    import pymssql

    return pymssql.connect(
        server=server or os.environ.get("HAZUS_SQL_SERVER", "127.0.0.1"),
        port=str(port or os.environ.get("HAZUS_SQL_PORT", "14333")),
        user=user or os.environ.get("HAZUS_SQL_USER", "sa"),
        password=password if password is not None else os.environ["HAZUS_SA_PASSWORD"],
        database=database,
    )


def _table_columns(conn, name: str) -> tuple[str, list[tuple[str, str]]] | None:
    """(actual table name, [(column, type)]) or None if the table does not exist. Names match case-insensitively."""
    with conn.cursor() as cur:
        cur.execute("SELECT t.name, c.name, ty.name FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id "
                    "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
                    "WHERE t.name = %s AND SCHEMA_NAME(t.schema_id) = 'dbo' ORDER BY c.column_id", (name,))
        rows = cur.fetchall()
    if not rows:
        return None
    return rows[0][0], [(r[1], r[2]) for r in rows]


def read_table(conn, name: str) -> pd.DataFrame | None:
    """One table as a DataFrame (a GeoDataFrame if it has a geometry column); None if it does not exist."""
    found = _table_columns(conn, name)
    if found is None:
        return None
    actual, columns = found
    geom = [c for c, t in columns if t in ("geometry", "geography")]
    select = ", ".join(f"[{c}].STAsBinary() AS [{c}]" if c in geom else f"[{c}]" for c, _ in columns)
    srid = None
    with conn.cursor() as cur:
        cur.execute(f"SELECT {select} FROM dbo.[{actual}]")
        rows = cur.fetchall()
        if geom:
            cur.execute(f"SELECT TOP 1 [{geom[0]}].STSrid FROM dbo.[{actual}] WHERE [{geom[0]}] IS NOT NULL")
            r = cur.fetchone()
            srid = r[0] if r else None
    df = pd.DataFrame(rows, columns=[c for c, _ in columns])
    for c, t in columns:
        if t in ("char", "nchar"):
            df[c] = df[c].map(lambda v: v.rstrip() if isinstance(v, str) else v)
        elif t in ("decimal", "numeric", "money", "smallmoney"):
            df[c] = pd.to_numeric(df[c].map(lambda v: None if v is None else float(v)), errors="coerce")
    if not geom:
        return df
    import geopandas as gpd
    import shapely

    shapes = shapely.from_wkb([bytes(v) if v is not None else None for v in df[geom[0]]])
    others = [g for g in geom[1:]]
    df = df.drop(columns=[geom[0], *others])
    return gpd.GeoDataFrame(df, geometry=shapes, crs=f"EPSG:{srid or 4326}")


def export_region(conn, out_dir: str | Path, tables: Iterable[str] = FLOOD_TABLES) -> dict[str, Path]:
    """Write each table that exists in the database to ``out_dir/<table>.parquet``; returns what was written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name in tables:
        df = read_table(conn, name)
        if df is None:
            continue
        path = out_dir / f"{name}.parquet"
        df.to_parquet(path, index=False)
        written[name] = path
    return written


def export_state(conn, out_dir: str | Path) -> dict[str, Path]:
    """Export a FEMA state inventory database (attached as e.g. ``[DC]``) for ``hazus.inventory.region``."""
    return export_region(conn, out_dir, STATE_TABLES)
