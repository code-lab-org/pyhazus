"""Copy the Hazus lookup tables the library needs from the database export into the package.

    python tools/build_data.py --export work/db --out src/hazus/data/hazus72

Takes the non-empty lookup tables of the study-region template (``HazusModel``) and the national
distribution tables of ``flTmpDB``. Study-region inventory and result tables are not lookups and are
skipped.

``--schema`` also writes ``_region_schema.parquet``: the column types of every study-region table. Region
creation (``hazus.inventory.region``) converts state data to those types, as the INSERTs of the Hazus
aggregation do. It reads the template database from the SQL Server of ``tools/export_db.py``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pyarrow.parquet as pq

LOOKUP_PREFIXES = ("fl", "hz", "cl", "Cl")
SKIP_PREFIXES = ("flAn", "flFR", "flGBSX", "flGBSFinal", "flStudyCase", "flDmgBase", "flDmgFinal")
FLTMPDB_TABLES = ("flGBSDistNonRes", "flGBSDistRes")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", type=Path, default=Path("work/db"))
    ap.add_argument("--out", type=Path, default=Path("src/hazus/data/hazus72"))
    ap.add_argument("--schema", action="store_true", help="also write _region_schema.parquet (needs SQL Server)")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted((args.export / "HazusModel").glob("dbo.*.parquet")):
        name = src.stem.removeprefix("dbo.")
        if not name.startswith(LOOKUP_PREFIXES) or name.startswith(SKIP_PREFIXES):
            continue
        if pq.ParquetFile(src).metadata.num_rows == 0:
            continue
        shutil.copyfile(src, args.out / f"{name}.parquet")
        copied += 1
    for name in FLTMPDB_TABLES:
        shutil.copyfile(args.export / "flTmpDB" / f"dbo.{name}.parquet", args.out / f"{name}.parquet")
        copied += 1
    if args.schema:
        write_region_schema(args.out / "_region_schema.parquet")
    size = sum(p.stat().st_size for p in args.out.glob("*.parquet"))
    print(f"{copied} tables, {size / 1e6:.1f} MB -> {args.out}")
    return 0


def write_region_schema(path: Path, database: str = "HazusModel") -> None:
    import pandas as pd

    sys.path.insert(0, str(Path(__file__).parent))
    from export_db import connect

    with connect(database) as conn, conn.cursor() as cur:
        cur.execute("SELECT t.name, c.name, TYPE_NAME(c.user_type_id), c.precision, c.scale, c.max_length, "
                    "c.is_computed, c.column_id, cc.definition FROM sys.tables t "
                    "JOIN sys.columns c ON c.object_id = t.object_id "
                    "LEFT JOIN sys.computed_columns cc ON cc.object_id = c.object_id AND cc.column_id = c.column_id "
                    "WHERE SCHEMA_NAME(t.schema_id) = 'dbo' ORDER BY t.name, c.column_id")
        rows = cur.fetchall()
    pd.DataFrame(rows, columns=["table", "column", "type", "precision", "scale", "max_length", "is_computed",
                                "column_id", "definition"]).to_parquet(path, index=False)


if __name__ == "__main__":
    sys.exit(main())
