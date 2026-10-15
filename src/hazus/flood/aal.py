"""Average annualized loss (AAL) for the general building stock.

Port of the coefficient loop in ``Hazus.Logic.Flood.AnalysisLogic.RunAsync`` (stored in ``flReturnPeriod``)
and of ``absp_AALDoIt`` (Hazus 7.2).

For each block × occupancy × hazard × pre/post-FIRM, the return periods present are ordered from longest to
shortest. The longest contributes ``coef × loss``; every other one contributes
``coef × (loss + loss_of_next_longer) / 2``. The sum is rounded half away from zero to an integer.
Coefficients are ``NUMERIC(18,8)`` and the arithmetic is exact decimal, as in SQL Server.

Hazus quirk kept on purpose: the AAL "Wood" general-building-type row uses the *concrete* share (``CPct``),
so it equals the "Concrete" row. Pass ``wood_uses_concrete_pct=False`` to use ``WPct`` instead.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, getcontext
from fractions import Fraction
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

getcontext().prec = 38

ECON_COLS = ["BuildingLoss", "ContentsLoss", "InventoryLoss", "RelocationCost", "IncomeLoss",
             "RentalIncomeLoss", "WageLoss", "OutputLoss", "EmploymentLoss"]


def aal_coefficients(return_periods: Iterable[int]) -> dict[int, float]:
    """Exceedance-probability band width per return period (as floats).

    Return periods are processed from longest to shortest: ``coef(RP) = 1/RP − 1/RP_next_longer``, and the
    longest uses 0 for the previous probability. Example: ``{500, 100, 50, 25, 10}`` →
    ``{500: .002, 100: .008, 50: .01, 25: .02, 10: .06}``. Hazus only computes AAL with more than two return
    periods.
    """
    return {rp: float(c) for rp, c in aal_coefficients_decimal(return_periods).items()}


def aal_coefficients_decimal(return_periods: Iterable[int]) -> dict[int, Decimal]:
    """The coefficients exactly as stored in ``flReturnPeriod.Coefficient`` (``NUMERIC(18,8)``)."""
    coefs: dict[int, Decimal] = {}
    previous = Decimal(0)
    for rp in sorted(set(return_periods), reverse=True):
        p = Decimal(1) / Decimal(rp)  # C# decimal arithmetic
        coefs[rp] = (p - previous).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        previous = p
    return coefs


def aal_by_soccup(ecloss_soccup: pd.DataFrame, coefficients: Mapping[int, Decimal]) -> pd.DataFrame:
    """``flFRGBSEcLossBySOccup`` rows with ``ReturnPeriodId = 'Annual'``.

    The sum is exact: with coefficients ``C / L`` (``L`` their common denominator), ``2 L × AAL`` is the
    integer ``Σ 2 C v`` (longest period) ``+ Σ C (v + v_longer)``, rounded half away from zero by integer
    division. Python integers take over if int64 could overflow.
    """
    rp = pd.to_numeric(ecloss_soccup["ReturnPeriodId"].astype(str).str.strip(), errors="coerce")
    df = ecloss_soccup[rp.isin(list(coefficients))].assign(ReturnPeriod=rp.astype("Int64"))
    part_keys = ["StudyCaseId", "CensusBlock", "ControlHazard", "SOccup", "Pre_PostFirm", "AnalysisOptId"]
    columns = part_keys + ["ReturnPeriodId"] + ECON_COLS
    df = df.groupby(part_keys + ["ReturnPeriod"], as_index=False)[ECON_COLS].sum()
    if df.empty:
        return pd.DataFrame(columns=columns)

    # groups in order of first appearance after sorting by return period, longest period first within each
    df = df.sort_values("ReturnPeriod", ascending=False)
    gid = df.groupby(part_keys, sort=False).ngroup().to_numpy()
    periods = df["ReturnPeriod"].to_numpy("int64")
    order = np.lexsort((-periods, gid))
    gid, periods = gid[order], periods[order]
    values = df[ECON_COLS].to_numpy("int64")[order]

    fractions = {int(k): Fraction(v) for k, v in coefficients.items()}
    denominator = math.lcm(*(f.denominator for f in fractions.values()))
    scaled = {k: f.numerator * (denominator // f.denominator) for k, f in fractions.items()}
    bound = 2 * int(np.abs(values).max(initial=0)) * sum(abs(c) for c in scaled.values()) + denominator
    dtype = "int64" if bound < 2 ** 62 else object
    values = values.astype(dtype)
    coef = np.array([scaled[int(p)] for p in periods], dtype=dtype)[:, None]

    first = np.r_[True, gid[1:] != gid[:-1]]
    longer = np.roll(values, 1, axis=0)
    longer[first] = values[first]  # the longest period counts twice: 2 C v
    starts = np.flatnonzero(first)
    totals = np.add.reduceat(coef * (values + longer), starts, axis=0)
    magnitude = (np.abs(totals) + denominator) // (2 * denominator)
    rounded = np.where(totals >= 0, magnitude, -magnitude).astype("int64")

    out = df.iloc[order[starts]][part_keys].reset_index(drop=True).assign(ReturnPeriodId="Annual")
    for j, col in enumerate(ECON_COLS):
        out[col] = rounded[:, j]
    return out[columns]


def aal_gbs(ecloss_soccup: pd.DataFrame, coefficients: Mapping[int, Decimal], region: Mapping[str, pd.DataFrame],
            *, wood_uses_concrete_pct: bool = True) -> dict[str, pd.DataFrame]:
    """The ``'Annual'`` rows that ``absp_AALDoIt`` adds to the four economic-loss result tables."""
    from hazus.flood.gbs import BLDG_TYPES, _gbldgtype, _goccup

    soccup = aal_by_soccup(ecloss_soccup, coefficients)
    goccup = _goccup(soccup, ECON_COLS)
    total = goccup.groupby(["StudyCaseId", "ReturnPeriodId", "CensusBlock", "Pre_PostFirm", "AnalysisOptId"],
                           as_index=False)[ECON_COLS].sum()
    bt = _gbldgtype(soccup, ECON_COLS, region, as_int=True,
                    types=[("Wood", "CPct" if wood_uses_concrete_pct else "WPct")] + BLDG_TYPES[1:])
    return {"flFRGBSEcLossBySOccup": soccup, "flFRGBSEcLossByGOccup": goccup, "flFRGBSEcLossByTotal": total,
            "flFRGBSEcLossByGBldgType": bt}
