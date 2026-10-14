"""Reference runs of the original Hazus T-SQL, for differential tests of the Python port.

Requires a SQL Server with the Hazus databases attached (see ``tools/export_db.py``) and the connection
environment variables used there (HAZUS_SA_PASSWORD, ...). The study-region template database
``HazusModel`` is used as a scratch study region.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from export_db import connect  # noqa: E402

DB = os.environ.get("HAZUS_SQL_STUDY_DB", "HazusModel")


def available() -> bool:
    if not os.environ.get("HAZUS_SA_PASSWORD"):
        return False
    try:
        with connect(DB):
            return True
    except Exception:
        return False


def execute(sql: str, params: tuple | None = None) -> None:
    with connect(DB) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        while cur.nextset():
            pass


def query(sql: str, params: tuple | None = None) -> pd.DataFrame:
    with connect(DB) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    return pd.DataFrame.from_records(rows, columns=columns)


def dmg_final(haz_type: int) -> pd.DataFrame:
    """``flDmgFinal`` as built by ``absp_DmgDoIt @haz_type``."""
    execute("EXEC absp_DmgDoIt %d", (haz_type,))
    return query("SELECT * FROM flDmgFinal")


REGION_TABLES = ["hzTract", "hzCensusBlock", "hzDemographicsB", "flSchemeMapping", "hzExposureOccupB",
                 "hzExposureContentOccupB", "hzBldgCountOccupB", "hzSqFootageOccupB", "flDepExposureOccupB",
                 "flDepExposureContentOccupB", "hzGenBldgScheme", "flSchemeRiverine", "flSchemeCoastal"]
GBS_RESULT_TABLES = [
    "flGBSFinal",
    "flFRGBSDmgSOccupB", "flFRGBSDmgGOccupB", "flFRGBSDmgGBldgType",
    "flFRGBSEcLossBySOccup", "flFRGBSEcLossByGOccup", "flFRGBSEcLossByTotal", "flFRGBSEcLossByGBldgType",
    "flFRGBSDmgCountSOccupB", "flFRGBSDmgCountGOccupB", "flFRGBSDmgCountGBldgType",
    "flFRGBSDepEcLossBySOccup", "flFRGBSDepEcLossByGOccup", "flFRGBSDepEcLossByTotal",
    "flFRGBSDepEcLossByGBldgType",
]
SCRATCH_TABLES = ["flAnAreaWeighted", "flStudyCase", "flStudyCaseBlocks", "flStudyCaseParms", "flLog",
                  "flAnResultsControl", "flAnShelterResult", "flFRShelter", "flFRDebris"]


def _columns(conn, name: str) -> dict[str, tuple[bool, bool]]:
    """column → (is_computed, is_identity)"""
    with conn.cursor() as cur:
        cur.execute("SELECT name, is_computed, is_identity FROM sys.columns WHERE object_id = OBJECT_ID(%s)", (name,))
        return {r[0]: (bool(r[1]), bool(r[2])) for r in cur.fetchall()}


def _insert(conn, name: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    info = _columns(conn, name)
    cols = [c for c in df.columns if c in info and not info[c][0]]
    identity = any(info[c][1] for c in cols)
    df = df[cols]
    sql = f"INSERT INTO [{name}] ({', '.join(f'[{c}]' for c in cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    rows = [tuple(None if (v is pd.NA or (isinstance(v, float) and v != v)) else
                  (v.item() if hasattr(v, "item") else v) for v in row)
            for row in df.astype(object).itertuples(index=False, name=None)]
    with conn.cursor() as cur:
        if identity:
            cur.execute(f"SET IDENTITY_INSERT [{name}] ON")
        cur.executemany(sql, rows)
        if identity:
            cur.execute(f"SET IDENTITY_INSERT [{name}] OFF")


def load_study_region(region: dict[str, pd.DataFrame], area_weighted: pd.DataFrame,
                      study_case_id: int = 1, study_case_name: str = "Test") -> None:
    """Replace the scratch study region's inventory, area-weighted depths and results."""
    with connect(DB) as conn:
        with conn.cursor() as cur:
            for name in REGION_TABLES:
                cur.execute(f"IF OBJECT_ID('{name}', 'U') IS NOT NULL DELETE FROM [{name}]")
        for name in REGION_TABLES:
            _insert(conn, name, region[name])
    load_area_weighted(area_weighted, study_case_id, study_case_name)


