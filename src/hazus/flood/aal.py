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

from decimal import ROUND_HALF_UP, Decimal, getcontext
from fractions import Fraction
from typing import Iterable, Mapping

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


def _round_half_away(x: Fraction) -> int:
    n = abs(x)
    r = int(n + Fraction(1, 2))
    return r if x >= 0 else -r


def aal_by_soccup(ecloss_soccup: pd.DataFrame, coefficients: Mapping[int, Decimal]) -> pd.DataFrame:
    """``flFRGBSEcLossBySOccup`` rows with ``ReturnPeriodId = 'Annual'``."""
    df = ecloss_soccup.copy()
    rp = pd.to_numeric(df["ReturnPeriodId"].astype(str).str.strip(), errors="coerce")
    df = df[rp.isin(list(coefficients))].assign(ReturnPeriod=rp.astype("Int64"))
    part_keys = ["StudyCaseId", "CensusBlock", "ControlHazard", "SOccup", "Pre_PostFirm", "AnalysisOptId"]
    df = df.groupby(part_keys + ["ReturnPeriod"], as_index=False)[ECON_COLS].sum()
    coef = {int(k): Fraction(v) for k, v in coefficients.items()}

    rows = []
    for key, g in df.sort_values("ReturnPeriod", ascending=False).groupby(part_keys, sort=False):
        totals = dict.fromkeys(ECON_COLS, Fraction(0))
        prev = None
        for r in g.itertuples(index=False):
            c = coef[int(r.ReturnPeriod)]
            for col in ECON_COLS:
                v = int(getattr(r, col))
                totals[col] += c * v if prev is None else c * (v + int(getattr(prev, col))) / 2
            prev = r
        rows.append({**dict(zip(part_keys, key)), "ReturnPeriodId": "Annual",
                     **{col: _round_half_away(totals[col]) for col in ECON_COLS}})
    return pd.DataFrame(rows, columns=part_keys + ["ReturnPeriodId"] + ECON_COLS)


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
