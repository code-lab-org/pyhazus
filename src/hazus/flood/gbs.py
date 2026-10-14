"""General building stock (aggregated baseline inventory) flood damage and loss.

Port of ``absp_GBSDoIt`` (Hazus 7.2) for the parameter values 7.2 actually uses (see
``docs/flood-gbs.md``):

* ``absp_GBSBasePrep`` → :func:`base_prep` (the ``flGBSFinal`` table)
* ``absp_GBSBldgContDmg`` → :func:`bldg_cont_dmg` (``flFRGBSDmgSOccupB`` + rollups)
* ``absp_GBSEconLoss`` → :func:`econ_loss` (``flFRGBSEcLossBySOccup`` + rollups)
* ``absp_GBSDmgCounts`` → :func:`dmg_counts` (``flFRGBSDmgCountSOccupB`` + rollups)
* ``absp_GBSDepEconLoss`` → :func:`dep_econ_loss` (``flFRGBSDepEcLossBySOccup`` + rollups)

Inputs and outputs are DataFrames with the Hazus table and column names. Arithmetic follows the SQL:
intermediate ``REAL`` columns are float32, float sums are float64, and floats stored in ``INT`` columns are
truncated toward zero.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

import numpy as np
import pandas as pd

from hazus.data import table
from hazus.flood.ddf import build_dmg_final

F32 = np.float32
BIN_CODES = list(range(0, 2401, 50))
BIN_COLS = ["ft00"] + [f"ft{b}" for b in BIN_CODES[1:]]
DMG_BINS = ["NoDmg", "Dmg1to10", "Dmg11to20", "Dmg21to30", "Dmg31to40", "Dmg41to50", "Dmg51to60",
            "Dmg61to70", "Dmg71to80", "Dmg81to90", "Dmg91to100"]
BLDG_TYPES = [("Wood", "WPct"), ("Steel", "SPct"), ("Concrete", "CPct"), ("Masonry", "MPct"),
              ("ManufHousing", "HPct")]
INV_OCCIDS = {12, 13, 22, 23, 24, 25, 26, 27, 28}  # COM1, COM2, IND1-6, AGR1
ECON_COLS = ["BuildingLoss", "ContentsLoss", "InventoryLoss", "RelocationCost", "IncomeLoss",
             "RentalIncomeLoss", "WageLoss", "OutputLoss", "EmploymentLoss"]

Region = Mapping[str, pd.DataFrame]


# ----------------------------------------------------------------------------------------------- helpers

def _strip(s: pd.Series) -> pd.Series:
    return s.astype(str).str.rstrip()


def sql_int(x) -> np.ndarray:
    """Float → INT column: SQL Server truncates toward zero."""
    return np.trunc(np.asarray(x, dtype="float64")).astype("int64")


def sql_round(x, digits: int = 0) -> np.ndarray:
    """T-SQL ROUND on a float, which is also how a float is stored in a NUMERIC column.

    SQL Server rounds the float's exact binary value half away from zero: ``ROUND(2.675, 2)`` is 2.67 because
    the double nearest 2.675 is slightly below it.
    """
    x = np.asarray(x, dtype="float64")
    f = 10.0 ** digits
    out = np.sign(x) * np.floor(np.abs(x) * f + 0.5) / f
    # |x| * f is itself rounded; settle the values that land near a half with exact decimal arithmetic
    t = np.abs(x) * f
    near = np.abs(t - np.floor(t) - 0.5) < 1e-6
    if near.any():
        quantum = Decimal(1).scaleb(-digits)
        out[near] = [float(Decimal(v).quantize(quantum, ROUND_HALF_UP)) for v in x[near]]
    return out


def _round_f32(x: np.ndarray) -> np.ndarray:
    """ROUND(real, 0) in float32."""
    x = np.asarray(x, dtype=F32)
    return (np.sign(x) * np.floor(np.abs(x) + F32(0.5))).astype(F32)


def _occupancies() -> pd.DataFrame:
    cls = table("ClSOccupancy")
    cls["SOccup"] = _strip(cls["SOccup"])
    return cls[["SOccup", "SortOrder"]]


def _occ_column(region: Region, name: str, suffix: str, blocks: pd.Series, occ: pd.Series) -> np.ndarray:
    """Per-row value of the block's ``{SOccup}{suffix}`` column in a ``*OccupB`` table (NaN if missing)."""
    t = region[name].copy()
    t["CensusBlock"] = _strip(t["CensusBlock"])
    long = t.melt(id_vars="CensusBlock", var_name="col", value_name="v")
    key = pd.DataFrame({"CensusBlock": blocks.to_numpy(), "col": (occ + suffix).to_numpy()})
    return key.merge(long, on=["CensusBlock", "col"], how="left")["v"].to_numpy(dtype="float64")


# ------------------------------------------------------------------------------------------- base prep

