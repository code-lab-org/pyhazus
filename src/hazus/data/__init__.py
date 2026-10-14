"""Packaged Hazus lookup tables.

``hazus72/`` holds the lookup tables of the Hazus 7.2 study-region template database and the national
building-distribution tables, exported once with ``tools/export_db.py`` and copied in with
``tools/build_data.py``. Table and column names are kept exactly as in Hazus.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "hazus72"


@lru_cache(maxsize=None)
def _read(name: str) -> pd.DataFrame:
    path = DATA_DIR / f"{name}.parquet"
    if not path.exists():
        raise KeyError(f"no packaged Hazus table {name!r}")
    return pd.read_parquet(path)


def table(name: str) -> pd.DataFrame:
    """A copy of the packaged Hazus table ``name`` (e.g. ``"flBldgStructDmgFn"``)."""
    return _read(name).copy()


def available() -> list[str]:
    return sorted(p.stem for p in DATA_DIR.glob("*.parquet"))
