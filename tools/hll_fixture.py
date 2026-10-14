"""Turn a Hazus Package Region (.hpr) from the Hazus Loss Library into a cross-check fixture.

The Hazus Loss Library (https://hazards.fema.gov/hazus-loss-library/) publishes the study regions of its
flood analyses as HPR files. An HPR is a zip of the region folder. It holds a SQL Server backup of the region
database, which includes Hazus's own ``flAnAreaWeighted``, and the depth grid of every scenario. This tool
writes a self-contained fixture that ``tests/test_hll_crosscheck.py`` uses without SQL Server:

    <out>/<name>/hpr/                          the extracted package (depth grids under <scenario>/*/Depth/)
    <out>/<name>/region/                       hazus.io study region (blocks, boundary, ...)
    <out>/<name>/reference/flAnAreaWeighted.parquet
    <out>/<name>/scenarios.csv                 StudyCaseID, StudyCaseName, ReturnPeriodID, hazard, depth_grid

It needs the SQL Server container of ``tools/export_db.py`` only while it runs:

    python tools/hll_fixture.py work/hll/download/lbgrandMO.hpr --out work/hll/fixtures
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from export_db import connect  # noqa: E402
from make_region import CONTAINER, _attached, _docker  # noqa: E402

BACKUPS_DIR = "/var/opt/mssql/backups"
REGIONS_DIR = "/var/opt/mssql/regions"


def extract(hpr: Path, dest: Path) -> Path:
    dest = dest.resolve()
    with zipfile.ZipFile(hpr) as z:
        for info in z.infolist():
            if not str((dest / info.filename).resolve()).startswith(str(dest)):
                raise SystemExit(f"unsafe path in {hpr}: {info.filename}")
        z.extractall(dest)
    return dest


def restore(backup: Path, database: str) -> None:
    if _attached(database):
        print(f"{database} already restored")
        return
    _docker("exec", CONTAINER, "mkdir", "-p", BACKUPS_DIR)
    target = f"{BACKUPS_DIR}/{backup.name}"
    _docker("cp", str(backup), f"{CONTAINER}:{target}")
    _docker("exec", "-u", "root", CONTAINER, "chown", "mssql", target)
    with connect("master") as conn, conn.cursor() as cur:
        cur.execute(f"RESTORE FILELISTONLY FROM DISK = '{target}'")
        files = [(r[0], r[2]) for r in cur.fetchall()]
        moves = ", ".join(f"MOVE '{n}' TO '{REGIONS_DIR}/{database}_{i}.{'ldf' if t == 'L' else 'mdf'}'"
                          for i, (n, t) in enumerate(files))
        cur.execute(f"RESTORE DATABASE [{database}] FROM DISK = '{target}' WITH {moves}, RECOVERY")
        while cur.nextset():
            pass
    _docker("exec", CONTAINER, "rm", "-f", target)


def _query(conn, sql: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql)
        return pd.DataFrame.from_records(cur.fetchall(), columns=[d[0] for d in cur.description])


def drop(database: str) -> None:
    with connect("master") as conn, conn.cursor() as cur:
        cur.execute(f"ALTER DATABASE [{database}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        cur.execute(f"DROP DATABASE [{database}]")


def depth_grid(case_dir: Path) -> Path:
    """The scenario's depth grid: the ESRI grid under ``<scenario>/<Riverine|Coastal>/Depth/``."""
    grids = [p for p in case_dir.glob("*/Depth/*") if p.is_dir() and (p / "hdr.adf").exists()]
    if len(grids) != 1:
        raise SystemExit(f"expected one depth grid under {case_dir}, found {[str(g) for g in grids]}")
    return grids[0]


def main() -> None:
    from hazus.inventory.sqlserver import export_region

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hpr", type=Path)
    ap.add_argument("--out", type=Path, default=Path("work/hll/fixtures"))
    ap.add_argument("--name", help="fixture name (default: the HPR file name)")
    ap.add_argument("--keep-db", action="store_true", help="keep the restored database in SQL Server")
    args = ap.parse_args()
    name = args.name or args.hpr.stem
    root = (args.out / name).resolve()
    hpr_dir = extract(args.hpr, root / "hpr")
    backups = list(hpr_dir.glob("*.bk"))
    if len(backups) != 1:
        raise SystemExit(f"expected one .bk database backup in {args.hpr}, found {len(backups)}")
    database = f"HLL_{name}"
    restore(backups[0], database)
    try:
        with connect(database) as conn:
            export_region(conn, root / "region")
            aw = _query(conn, "SELECT * FROM flAnAreaWeighted")
            cases = _query(conn, "SELECT StudyCaseID, StudyCaseName FROM flStudyCase")
    finally:
        if not args.keep_db:
            drop(database)
    backups[0].unlink()  # restored and exported; the fixture does not need it
    aw["CensusBlock"] = aw["CensusBlock"].str.strip()
    (root / "reference").mkdir(exist_ok=True)
    aw.to_parquet(root / "reference" / "flAnAreaWeighted.parquet", index=False)

    rows = []
    cases["StudyCaseName"] = cases["StudyCaseName"].str.strip()
    for (sc, rp, hz), _ in aw.groupby(["StudyCaseID", "ReturnPeriodID", "ControllingHazard"]):
        case = cases.loc[cases["StudyCaseID"] == sc, "StudyCaseName"].iloc[0]
        grid = depth_grid(hpr_dir / case)
        rows.append({"StudyCaseID": sc, "StudyCaseName": case, "ReturnPeriodID": rp.strip(),
                     "hazard": "riverine" if hz.strip() == "R" else "coastal",
                     "depth_grid": grid.relative_to(root).as_posix()})
    pd.DataFrame(rows).to_csv(root / "scenarios.csv", index=False)
    print(f"fixture {root}: {len(rows)} scenarios")


if __name__ == "__main__":
    main()