def unpivot_area_weighted(aw: pd.DataFrame) -> pd.DataFrame:
    """``#AnAreaWeightedT``: one row per (block, zone, depth bin)."""
    keys = ["StudyCaseID", "ReturnPeriodID", "AnalysisOptId", "ControllingHazard", "CoastalZone", "CensusBlock"]
    long = aw[keys + BIN_COLS].melt(id_vars=keys, value_vars=BIN_COLS, var_name="col", value_name="PctAffected")
    long = long[long["PctAffected"].notna()]
    long["Depth"] = long["col"].map(dict(zip(BIN_COLS, BIN_CODES))).astype("int64")
    long["CensusBlock"] = _strip(long["CensusBlock"])
    long["CoastalZone"] = _strip(long["CoastalZone"])
    long["ControllingHazard"] = _strip(long["ControllingHazard"])
    long["ReturnPeriodID"] = _strip(long["ReturnPeriodID"])
    return long.drop(columns="col").reset_index(drop=True)


def _pre_post_bldg(demo: pd.DataFrame, scheme: pd.DataFrame) -> pd.DataFrame:
    """Per block PreBldg / PostBldg / TotalBldg around the NFIP entry date (``#GBSPrePostBldg``)."""
    d = demo.merge(scheme, on="CensusBlock")
    ed = d["EntryDate"].to_numpy(dtype="int64")
    edf = ed.astype(F32)
    b = {c: d[c].fillna(0).to_numpy(dtype="int64") for c in
         ["BuiltBefore40", "Built40to49", "Built50to59", "Built60to69", "Built70to79", "Built80to89",
          "Built90to99", "Built00to09", "Built10to13", "BuiltAfter13"]}
    cum = np.cumsum(np.stack(list(b.values())), axis=0)  # cumulative counts through each decade column
    names = list(b)

    def upto(col):  # sum of all decade columns through ``col`` (inclusive)
        return cum[names.index(col)]

    def pre(start, col, span):
        return _round_f32(F32(0.01) + (edf - F32(start)) / F32(span) * b[col].astype(F32))

    def post(end, col, span):
        return _round_f32((F32(end) - edf) / F32(span) * b[col].astype(F32))

    conds = [ed <= 1969, (ed >= 1970) & (ed <= 1979), (ed >= 1980) & (ed <= 1989), (ed >= 1990) & (ed <= 1999),
             (ed >= 2000) & (ed <= 2009), (ed >= 2009) & (ed <= 2013), ed > 2013]
    pre_vals = [pre(1960, "Built60to69", 10) + upto("Built50to59"),
                pre(1970, "Built70to79", 10) + upto("Built60to69"),
                pre(1980, "Built80to89", 10) + upto("Built70to79"),
                pre(1990, "Built90to99", 10) + upto("Built80to89"),
                pre(2000, "Built00to09", 10) + upto("Built90to99"),
                pre(2009, "Built10to13", 4) + upto("Built00to09"),
                upto("Built10to13").astype(F32)]
    total = cum[-1]
    after = {c: total - upto(c) for c in names}  # sum of the columns after ``c``
    post_vals = [post(1970, "Built60to69", 10) + after["Built60to69"],
                 post(1980, "Built70to79", 10) + after["Built70to79"],
                 post(1990, "Built80to89", 10) + after["Built80to89"],
                 post(2000, "Built90to99", 10) + after["Built90to99"],
                 post(2010, "Built00to09", 10) + after["Built00to09"],
                 post(2014, "Built10to13", 4) + after["Built10to13"],
                 b["BuiltAfter13"].astype(F32)]
    # CASE takes the first matching branch; post's 2010-2013 branch starts at 2010, not 2009
    post_conds = list(conds)
    post_conds[5] = (ed >= 2010) & (ed <= 2013)
    out = d[["CensusBlock", "EntryDate"]].copy()
    out["PreBldg"] = np.select(conds, pre_vals, default=np.nan).astype(F32)
    out["PostBldg"] = np.select(post_conds, post_vals, default=np.nan).astype(F32)
    out["TotalBldg"] = total
    return out


def _pre_post_pct(ppb: pd.DataFrame, scheme: pd.DataFrame) -> pd.DataFrame:
    """``#GBSPrePostPct``: PrePct / PostPct per block.

    ``ROUND`` returns FLOAT in T-SQL, so these are float64 (e.g. ``PostPct = 100.1 - 37 = 63.1`` exactly as a
    double). They only become REAL when stored in ``flGBSFinal``.
    """
    x = ppb.merge(scheme[["CensusBlock", "UDPrePct"]], on="CensusBlock")
    ud = x["UDPrePct"].fillna(0).to_numpy(dtype="int64")
    has = x["TotalBldg"].to_numpy() != 0
    tot = np.where(has, x["TotalBldg"], 1).astype("float64")
    computed = sql_round(100.0 * x["PreBldg"].to_numpy("float64") / tot, 0)
    pre = np.where(ud == 0, computed, ud.astype("float64"))
    post = np.where(ud == 0, 100.1 - computed, (100 - ud).astype("float64"))
    pre = np.where(has, pre, 100.0)
    post = np.where(has, post, 0.0)
    out = x[["CensusBlock"]].copy()
    out["PrePct"], out["PostPct"] = pre, post
    return out.drop_duplicates()


