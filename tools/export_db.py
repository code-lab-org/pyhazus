"""Attach Hazus MDF files to a SQL Server 2022 instance and export tables and module definitions.

One-time tooling: the library itself never needs SQL Server.

    # SQL Server 2022 in Docker (data files are copied into the container, not bind-mounted):
    docker run -d --name hazus-sql -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=$env:HAZUS_SA_PASSWORD -p 14333:1433 \
        mcr.microsoft.com/mssql/server:2022-latest
    docker exec hazus-sql mkdir -p /var/opt/mssql/hazus
    docker cp extracted/db72/Hazus_model_Primary.mdf hazus-sql:/var/opt/mssql/hazus/
    ...

    python tools/export_db.py attach --mdf-dir /var/opt/mssql/hazus
    python tools/export_db.py export --out work

Connection settings come from HAZUS_SQL_SERVER (default ``localhost``), HAZUS_SQL_PORT (default 14333),
HAZUS_SQL_USER (default ``sa``) and HAZUS_SA_PASSWORD.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DATABASES = {
    # database name : primary data file name
    "syHazus": "syHazus_Primary.mdf",
    "flTmpDB": "flTmpDB_Primary.mdf",
    "HazusModel": "Hazus_model_Primary.mdf",  # study-region template (holds the absp_* procedures)
}


def connect(database: str = "master"):
    import pymssql

    return pymssql.connect(
        server=os.environ.get("HAZUS_SQL_SERVER", "localhost"),
        port=int(os.environ.get("HAZUS_SQL_PORT", "14333")),
        user=os.environ.get("HAZUS_SQL_USER", "sa"),
        password=os.environ["HAZUS_SA_PASSWORD"],
        database=database,
        autocommit=True,
        tds_version="7.4",
    )


def attach(mdf_dir: str) -> None:
    with connect() as conn, conn.cursor() as cur:
        for db, mdf in DATABASES.items():
            cur.execute("SELECT 1 FROM sys.databases WHERE name = %s", (db,))
            if cur.fetchone():
                print(f"{db}: already attached")
                continue
            path = f"{mdf_dir.rstrip('/')}/{mdf}"
            cur.execute(f"CREATE DATABASE [{db}] ON (FILENAME = N'{path}') FOR ATTACH_REBUILD_LOG")
            print(f"{db}: attached from {path}")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def export_modules(db: str, out: Path) -> None:
    with connect(db) as conn, conn.cursor(as_dict=True) as cur:
        cur.execute("""
            SELECT s.name AS schema_name, o.name, o.type_desc, m.definition,
                   OBJECTPROPERTY(o.object_id, 'IsEncrypted') AS is_encrypted
            FROM sys.objects o
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
            WHERE o.type IN ('P', 'FN', 'IF', 'TF', 'V', 'TR') AND o.is_ms_shipped = 0
            ORDER BY o.type_desc, o.name""")
        rows = cur.fetchall()
    encrypted = [r["name"] for r in rows if r["is_encrypted"]]
    for r in rows:
        if r["definition"] is None:
            continue
        target = out / "sql" / db / r["type_desc"].lower() / f"{_safe(r['schema_name'])}.{_safe(r['name'])}.sql"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(r["definition"].replace("\r\n", "\n"), encoding="utf-8")
    print(f"{db}: {len(rows)} modules exported, {len(encrypted)} encrypted{': ' + ', '.join(encrypted) if encrypted else ''}")


def export_tables(db: str, out: Path, max_rows: int | None = None) -> None:
    import pandas as pd

    with connect(db) as conn, conn.cursor(as_dict=True) as cur:
        cur.execute("""
            SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name, ty.name AS type_name,
                   c.column_id
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.columns c ON c.object_id = t.object_id
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE t.is_ms_shipped = 0
            ORDER BY s.name, t.name, c.column_id""")
        cols = pd.DataFrame(cur.fetchall())
    if cols.empty:
        print(f"{db}: no tables")
        return
    for (schema, table), group in cols.groupby(["schema_name", "table_name"], sort=True):
        select = []
        for name, typ in zip(group["column_name"], group["type_name"]):
            if typ in ("geometry", "geography"):
                select.append(f"[{name}].STAsBinary() AS [{name}]")  # WKB; SRID is kept in the metadata file
            elif typ in ("hierarchyid", "sql_variant", "xml"):
                select.append(f"CAST([{name}] AS NVARCHAR(MAX)) AS [{name}]")
            else:
                select.append(f"[{name}]")
        top = f"TOP {max_rows} " if max_rows else ""
        sql = f"SELECT {top}{', '.join(select)} FROM [{schema}].[{table}]"
        with connect(db) as conn:
            df = pd.read_sql(sql, conn)
        target = out / "db" / db / f"{_safe(schema)}.{_safe(table)}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(target, index=False)
        print(f"  {db}.{schema}.{table}: {len(df):,} rows")
    cols.to_csv(out / "db" / db / "_columns.csv", index=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("attach", help="attach the MDF files (paths as seen by the SQL Server process)")
    a.add_argument("--mdf-dir", required=True)
    e = sub.add_parser("export", help="export module definitions and tables")
    e.add_argument("--out", type=Path, default=Path("work"))
    e.add_argument("--db", action="append", choices=sorted(DATABASES), help="limit to these databases")
    e.add_argument("--modules-only", action="store_true")
    e.add_argument("--max-rows", type=int, help="cap rows per table (for quick looks)")
    args = ap.parse_args(argv)

    if args.cmd == "attach":
        attach(args.mdf_dir)
        return 0
    for db in args.db or DATABASES:
        export_modules(db, args.out)
        if not args.modules_only:
            export_tables(db, args.out, args.max_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
