"""Average annualized loss (AAL) return-period coefficients.

Port of the coefficient loop in ``Hazus.Logic.Flood.AnalysisLogic.RunAsync``. The coefficients are
written to ``ReturnPeriodData`` before ``absp_AALDoIt`` runs.
"""

from __future__ import annotations

from typing import Iterable


def aal_coefficients(return_periods: Iterable[int]) -> dict[int, float]:
    """Exceedance-probability band width per return period.

    Return periods are processed from longest to shortest:
    ``coef(RP) = 1/RP - 1/RP_next_longer`` (the longest uses 0).
    Example: ``{500, 100, 50, 25, 10}`` → ``{500: .002, 100: .008, 50: .01, 25: .02, 10: .06}``.

    Hazus only computes AAL when more than two return periods are present.
    """
    coefs: dict[int, float] = {}
    previous = 0.0
    for rp in sorted(set(return_periods), reverse=True):
        p = 1.0 / rp
        coefs[rp] = p - previous
        previous = p
    return coefs