def _dmg_bin(pct: np.ndarray) -> np.ndarray:
    """``DmgStrBin`` CASE (on the REAL percent): 0 → 1, (0,11) → 2, (10,21) → 3, …, (90,101) → 11; else NULL."""
    p = np.asarray(pct, dtype="float64")
    conds = [p == 0] + [(p > lo) & (p < lo + 11) for lo in range(0, 100, 10)]
    return np.select(conds, list(range(1, 12)), default=-1)


def _dist_damage(dmg: pd.DataFrame, dist: pd.DataFrame, schemes, haz_types, with_decade: bool) -> pd.DataFrame:
    """``#GBS{NonRes,Res}DMGsum``: Σ over foundation/story of dist Pct × damage (Hazus keeps the bins apart)."""
    d = dist[dist["SchemeID"].isin(schemes)]
    df = dmg[dmg["HazardTypeID"].isin(haz_types)]
    m = df.merge(d, left_on=["BlockTypeID", "OCCID", "STORYID", "FirmID", "FoundationID", "ZoneID"],
                 right_on=["BlockTypeID", "SOccupID", "StoryID", "FIRMID", "FoundationID", "HazardZoneID"])
    pct = m["Pct"].to_numpy(F32)  # REAL column
    for src, dst in (("PctDmgStr", "PctDmgStrC"), ("PctDmgCont", "PctDmgContC"), ("PctDmgInv", "PctDmgInvC")):
        # Pct * (PctDmg/100): REAL * REAL → REAL; the SUMs that follow are FLOAT
        m[dst] = (pct * (m[src].to_numpy(F32) / F32(100))).astype("float64")
    m["Pct"] = pct.astype("float64")
    m["DmgStrBin"] = _dmg_bin(m["PctDmgStr"].to_numpy(F32))
    keys = ["SchemeID", "ZoneID", "FirmID", "BlockTypeID", "HazardTypeID", "OCCID", "DepthID"]
    keys += (["DecadeID"] if with_decade else []) + ["DmgStrBin"]
    g = m.groupby(keys, as_index=False, dropna=False).agg(
        SumPctDmgStrC=("PctDmgStrC", "sum"), SumPctDmgContC=("PctDmgContC", "sum"),
        SumPctDmgInvC=("PctDmgInvC", "sum"), DistPct=("Pct", "sum"))
    return g


