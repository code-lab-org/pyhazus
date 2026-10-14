"""Area-weighting insert: Python port vs. the C# join SQL and ``absp_GBSInsertAW``.

The ArcPy tool exports two raster attribute tables to the study region: ``COASTALSECONDJOIN`` (the Combine of
block grid, depth bin and zone: Value/Count_/blkgrid/reclassed/zoned) and ``BLKGRID`` (cells per block:
Value/Count_/CensusBloc). Synthetic versions of both are loaded here, so this checks everything after the
raster processing.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).parent))
import sqlref  # noqa: E402
from test_sql_parity import compare_tables  # noqa: E402

pytestmark = pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available")

ZONES = {"R": 1, "A": 2, "CA": 3, "CV": 4}

# the SQL that CreateAreaWeightedJoinAsync runs
SECOND_JOIN = """
DROP TABLE IF EXISTS flAnGBSSecondJoin;
select a.ObjectID, Cast(a.Count_ as int) as [Count], blkgrid as BlkGrid, b.CensusBloc as CensusBlock,
       zoned as CoastalZone, reclassed as ReClass, Cast(b.Count_ as int) as TotalCount
  into flAnGBSSecondJoin
  from COASTALSECONDJOIN a left join BLKGRID b ON a.blkgrid = b.Value
"""


def make_counts(seed: int, coastal: bool, n_blocks: int = 30) -> pd.DataFrame:
    """Long-form cell counts as :func:`hazus.flood.exposure.area_weighted_depths` returns them."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_blocks):
        total = int(rng.integers(20, 50_000))
        left = total
        zones = list(rng.choice(["A", "CA", "CV"], size=rng.integers(1, 4), replace=False)) if coastal else ["R"]
        for z in zones:
            for reclass in rng.choice(np.arange(0, 2401, 50), size=rng.integers(1, 8), replace=False):
                if left <= 0:
                    break
                c = int(rng.integers(1, max(2, left // 3)))
                left -= c
                rows.append({"CensusBlock": f"24001{i:010d}", "CoastalZone": z, "ReClass": int(reclass),
                             "Count": c, "TotalCount": total})
    long = pd.DataFrame(rows)
    long["PctAffected"] = long["Count"] / long["TotalCount"] * 100.0
    return long


def load_rasters_tables(long: pd.DataFrame) -> None:
    blocks = long.drop_duplicates("CensusBlock")[["CensusBlock", "TotalCount"]].reset_index(drop=True)
    value = {b: i + 1 for i, b in enumerate(blocks["CensusBlock"])}
    blkgrid = pd.DataFrame({"ObjectID": blocks.index + 1, "Value": blocks["CensusBlock"].map(value),
                            "Count_": blocks["TotalCount"].astype(float), "CensusBloc": blocks["CensusBlock"]})
    join = pd.DataFrame({"ObjectID": long.index + 1, "Value": long.index + 1, "Count_": long["Count"].astype(float),
                         "blkgrid": long["CensusBlock"].map(value), "reclassed": long["ReClass"],
                         "zoned": long["CoastalZone"].map(ZONES)})
    sqlref.execute("DROP TABLE IF EXISTS COASTALSECONDJOIN; DROP TABLE IF EXISTS BLKGRID; "
                   "CREATE TABLE COASTALSECONDJOIN (ObjectID int, Value int, Count_ float, blkgrid int, "
                   "reclassed int, zoned int); "
                   "CREATE TABLE BLKGRID (ObjectID int, Value int, Count_ float, CensusBloc nvarchar(30))")
    with sqlref.connect(sqlref.DB) as conn:
        sqlref._insert(conn, "COASTALSECONDJOIN", join)
        sqlref._insert(conn, "BLKGRID", blkgrid)


@pytest.mark.parametrize("seed, coastal", [(3, False), (5, True), (8, True)])
def test_insert_aw_matches_sql(seed, coastal):
    from hazus.flood.exposure import BIN_CODES, area_weighted_rows, bin_column
    from hazus.flood.shelter import shelter_result

    long = make_counts(seed, coastal)
    haz = "C" if coastal else "R"
    load_rasters_tables(long)
    sqlref.execute("DELETE flAnAreaWeighted; DELETE flAnShelterResult")
    sqlref.execute(SECOND_JOIN)
    sqlref.execute("EXEC absp_GBSInsertAW @StudyCaseID=1, @ReturnPeriod='100', @AnalysisOptID=0, @haztype=%s",
                   (haz,))
    ref_aw = sqlref.query("SELECT * FROM flAnAreaWeighted")
    ref_sr = sqlref.query("SELECT * FROM flAnShelterResult")

    got_aw = area_weighted_rows(long, study_case_id=1, return_period="100", controlling_hazard=haz)
    got_sr = shelter_result(long, study_case_id=1, return_period="100", control_hazard=haz)

    bins = [bin_column(int(b)) for b in BIN_CODES]
    problems = compare_tables(got_aw, ref_aw, ["CensusBlock", "CoastalZone"], bins,
                              ["StudyCaseID", "Proc_Order", "AnalysisOptId"], rtol=1e-12, atol=1e-12)
    problems += compare_tables(got_sr, ref_sr, ["CensusBlock"], ["PctArea", "CensusArea", "AffectArea"],
                               ["StudyCaseId", "AnalysisOptId"], rtol=1e-12, atol=1e-12)
    assert not problems, "\n".join(problems)
    assert len(ref_aw) == len(got_aw) > 0
    assert (ref_aw["ControllingHazard"].str.strip() == haz).all()
