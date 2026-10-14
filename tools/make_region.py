"""Build a Hazus study region from a FEMA state database download, with Hazus's own aggregation SQL.

One-time tooling, for real-data validation: it needs the SQL Server container from ``tools/export_db.py``
with ``syHazus`` attached. Steps:

1. Stream ``<ST>.mdf`` from the state download (``<ST>.zip``) into the container and attach it as ``[<ST>]``.
   The file is not extracted on the host, which saves disk space.
2. Copy the pristine study-region template (``Hazus_model_Primary.mdf``) into the container and attach it
   under the region name.
3. Fill ``syStateSel`` / ``syCountySel`` as ``AggregationLogic`` does and run ``syHazus..AggregateByCounty``
   with the flags Hazus uses: flood and earthquake always, hurricane and tsunami where the state has them.

    python tools/make_region.py DC data/DC.zip --name DC_Region
    python tools/make_region.py MD data/MD.zip --counties 24003,24510 --name MD_AA_Balt

Then ``hazus inventory export DC_Region regions/DC`` writes the region as Parquet. Connection settings are
those of ``tools/export_db.py``; the container name comes from HAZUS_SQL_CONTAINER (default ``hazus-sql``).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from export_db import connect  # noqa: E402

CONTAINER = os.environ.get("HAZUS_SQL_CONTAINER", "hazus-sql")
STATES_DIR = "/var/opt/mssql/states"
REGIONS_DIR = "/var/opt/mssql/regions"


def _docker(*args: str, stdin=None) -> None:
    subprocess.run(["docker", *args], check=True, stdin=stdin)


def _attached(name: str) -> bool:
    with connect("master") as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM sys.databases WHERE name = %s", (name,))
        return cur.fetchone() is not None


def _attach(name: str, path: str) -> None:
    with connect("master") as conn, conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE [{name}] ON (FILENAME = '{path}') FOR ATTACH_REBUILD_LOG")
        while cur.nextset():
            pass


def attach_state(state: str, download: Path) -> None:
    """Stream ``<state>.mdf`` out of the download into the container and attach it as ``[<state>]``."""
    if _attached(state):
        print(f"{state} already attached")
        return
    with zipfile.ZipFile(download) as z:
        member = next((n for n in z.namelist() if n.lower() == f"{state.lower()}.mdf"), None)
        if member is None:
            raise SystemExit(f"{download} has no {state}.mdf")
        _docker("exec", CONTAINER, "mkdir", "-p", STATES_DIR)
        target = f"{STATES_DIR}/{state}.mdf"
        proc = subprocess.Popen(["docker", "exec", "-i", CONTAINER, "sh", "-c", f"cat > {target}"],
                                stdin=subprocess.PIPE)
        with z.open(member) as src:
            shutil.copyfileobj(src, proc.stdin, 1 << 20)
        proc.stdin.close()
        if proc.wait() != 0:
            raise SystemExit(f"copying {member} into the container failed")
    _docker("exec", "-u", "root", CONTAINER, "chown", "mssql", target)
    _attach(state, target)
    print(f"attached {state}")


def attach_template(name: str, template: Path) -> None:
    if _attached(name):
        raise SystemExit(f"database {name} already exists; pick another --name")
    _docker("exec", CONTAINER, "mkdir", "-p", REGIONS_DIR)
    target = f"{REGIONS_DIR}/{name}.mdf"
    _docker("cp", str(template), f"{CONTAINER}:{target}")
    _docker("exec", "-u", "root", CONTAINER, "chown", "mssql", target)
    _attach(name, target)


def aggregate(state: str, name: str, counties: list[str] | None) -> None:
    with connect(state) as conn, conn.cursor() as cur:
        cur.execute("SELECT CountyFips FROM hzCounty ORDER BY CountyFips")
        available = [r[0].strip() for r in cur.fetchall()]
    counties = counties or available
    missing = sorted(set(counties) - set(available))
    if missing:
        raise SystemExit(f"counties not in {state}: {', '.join(missing)}")

    with connect("syHazus") as conn, conn.cursor() as cur:
        cur.execute("SELECT StateFips, HUState, TSState FROM syState WHERE StateID = %s", (state,))
        row = cur.fetchone()
        if row is None:
            raise SystemExit(f"{state} is not in syHazus..syState")
        fips, hurricane, tsunami = row[0].strip(), bool(row[1]), bool(row[2])
        cur.execute("DELETE syCountySel; DELETE syStateSel")
        cur.execute("INSERT syStateSel (StateSel, StateFipsSel, NumCountiesSel) VALUES (%s, %s, %d)",
                    (state, fips, len(counties)))
        for c in counties:
            cur.execute("INSERT syCountySel (CountyFipsSel, StateSel, NumTractsSel) VALUES (%s, %s, -1)", (c, state))
        start = time.time()
        cur.execute("EXEC AggregateByCounty @StateMdfPath = %s, @StudyRegion = %s, @IncludeEarthquake = 1, "
                    "@IncludeFlood = 1, @IncludeHurricane = %d, @IncludeTsunami = %d",
                    (STATES_DIR, name, int(hurricane), int(tsunami)))
        while cur.nextset():
            pass
    print(f"aggregated {len(counties)} counties of {state} into {name} in {time.time() - start:.0f} s")
    # Hazus shrinks the region database after aggregation (SyHazusRepository.ShrinkDatabaseAsync)
    with connect(name) as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM sys.database_files WHERE type_desc = 'LOG'")
        log = cur.fetchone()[0]
        cur.execute("CHECKPOINT")
        cur.execute(f"DBCC SHRINKFILE ([{log}], 64)")
        while cur.nextset():
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("state", help="state abbreviation, e.g. DC")
    ap.add_argument("download", type=Path, help="the FEMA state download (<ST>.zip)")
    ap.add_argument("--name", help="study region database name (default <ST>_Region)")
    ap.add_argument("--counties", help="comma-separated county FIPS codes (default: the whole state)")
    ap.add_argument("--template", type=Path, default=Path("extracted/db72/Hazus_model_Primary.mdf"),
                    help="pristine study-region template")
    args = ap.parse_args()
    state = args.state.upper()
    name = args.name or f"{state}_Region"
    attach_state(state, args.download)
    attach_template(name, args.template)
    aggregate(state, name, args.counties.split(",") if args.counties else None)


if __name__ == "__main__":
    main()