def _merge_calcs(nrds: pd.DataFrame, ppaw: pd.DataFrame, region: Region, with_decade: bool,
                 inventory: bool) -> pd.DataFrame:
    """``#GBS{NR,Res}MergeAll`` + ``#GBS{NR,Res}MergeCalcs``."""
    on_left = ["SchemeID", "DepthID", "HazardTypeID"] + (["DecadeID"] if with_decade else [])
    on_right = ["SchemeId", "Depth", "HazTypeID"] + (["DecadeID"] if with_decade else [])
    j = nrds.merge(ppaw, left_on=on_left, right_on=on_right, suffixes=("", "_aw"))
    keys = ["StudyCaseID", "AnalysisOptId", "ReturnPeriodID", "CensusBlock", "SchemeID", "ZoneID", "FirmID",
            "BlockTypeID", "TypeOfHaz", "OCCID", "Depth"] + (["DecadeID"] if with_decade else []) + \
           ["DmgStrBin", "PrePct", "PostPct"]
    g = j.groupby(keys, as_index=False, dropna=False).agg(
        PctAffected=("PctAffected", "sum"), PctDmgStrC=("SumPctDmgStrC", "sum"),
        PctDmgContC=("SumPctDmgContC", "sum"), PctDmgInvC=("SumPctDmgInvC", "sum"), DistPct=("DistPct", "sum"))
    if not with_decade:
        g["DecadeID"] = 0

    # Blocks must appear in every exposure table (flGBSXResults* inner joins).
    blocks = set(g["CensusBlock"])
    for name in ("hzExposureContentOccupB", "hzExposureOccupB", "hzBldgCountOccupB", "hzSqFootageOccupB",
                 "flDepExposureOccupB", "flDepExposureContentOccupB"):
        blocks &= set(_strip(region[name]["CensusBlock"]))
    g = g[g["CensusBlock"].isin(blocks)].reset_index(drop=True)

    occ = _occupancies().set_index("SortOrder")["SOccup"]
    soccup = g["OCCID"].map(occ)
    blk = g["CensusBlock"]
    sqft = _occ_column(region, "hzSqFootageOccupB", "F", blk, soccup).astype(F32).astype("float64")  # REAL column
    eob = _occ_column(region, "hzExposureOccupB", "I", blk, soccup)
    ecob = _occ_column(region, "hzExposureContentOccupB", "I", blk, soccup)
    bcob = _occ_column(region, "hzBldgCountOccupB", "I", blk, soccup)
    deob = _occ_column(region, "flDepExposureOccupB", "I", blk, soccup)
    decob = _occ_column(region, "flDepExposureContentOccupB", "I", blk, soccup)

    pre = g["PrePct"].to_numpy("float64")
    post = g["PostPct"].to_numpy("float64")
    aff = g["PctAffected"].to_numpy("float64") / 100.0
    dist = g["DistPct"].to_numpy("float64")
    s, c, inv = (g[k].to_numpy("float64") for k in ("PctDmgStrC", "PctDmgContC", "PctDmgInvC"))

    def amount(expo, share, factor):
        # Pre/PostPct are FLOAT here, so the whole product is float64
        val = expo * (share / 100.0) * aff * factor
        return np.where(share == 0, 0.0, val)

    out = g[["StudyCaseID", "AnalysisOptId", "ReturnPeriodID", "CensusBlock", "SchemeID", "ZoneID", "FirmID",
             "BlockTypeID"]].copy()
    out["HazardTypeID"] = g["TypeOfHaz"]
    for col in ("OCCID", "Depth", "DecadeID", "DistPct", "PctAffected", "PctDmgStrC", "PctDmgContC",
                "PctDmgInvC", "PrePct", "PostPct", "DmgStrBin"):
        out[col] = g[col]
    out["SqPostADmgStrC"] = amount(sqft, post, dist)
    out["SqPreADmgStrC"] = amount(sqft, pre, dist)
    out["BLDGPostADmgStrC"] = amount(eob, post, s)
    out["BLDGPreADmgStrC"] = amount(eob, pre, s)
    out["CONTDPostADmgContC"] = amount(ecob, post, c)
    out["CONTDPreADmgContC"] = amount(ecob, pre, c)
    out["CTPostADmgStrC"] = amount(bcob, post, dist)
    out["CTPreADmgStrC"] = amount(bcob, pre, dist)
    out["DEPBPostADmgStrC"] = amount(deob, post, s)
    out["DEPBPreADmgStrC"] = amount(deob, pre, s)
    out["DEPCPostADmgContC"] = amount(decob, post, c)
    out["DEPCPreADmgContC"] = amount(decob, pre, c)
    if inventory:
        psi = table("flBldgEconParamSalesAndInv")
        psi["Occupancy"] = _strip(psi["Occupancy"])
        p = pd.DataFrame({"SOccup": soccup}).merge(psi, left_on="SOccup", right_on="Occupancy", how="left")
        sales = p["AnnualSalesPerSqFt"].fillna(0).to_numpy("float64")
        invpct = p["BusinessInvPctofSales"].fillna(0).to_numpy("float64") / 100.0
        is_inv = g["OCCID"].isin(INV_OCCIDS).to_numpy()

        def inv_amount(share):
            # SqFt*(share/100)*(PctAffected/100)*PctDmgInvC*Sales*(InvPct/100), strictly left to right
            val = sqft * (share / 100.0) * aff * inv * sales * invpct
            return np.where(is_inv & (share != 0), val, 0.0)

        out["INVPostADmgInvC"] = inv_amount(post)
        out["INVPreADmgInvC"] = inv_amount(pre)
    else:
        out["INVPostADmgInvC"] = 0.0
        out["INVPreADmgInvC"] = 0.0
    return out


REAL_COLS = ["DistPct", "PctAffected", "PctDmgStrC", "PctDmgContC", "PctDmgInvC", "PrePct", "PostPct",
             "SqPostADmgStrC", "SqPreADmgStrC", "BLDGPostADmgStrC", "BLDGPreADmgStrC", "CONTDPostADmgContC",
             "CONTDPreADmgContC", "CTPostADmgStrC", "CTPreADmgStrC", "DEPBPostADmgStrC", "DEPBPreADmgStrC",
             "DEPCPostADmgContC", "DEPCPreADmgContC", "INVPostADmgInvC", "INVPreADmgInvC"]


