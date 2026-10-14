"""Facility damage-function fallback: Python port vs. the original ``flGetDamageXRefId*`` procedures."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
import sqlref  # noqa: E402

pytestmark = pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available")

PROCS = {"struct": ("flGetDamageXRefIdEssntFltyStruct", "flEssntFltyStructDmgXRef"),
         "cont": ("flGetDamageXRefIdEssntFltyCont", "flEssntFltyContDmgXRef"),
         "util": ("flGetDamageXRefIdUtilFlty", "flUtilFltyDmgXRef")}


@pytest.mark.parametrize("kind", list(PROCS))
def test_default_ids_match_sql(kind):
    from hazus.flood.facility_analysis import FacilityDamageFunctions

    proc, xref = PROCS[kind]
    occupancies = sorted(sqlref.query(f"SELECT DISTINCT RTRIM(SOccupId) AS o FROM {xref}")["o"]) + ["NONE"]
    f = FacilityDamageFunctions({xref: sqlref.query(f"SELECT * FROM {xref}")})
    mismatches = []
    for occ in occupancies:
        for hazard, depth in [("R", 3.0), ("R", 9.0), ("C", 6.0), ("C", 6.5)]:
            ref = sqlref.query(f"EXEC {proc} @ControllingHazard=%s, @FloodDepth=%s, @OccupId=%s",
                               (hazard, depth, occ))
            want = None if ref.empty else int(ref.iloc[0, 0])
            got = f.default_id(kind, hazard, depth, occ)
            if got != want:
                mismatches.append((occ, hazard, depth, got, want))
    assert not mismatches, mismatches