def load_area_weighted(area_weighted: pd.DataFrame, study_case_id: int = 1, study_case_name: str = "Test") -> None:
    """Keep the study region's inventory; replace its area-weighted depths, study case and results."""
    with connect(DB) as conn:
        with conn.cursor() as cur:
            for name in GBS_RESULT_TABLES + SCRATCH_TABLES:
                cur.execute(f"IF OBJECT_ID('{name}', 'U') IS NOT NULL DELETE FROM [{name}]")
        _insert(conn, "flAnAreaWeighted", area_weighted)
        _insert(conn, "flStudyCase", pd.DataFrame({"StudyCaseID": [study_case_id],
                                                   "StudyCaseName": [study_case_name], "StudyCaseDesc": [""]}))


def run_gbs(haz_type: int, study_case_name: str = "Test", analysis_opt_id: int = 0,
            fetch_final: bool = False) -> dict[str, pd.DataFrame]:
    """Run ``absp_GBSDoIt`` as Hazus 7.2 does and return its result tables (plus ``flLog``).

    ``flGBSFinal`` (millions of rows for a real region) is only fetched with ``fetch_final=True``.
    """
    execute("EXEC absp_GBSDoIt @RegionsPath=%s, @StudyCaseName=%s, @AnalysisBits=15, @AnalysisOptID=%d, "
            "@Debug='N', @haztype=%d, @RegDivide=0, @proccode=1",
            ("C:\\HazusData\\Regions\\Test", study_case_name, analysis_opt_id, haz_type))
    out = {name: query(f"SELECT * FROM [{name}]") for name in GBS_RESULT_TABLES
           if fetch_final or name != "flGBSFinal"}
    out["flLog"] = query("SELECT * FROM flLog")
    return out


def run_shelter(shelter_results: pd.DataFrame, haz_type: int, study_case_name: str = "Test") -> dict[str, pd.DataFrame]:
    """Load ``flAnShelterResult`` rows and run ``absp_SHRDoIT`` as Hazus 7.2 does."""
    with connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM flAnShelterResult; DELETE FROM flFRShelter")
        _insert(conn, "flAnShelterResult", shelter_results)
    execute("EXEC absp_SHRDoIT @RegionsPath=%s, @StudyCaseName=%s, @AnalysisBits=15, @AnalysisOptID=0, "
            "@haztype=%d, @Debug='N', @ShIngress=0, @AffectHt=0",
            ("C:\\HazusData\\Regions\\Test", study_case_name, haz_type))
    return {"flFRShelter": query("SELECT * FROM flFRShelter"), "flLog": query("SELECT * FROM flLog")}


def run_debris(study_case_id: int = 1, analysis_opt_id: int = 0) -> pd.DataFrame:
    """``CalculateDebris`` (after refreshing the study region's distribution slice with ``absp_DistDoIt``)."""
    execute("EXEC absp_DistDoIt")
    # absp_GBSDoIt fills flStudyCaseBlocks before debris runs in Hazus
    execute("DELETE flStudyCaseBlocks WHERE StudyCaseId = %d; INSERT flStudyCaseBlocks "
            "SELECT DISTINCT %d AS StudyCaseId, CensusBlock FROM flAnAreaWeighted", (study_case_id, study_case_id))
    execute("EXEC CalculateDebris @StudyCaseID=%d, @AnalysisOptID=%d", (study_case_id, analysis_opt_id))
    return query("SELECT * FROM flFRDebris")


def run_aal(coefficients: dict, study_case_id: int = 1, analysis_opt_id: int = 0) -> dict[str, pd.DataFrame]:
    """Write ``flReturnPeriod`` as the C# does and run ``absp_AALDoIt``; returns the 'Annual' result rows."""
    with connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE flReturnPeriod WHERE StudyCaseId = %d", (study_case_id,))
            for rp, c in coefficients.items():
                cur.execute("INSERT flReturnPeriod (StudyCaseId, ReturnPeriod, Coefficient) VALUES (%d, %d, %s)",
                            (study_case_id, int(rp), str(c)))
    execute("EXEC absp_AALDoIt @StudyCaseID=%d, @AnalysisOptID=%d", (study_case_id, analysis_opt_id))
    names = ["flFRGBSEcLossBySOccup", "flFRGBSEcLossByGOccup", "flFRGBSEcLossByTotal", "flFRGBSEcLossByGBldgType"]
    return {n: query(f"SELECT * FROM [{n}] WHERE ReturnPeriodId = 'Annual'") for n in names}


def log_errors(log: pd.DataFrame) -> list[str]:
    """Messages that ``absp_GetErrorInfo`` logged from CATCH blocks (module starts with '!')."""
    if log.empty:
        return []
    bad = log[log["Module"].astype(str).str.startswith("!")]
    return [f"{m}: {msg}" for m, msg in zip(bad["Module"], bad["Message"])]
