"""Flood debris (finishes, structure, foundation) for the general building stock.

Port of ``CalculateDebris`` (Hazus 7.2). Quirks kept from the SQL:

* Depth bins are joined to ``flDebris`` weight rows by flood depth (``MinFloodDepth <= depth < MaxFloodDepth``,
  in feet), **not** by depth in structure.
* The foundation mix comes from ``flSchemeRiverine`` / ``flSchemeCoastal``. The scheme is matched **by block
  type only**: every scheme of that block type in the state is joined, not the block's own scheme.
* Pre-FIRM and Coastal-A post-FIRM tons apply to hazard types 1–3 (R, A, CA). V-zone post-FIRM tons apply to
  hazard type 4 (CV) only.
* Results are ``NUMERIC(19,2)``: SQL Server rounds the float sum's exact binary value half away from zero, so
  a sum stored as 2695.74499999999989 becomes 2695.74.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from hazus.data import table
from hazus.flood.gbs import _pre_post_bldg, _pre_post_pct, _strip, sql_round, unpivot_area_weighted

SLAB = {"Fill", "Slab"}
FOOTING = {"Basement", "Crawl", "Pier", "Pile", "SolidWall"}
TONS = ["PreFinishTons", "PostAFinishTons", "PostVFinishTons", "PreStructureTons", "PostAStructureTons",
        "PostVStructureTons", "PreFoundationTons", "PostAFoundationTons", "PostVFoundationTons"]


def _foundation_category(s: pd.Series) -> pd.Series:
    s = _strip(s)
    return np.where(s.isin(SLAB), "Slab", np.where(s.isin(FOOTING), "Footing", "Slab"))


def _debris_foundation(region: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    if "flSchemeCoastal" in region and not region["flSchemeCoastal"].empty:
        c = region["flSchemeCoastal"].copy()
        c["FoundationCategory"] = _foundation_category(c["FoundationType"])
        c["SchemeId"], c["SOccup"] = _strip(c["SchemeId"]), _strip(c["SOccup"])
        parts.append(c.groupby(["SchemeId", "SOccup", "FoundationCategory"], as_index=False).agg(
            PrePercent=("PreFirmDist", "sum"), PostAPercent=("PostFirmDistAZone", "sum"),
            PostVPercent=("PostFirmDistVZone", "sum")))
    if "flSchemeRiverine" in region and not region["flSchemeRiverine"].empty:
        r = region["flSchemeRiverine"].copy()
        r["FoundationCategory"] = _foundation_category(r["FoundationType"])
        r["SchemeId"], r["SOccup"] = _strip(r["SchemeId"]), _strip(r["SOccup"])
        g = r.groupby(["SchemeId", "SOccup", "FoundationCategory"], as_index=False).agg(
            PrePercent=("PreFirmDist", "sum"), PostAPercent=("PostFirmDist", "sum"))
        g["PostVPercent"] = 0
        parts.append(g)
    if not parts:
        return pd.DataFrame(columns=["SchemeId", "SOccup", "FoundationCategory", "PrePercent", "PostAPercent",
                                     "PostVPercent"])
    return pd.concat(parts, ignore_index=True)


def calculate_debris(region: Mapping[str, pd.DataFrame], area_weighted: pd.DataFrame, *, study_case_id: int = 1,
                     analysis_opt_id: int = 0) -> pd.DataFrame:
    """The Hazus ``flFRDebris`` table (tons per block, return period and hazard type)."""
    aw_t = unpivot_area_weighted(area_weighted)
    bt = table("flBlockType")
    ht = table("flHazType")
    aw = aw_t.merge(bt[["ID", "BlockTypeID"]].rename(columns={"ID": "BlockTypeNum", "BlockTypeID": "BT"}),
                    left_on="ControllingHazard", right_on="BT") \
        .merge(ht[["ID", "ZoneAbbr"]].rename(columns={"ID": "HazTypeID"}), left_on="CoastalZone", right_on="ZoneAbbr")
    aw = aw[aw["PctAffected"] > 0]

    scheme = region["flSchemeMapping"].copy()
    scheme["CensusBlock"] = _strip(scheme["CensusBlock"])
    demo = region["hzDemographicsB"].copy()
    demo["CensusBlock"] = _strip(demo["CensusBlock"])
    sc_blocks = aw_t.loc[aw_t["StudyCaseID"] == study_case_id, "CensusBlock"].unique()
    ppp = _pre_post_pct(_pre_post_bldg(demo[demo["CensusBlock"].isin(sc_blocks)], scheme), scheme)

    gbs = aw[(aw["StudyCaseID"] == study_case_id) & (aw["AnalysisOptId"] == analysis_opt_id)].merge(ppp, on="CensusBlock")
    gbs = pd.DataFrame({
        "StudyCaseId": gbs["StudyCaseID"], "AnalysisOptId": gbs["AnalysisOptId"], "ReturnPeriodID": gbs["ReturnPeriodID"],
        "BlockTypeID": gbs["BlockTypeNum"], "TypeOfHaz": gbs["HazTypeID"].map({1: 1, 2: 2, 3: 2, 4: 2, 5: 5, 6: 6, 7: 7}),
        "CensusBlock": gbs["CensusBlock"],
        "Depth": (gbs["Depth"].to_numpy(np.float32) / np.float32(100)).astype("float64"),  # CAST(Depth AS REAL)/100
        "HazTypeID": gbs["HazTypeID"], "PctAffected": gbs["PctAffected"],
        "PrePct": gbs["PrePct"] / 100.0, "PostPct": gbs["PostPct"] / 100.0}).drop_duplicates()

    # square footage of every occupancy with non-zero area, over all blocks of the region
    sq = region["hzSqFootageOccupB"].copy()
    sq["CensusBlock"] = _strip(sq["CensusBlock"])
    occs = [c for c in sq.columns if c.endswith("F") and c not in ("TotalSqft",) and
            c[:-1] in set(_strip(table("ClSOccupancy")["SOccup"]))]
    occ = sq.melt(id_vars="CensusBlock", value_vars=occs, var_name="col", value_name="SqFootage")
    occ = occ[occ["SqFootage"].notna() & (occ["SqFootage"] != 0)]
    occ["SOccupancy"] = occ["col"].str[:-1]
    occ["SqFootage"] = occ["SqFootage"].astype(np.float32).astype("float64")  # REAL column
    deb = table("flDebris")
    deb["SOccup"] = _strip(deb["SOccup"])
    deb["FoundType"] = _strip(deb["FoundType"])
    dfw = occ.merge(deb, left_on="SOccupancy", right_on="SOccup", how="left")

    states = {t[:2] for t in _strip(region["hzTract"]["Tract"])}
    dist_r = table("flGBSDistRes")
    m = dist_r[dist_r["StateFIPS"].isin(states)][["SchemeID", "BlockTypeID"]].drop_duplicates()

    j = gbs.merge(m, on="BlockTypeID", how="left")
    j = j.merge(dfw, on="CensusBlock")
    j = j[(j["Depth"] >= j["MinFloodDepth"]) & (j["Depth"] < j["MaxFloodDepth"])]
    df = _debris_foundation(region)
    j = j.merge(df, left_on=["SchemeID", "SOccup", "FoundType"], right_on=["SchemeId", "SOccup", "FoundationCategory"],
                how="left")

    base = j["PctAffected"].to_numpy("float64") / 100.0 * j["SqFootage"].to_numpy("float64")
    haz = j["HazTypeID"].to_numpy()
    lo = haz <= 3
    v = haz == 4

    def tons(pct_col, wt_col, share_col, mask):
        pct = j[pct_col].astype("float64").astype(np.float32).astype("float64")  # CAST(... AS REAL)
        val = base * pct / 100.0 * j[wt_col].to_numpy("float64") * j[share_col].to_numpy("float64")
        return np.where(mask, val, 0.0)

    out = j[["CensusBlock", "StudyCaseId", "AnalysisOptId", "ReturnPeriodID", "TypeOfHaz"]].copy()
    for part, wt in (("Finish", "FinishWt"), ("Structure", "StructureWt"), ("Foundation", "FoundationWt")):
        out[f"Pre{part}Tons"] = tons("PrePercent", wt, "PrePct", lo)
        out[f"PostA{part}Tons"] = tons("PostAPercent", wt, "PostPct", lo)
        out[f"PostV{part}Tons"] = tons("PostVPercent", wt, "PostPct", v)
    keys = ["CensusBlock", "StudyCaseId", "AnalysisOptId", "ReturnPeriodID", "TypeOfHaz"]
    res = out.groupby(keys, as_index=False)[TONS].sum(min_count=1)
    for c in TONS:
        res[c] = sql_round(res[c].to_numpy("float64"), 2)  # NUMERIC(19,2)
    return res.rename(columns={"ReturnPeriodID": "ReturnPeriodId"})