def base_prep(region: Region, area_weighted: pd.DataFrame, dmg_final: pd.DataFrame, *, study_case_id: int = 1,
              analysis_opt_id: int = 0) -> pd.DataFrame:
    """The Hazus ``flGBSFinal`` table (``absp_GBSBasePrep``)."""
    aw_t = unpivot_area_weighted(area_weighted)
    aw_t = aw_t[aw_t["StudyCaseID"] == study_case_id]
    bt = table("flBlockType")
    ht = table("flHazType")
    aw = aw_t.merge(bt[["ID", "BlockTypeID"]].rename(columns={"ID": "BlockTypeNum", "BlockTypeID": "BT"}),
                    left_on="ControllingHazard", right_on="BT") \
        .merge(ht[["ID", "ZoneAbbr"]].rename(columns={"ID": "HazTypeID"}), left_on="CoastalZone", right_on="ZoneAbbr")
    aw = aw[aw["PctAffected"] > 0]

    scheme = region["flSchemeMapping"].copy()
    scheme["CensusBlock"] = _strip(scheme["CensusBlock"])
    scheme["SchemeId"] = _strip(scheme["SchemeId"])
    demo = region["hzDemographicsB"].copy()
    demo["CensusBlock"] = _strip(demo["CensusBlock"])

    used_schemes = set(scheme.loc[scheme["CensusBlock"].isin(aw_t["CensusBlock"]), "SchemeId"])
    used_haz = set(aw["HazTypeID"])
    dec = table("flDecadeDescription")
    myb = demo[["CensusBlock", "MedianYearBuilt"]].dropna()
    decade = myb.merge(dec, how="cross")
    decade = decade[(decade["MedianYearBuilt"] >= decade["DecBegin"]) & (decade["MedianYearBuilt"] <= decade["DecEnd"])]
    decade = decade[["CensusBlock", "DecadeID"]]

    states = {t[:2] for t in _strip(region["hzTract"]["Tract"])}
    dist_nr = table("flGBSDistNonRes")
    dist_r = table("flGBSDistRes")
    dist_nr = dist_nr[dist_nr["StateFIPS"].isin(states)]
    dist_r = dist_r[dist_r["StateFIPS"].isin(states)]

    nrds = _dist_damage(dmg_final, dist_nr, used_schemes, used_haz, with_decade=True)
    rds = _dist_damage(dmg_final, dist_r, used_schemes, used_haz, with_decade=False)

    # pre/post-FIRM split per block (only blocks in the study case, demographics and scheme mapping)
    ppb = _pre_post_bldg(demo[demo["CensusBlock"].isin(aw_t["CensusBlock"])], scheme)
    ppp = _pre_post_pct(ppb, scheme)

    typeofhaz = aw["HazTypeID"].map({1: 1, 2: 2, 3: 2, 4: 2, 5: 5, 6: 6, 7: 7})
    base = aw.assign(TypeOfHaz=typeofhaz)[["StudyCaseID", "AnalysisOptId", "ReturnPeriodID", "TypeOfHaz",
                                           "CensusBlock", "Depth", "HazTypeID", "PctAffected"]]
    base = base[(base["StudyCaseID"] == study_case_id) & (base["AnalysisOptId"] == analysis_opt_id)]
    base = base.merge(ppp, on="CensusBlock").merge(scheme[["CensusBlock", "SchemeId"]], on="CensusBlock")
    ppaw_nr = base.merge(decade, on="CensusBlock").drop_duplicates()
    ppaw_r = base.drop_duplicates()

    nr = _merge_calcs(nrds, ppaw_nr, region, with_decade=True, inventory=True)
    r = _merge_calcs(rds, ppaw_r, region, with_decade=False, inventory=False)
    final = pd.concat([nr, r], ignore_index=True)
    for col in REAL_COLS:
        final[col] = final[col].astype(F32)
    final["DmgStrBin"] = final["DmgStrBin"].where(final["DmgStrBin"] >= 0)
    return final


# ---------------------------------------------------------------------------------- downstream results

def _control_hazard(h: pd.Series) -> pd.Series:
    return h.map({1: "R", 5: "L", 6: "M", 7: "H"}).fillna("C")


def _with_soccup(final: pd.DataFrame) -> pd.DataFrame:
    occ = _occupancies()
    return final.merge(occ, left_on="OCCID", right_on="SortOrder")


def _binned(final: pd.DataFrame, pre_col: str, post_col: str, prefix: str) -> pd.DataFrame:
    """Shared shape of ``absp_GBSBldgContDmg`` / ``absp_GBSDmgCounts``: per-bin sums by pre/post FIRM."""
    f = _with_soccup(final)
    f = f[f["FirmID"].isin([1, 2])]
    val = np.where(f["FirmID"] == 1, f[pre_col].astype("float64"), f[post_col].astype("float64"))
    out = f[["StudyCaseId" if "StudyCaseId" in f else "StudyCaseID", "ReturnPeriodID", "CensusBlock", "SOccup",
             "HazardTypeID", "FirmID", "AnalysisOptId"]].copy()
    out.columns = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "SOccup", "HazardTypeID", "FirmID", "AnalysisOptId"]
    for k, name in enumerate(DMG_BINS, start=1):
        out[f"{prefix}{name}"] = np.where(f["DmgStrBin"].to_numpy() == k, val, 0.0)
    keys = ["StudyCaseId", "ReturnPeriodId", "HazardTypeID", "CensusBlock", "SOccup", "FirmID", "AnalysisOptId"]
    g = out.groupby(keys, as_index=False).sum()
    g["ControlHazard"] = _control_hazard(g["HazardTypeID"])
    g["Pre_PostFirm"] = (g["FirmID"] != 1).astype(int)
    return g.drop(columns=["HazardTypeID", "FirmID"])


def _goccup(df: pd.DataFrame, value_cols: list[str], *, extra_keys=()) -> pd.DataFrame:
    keys = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "GenOccup", "Pre_PostFirm", "AnalysisOptId", *extra_keys]
    return df.assign(GenOccup=df["SOccup"].str[:3]).groupby(keys, as_index=False)[value_cols].sum()


