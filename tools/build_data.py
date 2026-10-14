"""Copy the Hazus lookup tables the library needs from the database export into the package.

    python tools/build_data.py --export work/db --out src/hazus/data/hazus72

Takes the non-empty lookup tables of the study-region template (``HazusModel``) and the national
distribution tables of ``flTmpDB``. Study-region inventory and result tables are not lookups and are
skipped.
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
    size = sum(p.stat().st_size for p in args.out.glob("*.parquet"))
    print(f"{copied} tables, {size / 1e6:.1f} MB -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
