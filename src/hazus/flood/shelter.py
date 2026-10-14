"""Displaced population and short-term shelter needs (Hazus 7.2 flood).

Port of ``absp_GBSInsertAW`` (its ``flAnShelterResult`` part) and of ``absp_SHRDoIt`` →
``absp_SHRCalcFactors`` + ``absp_SHRConstParam``. The SQL differs from the Flood TM (Eq. 10-1 … 10-3):

* The 7.2 data has weights ``IW = 1.0`` and ``AW = 0.0``, not the TM's 0.8 / 0.2, so only income matters.
* The working table's columns get their types from a first ``SELECT … 0 AS col INTO``. Most of them are
  therefore INT, and the income terms (``Inc_k / Households × factor × Population``) are **truncated** to
  integers.
* The income terms use the full block population, not the displaced share. The result is then capped at
  the displaced population.
* The ``IncOver40`` term is not multiplied by population, so after truncation it is always 0.
* In a riverine-and-coastal case a block has two ``flAnShelterResult`` rows (R and C). Population uses
  their summed area share, while the demographic terms are counted once per row.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd

from hazus.data import table

F32 = np.float32
AGE_CLASSES = ["MaleLess18", "Male18to64", "MaleOver64", "FemaleLess18", "Female18to64", "FemaleOver64"]
INC_CLASSES = ["IncLess10", "Inc10to20", "Inc20to30", "Inc30to40", "IncOver40"]


def shelter_result(area_weighted_long: pd.DataFrame, *, study_case_id: int, return_period: str,
                   control_hazard: str, analysis_opt_id: int = 0) -> pd.DataFrame:
    """``flAnShelterResult`` rows written by ``absp_GBSInsertAW``: ``PctArea = Σ PctAffected / 100`` per block.

    ``area_weighted_long`` is the output of :func:`hazus.flood.exposure.area_weighted_depths` for one depth
    grid.
    """
    g = area_weighted_long.groupby("CensusBlock", as_index=False)["PctAffected"].sum()
    return pd.DataFrame({"CensusBlock": g["CensusBlock"], "AnalysisOptId": analysis_opt_id,
                         "ReturnPeriodID": return_period, "StudyCaseId": study_case_id, "CensusArea": 0.0,
                         "AffectArea": 0.0, "PctArea": g["PctAffected"] / 100.0, "ControlHazard": control_hazard})


def run_shelter(region: Mapping[str, pd.DataFrame], shelter_results: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """``flFRShelter`` (plus ``flAnShrPopulation`` and ``flAnShrfactors``) from ``flAnShelterResult`` rows."""
    demo = region["hzDemographicsB"].copy()
    demo["CensusBlock"] = demo["CensusBlock"].astype(str).str.rstrip()
    sr = shelter_results.copy()
    sr["CensusBlock"] = sr["CensusBlock"].astype(str).str.rstrip()
    keys = ["CensusBlock", "AnalysisOptId", "ReturnPeriodID", "StudyCaseId"]
    rolled = sr.groupby(keys, as_index=False).agg(PctArea=("PctArea", "sum"))

    pop = demo.merge(rolled, on="CensusBlock")
    population = pd.DataFrame({
        "StudyCaseId": pop["StudyCaseId"], "CensusBlock": pop["CensusBlock"], "ReturnPeriodID": pop["ReturnPeriodID"],
        "AnalysisOptId": pop["AnalysisOptId"],
        "Population": pop["Population"].fillna(0).to_numpy("float64") * pop["PctArea"].to_numpy("float64"),
        "HouseHolds": pop["Households"].fillna(0).to_numpy("float64") * pop["PctArea"].to_numpy("float64")})

    params = table("flShelterParams")
    params["ParamClass"] = params["ParamClass"].astype(str).str.rstrip()
    factor = dict(zip(params["ParamClass"], params["Factors"].astype("float64")))

    d = demo.merge(sr[["CensusBlock", "StudyCaseId", "ReturnPeriodID", "AnalysisOptId", "PctArea"]], on="CensusBlock")
    hh = d["Households"].fillna(0).to_numpy("float64")
    p = d["Population"].fillna(0).to_numpy("float64")
    pct = d["PctArea"].to_numpy("float64")

    def col(name):
        return d[name].fillna(0).to_numpy("float64")

    def share(num):  # ISNULL(x * 1.0 / NULLIF(Households, 0), 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(hh != 0, num / np.where(hh != 0, hh, 1), 0.0)

    base = d[["StudyCaseId", "ReturnPeriodID", "CensusBlock", "AnalysisOptId"]]
    zeros = np.zeros(len(d))
    rows = []

    def add(**values):
        r = base.copy()
        for c in AGE_CLASSES + INC_CLASSES:
            r[c] = values.get(c, zeros)
        rows.append(r)

    # First SELECT INTO fixes the column types: MaleLess18 and FemaleLess18 FLOAT, everything else INT.
    add(MaleLess18=col("MaleLess18") * factor["AM1"] * pct, FemaleLess18=col("FemaleLess18") * factor["AM1"] * pct)
    add(Male18to64=np.trunc(col("Male18to64") * factor["AM2"]), Female18to64=np.trunc(col("Female18to64") * factor["AM2"]))
    add(MaleOver64=np.trunc(col("MaleOver64") * factor["AM3"]), FemaleOver64=np.trunc(col("FemaleOver64") * factor["AM3"]))
    for k, (cls, src) in enumerate(zip(INC_CLASSES[:4], ["IncLess10", "Inc10to20", "Inc20to30", "Inc30to40"]), start=1):
        add(**{cls: np.trunc(share(col(src)) * factor[f"IM{k}"] * p)})
    over40 = col("Inc40to50") + col("Inc50to60") + col("Inc60to75") + col("Inc75to100") + col("IncOver100")
    add(IncOver40=np.trunc(share(over40) * factor["IM5"]))
    demog = pd.concat(rows, ignore_index=True)

    f = demog.merge(population[["StudyCaseId", "ReturnPeriodID", "CensusBlock", "AnalysisOptId", "Population"]],
                    on=["StudyCaseId", "ReturnPeriodID", "CensusBlock", "AnalysisOptId"])
    popv = f["Population"].to_numpy("float64")
    factors = f[["StudyCaseId", "ReturnPeriodID", "CensusBlock", "AnalysisOptId"]].copy()
    with np.errstate(divide="ignore", invalid="ignore"):
        for c in AGE_CLASSES + INC_CLASSES:
            factors[c] = np.where(popv != 0, f[c].to_numpy("float64") / np.where(popv != 0, popv, 1), 0.0)

    iw = float(F32(factor["IW"]))  # @IW, @AW are declared REAL
    aw = float(F32(factor["AW"]))
    factors["_term"] = factors[AGE_CLASSES].sum(axis=1) * aw + factors[INC_CLASSES].sum(axis=1) * iw
    over = factors.groupby(["CensusBlock", "ReturnPeriodID", "StudyCaseId", "AnalysisOptId"], as_index=False) \
        .agg(OverAllFactors=("_term", "sum"))
    fin = over.merge(population, on=["CensusBlock", "StudyCaseId", "ReturnPeriodID", "AnalysisOptId"])
    disp = fin["Population"].to_numpy("float64")
    need = disp * fin["OverAllFactors"].to_numpy("float64")
    dp = _round(disp)
    stn = _round(need)
    shelter = pd.DataFrame({"StudyCaseId": fin["StudyCaseId"], "ReturnPeriodId": fin["ReturnPeriodID"],
                            "CensusBlock": fin["CensusBlock"], "DisplacedPop": dp,
                            "ShortTermNeeds": np.where(stn > dp, dp, stn), "AnalysisOptId": fin["AnalysisOptId"]})
    return {"flFRShelter": shelter, "flAnShrPopulation": population, "flAnShrfactors": factors.drop(columns="_term")}


def _round(x: np.ndarray) -> np.ndarray:
    """T-SQL ROUND(float, 0): half away from zero."""
    return np.sign(x) * np.floor(np.abs(x) + 0.5)