def _gbldgtype(df: pd.DataFrame, value_cols: list[str], region: Region, *, as_int: bool,
               types: list[tuple[str, str]] = BLDG_TYPES) -> pd.DataFrame:
    """``absp_GBSFR*GBldgType``: ``0.01 * round(Σ value × typePct, -2)`` per general building type."""
    cb = region["hzCensusBlock"][["CensusBlock", "BldgSchemesId"]].copy()
    cb["CensusBlock"] = _strip(cb["CensusBlock"])
    gs = region["hzGenBldgScheme"].copy()
    gs["Occupancy"] = _strip(gs["Occupancy"])
    j = df.merge(cb, on="CensusBlock").merge(gs, left_on=["BldgSchemesId", "SOccup"],
                                              right_on=["BldgSchemesId", "Occupancy"])
    keys = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "Pre_PostFirm", "AnalysisOptId"]
    parts = []
    for name, pct in types:
        w = j[keys].copy()
        for c in value_cols:
            v = j[c].to_numpy("int64" if as_int else "float64") * j[pct].to_numpy("int64")
            w[c] = v
        s = w.groupby(keys, as_index=False)[value_cols].sum()
        for c in value_cols:
            s[c] = 0.01 * sql_round(s[c].to_numpy("float64"), -2)
        s["BldgType"] = name
        parts.append(s)
    return pd.concat(parts, ignore_index=True)


def bldg_cont_dmg(final: pd.DataFrame, region: Region) -> dict[str, pd.DataFrame]:
    """``flFRGBSDmgSOccupB`` (square feet per damage bin) and its rollups."""
    cols = [f"SqFt{b}" for b in DMG_BINS]
    soccup = _binned(final, "SqPreADmgStrC", "SqPostADmgStrC", "SqFt")
    return {"flFRGBSDmgSOccupB": soccup,
            "flFRGBSDmgGOccupB": _goccup(soccup, cols),
            "flFRGBSDmgGBldgType": _gbldgtype(soccup, cols, region, as_int=False)}


def dmg_counts(final: pd.DataFrame, region: Region) -> dict[str, pd.DataFrame]:
    """``flFRGBSDmgCountSOccupB`` (building counts per damage bin, INT) and its rollups."""
    cols = [f"Count{b}" for b in DMG_BINS]
    soccup = _binned(final, "CTPreADmgStrC", "CTPostADmgStrC", "Count")
    for c in cols:
        soccup[c] = sql_int(soccup[c])
    return {"flFRGBSDmgCountSOccupB": soccup,
            "flFRGBSDmgCountGOccupB": _goccup(soccup, cols),
            "flFRGBSDmgCountGBldgType": _gbldgtype(soccup, cols, region, as_int=True)}


def base_tab(final: pd.DataFrame, region: Region) -> pd.DataFrame:
    """``absf_GBSBaseTabT``: exposure, content exposure, count, depreciated values and sq ft per block × occupancy."""
    blocks = pd.Series(sorted(set(final["CensusBlock"])), name="CensusBlock")
    for name in ("hzExposureOccupB", "hzExposureContentOccupB", "hzBldgCountOccupB", "flDepExposureOccupB",
                 "flDepExposureContentOccupB", "hzSqFootageOccupB"):
        blocks = blocks[blocks.isin(set(_strip(region[name]["CensusBlock"])))]
    occ = _occupancies()
    x = pd.DataFrame({"CensusBlock": blocks}).merge(occ[["SOccup"]], how="cross")
    x["Exposure"] = _occ_column(region, "hzExposureOccupB", "I", x["CensusBlock"], x["SOccup"])
    x["ExpCont"] = _occ_column(region, "hzExposureContentOccupB", "I", x["CensusBlock"], x["SOccup"])
    x["BldgCnt"] = _occ_column(region, "hzBldgCountOccupB", "I", x["CensusBlock"], x["SOccup"])
    x["DepExp"] = _occ_column(region, "flDepExposureOccupB", "I", x["CensusBlock"], x["SOccup"])
    x["DepExpCont"] = _occ_column(region, "flDepExposureContentOccupB", "I", x["CensusBlock"], x["SOccup"])
    x["SqFt"] = _occ_column(region, "hzSqFootageOccupB", "F", x["CensusBlock"], x["SOccup"]).astype(F32)
    return x


def restoration_by_depth() -> pd.DataFrame:
    """``absf_GBSRsFnGBST``: maximum restoration days per occupancy for each depth band (literal port)."""
    rs = table("flRsFnGBS")
    rs["SOccup"] = _strip(rs["SOccup"])
    mn, mx, days = rs["Min_Depth"], rs["Max_Depth"], rs["Max_Restor_Days"]

    def part(mask, valmask, name):
        p = rs.loc[mask, ["SOccup"]].copy()
        p[name] = np.where(valmask[mask], days[mask], np.nan)
        return p

    neg = part(mx == 0, mx == 0, "Neg")
    u1 = part(mn == 0, (mn == 0) & mx.isin([1, 4, 24]), "UpTo1")
    m4 = ((mn == 1) & (mx == 24)) | ((mn == 0) & (mx == 4)) | ((mn == 0) & (mx == 24))
    u4 = part(m4, m4, "UpTo4")
    m8 = ((mn == 1) & (mx == 24)) | ((mn == 4) & (mx == 8)) | ((mn == 0) & (mx == 24))
    u8 = part(m8, m8, "UpTo8")
    m12 = ((mn == 1) & (mx == 24)) | ((mn == 8) & (mx == 12)) | ((mn == 8) & (mx == 24)) | ((mn == 0) & (mx == 24))
    u12 = part(m12, m12, "UpTo12")
    u24 = part(mx == 24, mx == 24, "UpTo24")
    out = neg
    for p in (u1, u4, u8, u12, u24):
        out = out.merge(p, on="SOccup")
    return out


