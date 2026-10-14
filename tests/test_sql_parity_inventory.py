"""Inventory export round trip: a study region in SQL Server → Parquet directory → same scenario results."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).parent))
import sqlref  # noqa: E402
import synth  # noqa: E402
from test_sql_parity import compare_tables  # noqa: E402
from test_sql_parity_gbs import CHECKS  # noqa: E402

pytestmark = pytest.mark.skipif(not sqlref.available(), reason="Hazus SQL Server not available")
pytest.importorskip("geopandas")
pytest.importorskip("rasterio")
FACILITY_TABLES = ["hzCareFlty", "flCareFlty", "hzboundary"]


def load_shapes_and_facilities(region):
    sqlref.execute("; ".join(f"DELETE FROM {t}" for t in FACILITY_TABLES))
    with sqlref.connect(sqlref.DB) as conn:
        care = region["hzCareFlty"].assign(Tract=region["hzTract"]["Tract"].iloc[0])
        sqlref._insert(conn, "hzCareFlty", care)
        sqlref._insert(conn, "flCareFlty", region["flCareFlty"])
        with conn.cursor() as cur:
            # the abst_INSflCareFlty trigger replaces inserted values with the class defaults; restore ours
            for r in region["flCareFlty"].itertuples(index=False):
                cur.execute("UPDATE flCareFlty SET FirstFloorHt = %s, BldgDamageFnId = %s, ContDamageFnId = %s, "
                            "FloodProtection = %s WHERE CareFltyId = %s",
                            (float(r.FirstFloorHt), r.BldgDamageFnId, r.ContDamageFnId, int(r.FloodProtection),
                             r.CareFltyId))
            for blk, geom in zip(region["hzCensusBlock"]["CensusBlock"], region["hzCensusBlock"].geometry):
                cur.execute("UPDATE hzCensusBlock SET Shape = geometry::STGeomFromText(%s, 4326) "
                            "WHERE CensusBlock = %s", (geom.wkt, blk))
            for fid, x, y in zip(care["CareFltyId"], care["Longitude"], care["Latitude"]):
                cur.execute("UPDATE hzCareFlty SET Shape = geometry::Point(%s, %s, 4326) WHERE CareFltyId = %s",
                            (float(x), float(y), fid))
            cur.execute("INSERT hzboundary (Shape) VALUES (geometry::STGeomFromText(%s, 4326))",
                        (region["hzCensusBlock"].geometry.union_all().wkt,))
        conn.commit()


def test_export_region_round_trip(tmp_path):
    from hazus.inventory.sqlserver import export_region
    from hazus.io import read_region
    from hazus.flood.scenario import run_scenario

    region, rasters = synth.make_scenario(tmp_path, seed=11, n_blocks=6, depths={-1: 3.5})
    sqlref.load_study_region(region, region["hzTract"].iloc[:0])
    load_shapes_and_facilities(region)

    with sqlref.connect(sqlref.DB) as conn:
        written = export_region(conn, tmp_path / "region")
    assert {"hzCensusBlock", "hzBoundary", "hzCareFlty", "flCareFlty", "flSchemeMapping"} <= set(written)
    exported = read_region(tmp_path / "region")
    assert exported["hzCensusBlock"].crs.to_epsg() == 4326
    assert exported["hzCensusBlock"]["CensusBlock"].tolist() == region["hzCensusBlock"]["CensusBlock"].tolist()
    assert exported["hzCensusBlock"].geometry.geom_equals(region["hzCensusBlock"].geometry).all()

    want = run_scenario(region, rasters)
    got = run_scenario(exported, rasters)
    problems = []
    for name, (keys, float_cols, int_cols) in CHECKS.items():
        problems += [f"{name}: {p}" for p in compare_tables(got[name], want[name], keys, float_cols, int_cols)]
    problems += compare_tables(got["flFRCareFlty"], want["flFRCareFlty"], ["CareFltyId", "ReturnPeriodId"],
                               ["BldgDmgPct", "BldgLoss", "ContDmgPct", "ContLoss"], ["MaxTimetoFullRestoration"])
    assert not problems, "\n".join(problems)
    assert want["flFRGBSEcLossByTotal"]["BuildingLoss"].sum() > 0