def econ_loss(final: pd.DataFrame, region: Region) -> dict[str, pd.DataFrame]:
    """``flFRGBSEcLossBySOccup`` (INT, $ thousands) and its rollups (``absp_GBSEconLoss``)."""
    cls = _occupancies()
    params = table("flBldgEconParamRecaptureFactors")
    for name in ("flBldgEconParamRental", "flBldgEconParamWageCapitalIncome", "flBldgEconParamOwnerOccupied"):
        params = params.merge(table(name), on="Occupancy")
    params["Occupancy"] = _strip(params["Occupancy"])
    params = cls.merge(params, left_on="SOccup", right_on="Occupancy").merge(restoration_by_depth(), on="SOccup")
    f = final.merge(params, left_on="OCCID", right_on="SortOrder")
    tab = base_tab(final, region)
    f = f.merge(tab[["CensusBlock", "SOccup", "SqFt"]], on=["CensusBlock", "SOccup"])

    depth = f["Depth"].to_numpy()
    restoration = np.select([depth < 0, depth < 100, depth < 400, depth < 800, depth < 1200],
                            [f["Neg"], f["UpTo1"], f["UpTo4"], f["UpTo8"], f["UpTo12"]],
                            default=f["UpTo24"]).astype("float64")
    sqft = f["SqFt"].to_numpy(F32)
    dist = f["DistPct"].to_numpy(F32)
    aff = f["PctAffected"].to_numpy(F32)
    oo = f["PctOwnerOccupied"].to_numpy("float64")
    disr = f["DisruptionCostsPerSqFt"].to_numpy("float64")
    rent = f["RentalCostsPerSqFtPerDay"].to_numpy("float64")
    inc_rc = f["PctIncomeRecapture"].to_numpy("float64")
    wage_rc = f["PctWageRecapture"].to_numpy("float64")
    out_rc = f["PctOutputRecapture"].to_numpy("float64")
    inc = f["IncomePerSqftPerDay"].to_numpy("float64")
    wage = f["WagePerSqftDay"].to_numpy("float64")
    outp = f["OutputPerSqftDay"].to_numpy("float64")
    emp = f["EmployeesPerSqft"].to_numpy("float64")

    rows = []
    for firm, share_col, sq_col, prefix in ((1, "PrePct", "SqPreADmgStrC", "Pre"), (2, "PostPct", "SqPostADmgStrC", "Post")):
        m = (f["FirmID"] == firm).to_numpy()
        share = f[share_col].to_numpy(F32)
        sqd = f[sq_col].to_numpy(F32)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(dist != 0, (sqd / dist).astype(F32), F32(0))
        lead = (sqft * dist * F32(0.01) * aff * F32(0.01) * share).astype("float64")  # REAL chain
        lead2 = (sqft * dist * F32(0.01) * aff).astype("float64")  # EmploymentLoss omits the share factor
        has = dist > 0
        reloc = np.where(ratio > 0.1, lead * ((1 - 0.01 * oo) * disr + 0.01 * oo * (disr + rent * restoration)), 0.0)
        income = np.where(has, lead * (1 - inc_rc) * inc * restoration, 0.0)
        rental = np.where(ratio > 0.1, lead * (1 - 0.01 * oo) * rent * restoration, 0.0)
        wage_l = np.where(has, lead * (1 - wage_rc) * wage * restoration, 0.0)
        output = np.where(has, lead * (1 - out_rc) * outp * restoration, 0.0)
        out_test = lead * (1 - out_rc) * outp * restoration
        with np.errstate(divide="ignore", invalid="ignore"):
            employ = np.where(has & (out_test > 0) & (emp > 0) & (outp > 0),
                              lead2 * (1 - out_rc) * outp * restoration * emp / (outp * 365) / 100.0, 0.0)
        part = pd.DataFrame({
            "StudyCaseId": f.loc[m, "StudyCaseID"].to_numpy(), "ReturnPeriodId": f.loc[m, "ReturnPeriodID"].to_numpy(),
            "CensusBlock": f.loc[m, "CensusBlock"].to_numpy(), "SOccup": f.loc[m, "SOccup"].to_numpy(),
            "ControlHazard": _control_hazard(f.loc[m, "HazardTypeID"]).to_numpy(),
            "Pre_PostFirm": 0 if firm == 1 else 1,
            "BuildingLoss": f.loc[m, f"BLDG{prefix}ADmgStrC"].to_numpy("float64"),
            "ContentsLoss": f.loc[m, f"CONTD{prefix}ADmgContC"].to_numpy("float64"),
            "InventoryLoss": f.loc[m, f"INV{prefix}ADmgInvC"].to_numpy("float64"),
            "RelocationCost": reloc[m], "IncomeLoss": income[m], "RentalIncomeLoss": rental[m],
            "WageLoss": wage_l[m], "OutputLoss": output[m], "EmploymentLoss": employ[m],
            "AnalysisOptId": f.loc[m, "AnalysisOptId"].to_numpy()})
        rows.append(part)
    u = pd.concat(rows, ignore_index=True)
    keys = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "SOccup", "ControlHazard", "Pre_PostFirm", "AnalysisOptId"]
    soccup = u.groupby(keys, as_index=False)[ECON_COLS].sum()
    for c in ECON_COLS:
        soccup[c] = sql_int(soccup[c])
    # cap building and contents losses at the (INT) exposure
    capped = soccup.merge(tab[["CensusBlock", "SOccup", "Exposure", "ExpCont"]], on=["CensusBlock", "SOccup"])
    capped["BuildingLoss"] = np.where(capped["BuildingLoss"] > capped["Exposure"], capped["Exposure"],
                                      capped["BuildingLoss"]).astype("int64")
    capped["ContentsLoss"] = np.where(capped["ContentsLoss"] > capped["ExpCont"], capped["ExpCont"],
                                      capped["ContentsLoss"]).astype("int64")
    soccup = capped.drop(columns=["Exposure", "ExpCont"])

    goccup = _goccup(soccup, ECON_COLS)
    total = goccup.groupby(["StudyCaseId", "ReturnPeriodId", "CensusBlock", "Pre_PostFirm", "AnalysisOptId"],
                           as_index=False)[ECON_COLS].sum()
    return {"flFRGBSEcLossBySOccup": soccup, "flFRGBSEcLossByGOccup": goccup, "flFRGBSEcLossByTotal": total,
            "flFRGBSEcLossByGBldgType": _gbldgtype(soccup, ECON_COLS, region, as_int=True)}


def dep_econ_loss(final: pd.DataFrame, region: Region) -> dict[str, pd.DataFrame]:
    """``flFRGBSDepEcLossBySOccup`` (depreciated building/contents loss, INT) and rollups."""
    f = _with_soccup(final)
    parts = []
    for firm, prefix in ((1, "Pre"), (2, "Post")):
        m = f[f["FirmID"] == firm]
        parts.append(pd.DataFrame({
            "StudyCaseId": m["StudyCaseID"], "ReturnPeriodId": m["ReturnPeriodID"], "CensusBlock": m["CensusBlock"],
            "ControlHazard": _control_hazard(m["HazardTypeID"]), "SOccup": m["SOccup"],
            "Pre_PostFirm": 0 if firm == 1 else 1,
            "BuildingLoss": m[f"DEPB{prefix}ADmgStrC"].astype("float64"),
            "ContentsLoss": m[f"DEPC{prefix}ADmgContC"].astype("float64"),
            "AnalysisOptId": m["AnalysisOptId"]}))
    u = pd.concat(parts, ignore_index=True)
    keys = ["StudyCaseId", "ReturnPeriodId", "CensusBlock", "ControlHazard", "SOccup", "Pre_PostFirm", "AnalysisOptId"]
    soccup = u.groupby(keys, as_index=False)[["BuildingLoss", "ContentsLoss"]].sum()
    for c in ("BuildingLoss", "ContentsLoss"):
        soccup[c] = sql_int(soccup[c])
    cols = ["BuildingLoss", "ContentsLoss"]
    goccup = _goccup(soccup, cols)
    total = goccup.groupby(["StudyCaseId", "ReturnPeriodId", "CensusBlock", "Pre_PostFirm", "AnalysisOptId"],
                           as_index=False)[cols].sum()
    return {"flFRGBSDepEcLossBySOccup": soccup, "flFRGBSDepEcLossByGOccup": goccup,
            "flFRGBSDepEcLossByTotal": total, "flFRGBSDepEcLossByGBldgType": _gbldgtype(soccup, cols, region, as_int=True)}


def run_gbs(region: Region, area_weighted: pd.DataFrame, haz_type: int, *, study_case_id: int = 1,
            analysis_opt_id: int = 0, ffe_set: int = 0) -> dict[str, pd.DataFrame]:
    """All Hazus GBS result tables for one study case (``absp_GBSDoIt`` with ``@AnalysisBits = 15``)."""
    dmg = build_dmg_final(haz_type, ffe_set)
    final = base_prep(region, area_weighted, dmg, study_case_id=study_case_id, analysis_opt_id=analysis_opt_id)
    out = {"flGBSFinal": final}
    out.update(bldg_cont_dmg(final, region))
    out.update(econ_loss(final, region))
    out.update(dmg_counts(final, region))
    out.update(dep_econ_loss(final, region))
    return out
