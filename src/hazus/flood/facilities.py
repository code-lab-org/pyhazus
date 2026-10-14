"""Site-specific flood damage: essential facilities, utility facilities, user-defined facilities.

Port of ``Hazus.Logic.Flood.AnalysisLogic.ComputeEFDamage``, ``ComputeUtilityDamage`` and ``ComputeUDFDamage``
(Hazus 7.2). Damage functions are passed in as mappings from Hazus depth columns (``ft04m`` … ``ft24``)
to percent damage. Looking them up in the Hazus tables is the caller's job.

Hazus behaviours reproduced on purpose (see ``docs/flood-dataflow.md``):

* Depth is capped at 24 ft. Interpolation is linear between the floor and ceiling whole-foot columns.
  Below -4 ft it clamps to ``ft04m``. Utility functions clamp at ``ft10``.
* ``functionality`` mirrors the Hazus ``Functionality`` column exactly. It starts as ``True`` (also for
  unflooded and flood-protected facilities) and is then set to ``flooded_depth > functional_depth``, which is
  ``True`` when the facility is *non*-functional by the Technical Manual's definition. Callers that want the
  TM meaning should derive it from ``flooded_depth``.
* If the facility's flood protection (in years) exceeds the scenario return period, there is no damage.
* User-defined facilities report the *structure* damage percentage as the content damage percentage
  (``udf_content_uses_structure_pct``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

DamageFunction = Mapping[str, float]

MAX_DEPTH_FT = 24.0


def depth_column(i: int, *, clamp_high: int | None = None) -> str:
    """Hazus damage-function column for whole-foot depth ``i`` (e.g. -2 → ``ft02m``, 3 → ``ft03``, 12 → ``ft12``)."""
    if clamp_high is not None and i >= clamp_high:
        return f"ft{clamp_high:02d}"
    if 0 <= i < 10:
        return f"ft0{i}"
    if -4 <= i < 0:
        return f"ft0{abs(i)}m"
    if i < -4:
        return "ft04m"
    return f"ft{i}"


def interpolate(ddf: DamageFunction, depth: float, *, clamp_high: int | None = None) -> float:
    """Percent damage at ``depth`` ft by linear interpolation between whole-foot columns."""
    lo, hi = math.floor(depth), math.ceil(depth)
    v_hi = float(ddf[depth_column(hi, clamp_high=clamp_high)])
    if lo == hi:
        return v_hi
    v_lo = float(ddf[depth_column(lo, clamp_high=clamp_high)])
    return v_lo + (v_hi - v_lo) * (depth - lo) / (hi - lo)


def _protected(flood_protection: int | None, return_period: int) -> bool:
    return return_period > 0 and (flood_protection or 0) > return_period


@dataclass
class EssentialFacilityResult:
    bldg_dmg_pct: float = 0.0
    bldg_loss: float = 0.0
    cont_dmg_pct: float = 0.0
    cont_loss: float = 0.0
    functionality: bool = True  # raw Hazus value, see module docstring
    flooded_depth: float | None = None
    max_days_to_restoration: int = 1


def essential_facility_damage(depth: float, *, first_floor_ht: float, cost: float,
                              structure_ddf: DamageFunction, content_ddf: DamageFunction,
                              content_ratio_pct: float, restoration: Mapping[str, float] | None,
                              flood_protection: int | None = None,
                              return_period: int = 0) -> EssentialFacilityResult:
    """Damage to one essential facility.

    ``restoration`` is the row of the EF restoration table with
    ``MinDepth <= depth - first_floor_ht < MaxDepth``. It supplies ``MaxDays`` and ``FunctionalDepth``.
    ``content_ratio_pct`` is the content-to-structure value ratio of the facility's occupancy
    (see :func:`ef_occupancy`).
    """
    result = EssentialFacilityResult()
    if depth < 0:
        return result
    depth = min(depth, MAX_DEPTH_FT)
    flooded = depth - first_floor_ht
    result.flooded_depth = flooded
    functional_depth = 0.0
    if restoration is not None:
        result.max_days_to_restoration = int(restoration["MaxDays"])
        functional_depth = float(restoration.get("FunctionalDepth") or 0.0)
    if _protected(flood_protection, return_period):
        return result
    result.functionality = flooded > functional_depth
    pct = interpolate(structure_ddf, flooded)
    result.bldg_dmg_pct = pct
    result.bldg_loss = pct * cost / 100.0
    cpct = interpolate(content_ddf, flooded)
    result.cont_dmg_pct = cpct
    result.cont_loss = cpct * cost * (content_ratio_pct / 100.0) / 100.0
    return result


@dataclass
class UtilityResult:
    dmg_pct: float = 0.0
    loss: float = 0.0
    functionality: bool = True  # raw Hazus value, see module docstring
    flooded_depth: float | None = None


def utility_damage(depth: float, *, equipment_ht: float, cost: float, ddf: DamageFunction,
                   functional_depth: float, flood_protection: int | None = None,
                   return_period: int = 0) -> UtilityResult:
    """Damage to one utility facility (the damage function clamps at ``ft10``).

    Hazus 7.2 uses the equipment height as stored. It is not rounded as the Flood TM (Eq. 9-1) describes.
    """
    result = UtilityResult()
    if depth < 0:
        return result
    depth = min(depth, MAX_DEPTH_FT)
    flooded = depth - equipment_ht
    result.flooded_depth = flooded
    if _protected(flood_protection, return_period):
        return result
    result.functionality = flooded > functional_depth
    pct = interpolate(ddf, flooded, clamp_high=10)
    result.dmg_pct = pct
    result.loss = pct * cost / 100.0
    return result


@dataclass
class UserDefinedFacilityResult:
    bldg_dmg_pct: float = 0.0
    bldg_loss: float = 0.0
    cont_dmg_pct: float = 0.0
    cont_loss: float = 0.0
    inv_loss: float = 0.0


def user_defined_facility_damage(depth: float, *, first_floor_ht: float, cost: float, content_cost: float,
                                 area_sqft: float, structure_ddf: DamageFunction,
                                 content_ddf: DamageFunction | None, content_ratio_pct: float,
                                 annual_sales_per_sqft: float | None = None,
                                 business_inv_pct_of_sales: float | None = None,
                                 flood_protection: int | None = None, return_period: int = 0,
                                 udf_content_uses_structure_pct: bool = True) -> UserDefinedFacilityResult:
    """Damage to one user-defined facility.

    With ``udf_content_uses_structure_pct=True`` (the Hazus 7.2 behaviour) the content loss and content
    percentage use the *structure* damage percentage, and ``content_ddf`` is ignored.
    """
    result = UserDefinedFacilityResult()
    if depth < 0:
        return result
    depth = min(depth, MAX_DEPTH_FT)
    flooded = depth - first_floor_ht
    if _protected(flood_protection, return_period):
        return result
    pct = interpolate(structure_ddf, flooded)
    result.bldg_dmg_pct = pct
    result.bldg_loss = pct * cost / 100.0
    cpct = pct if udf_content_uses_structure_pct or content_ddf is None else interpolate(content_ddf, flooded)
    if not content_cost:
        result.cont_loss = cpct * cost * (content_ratio_pct / 100.0) / 100.0
    else:
        result.cont_loss = cpct * content_cost / 100.0
    result.cont_dmg_pct = cpct
    if annual_sales_per_sqft is not None:
        result.inv_loss = pct / 100.0 * annual_sales_per_sqft * area_sqft * (business_inv_pct_of_sales or 0.0)
    return result


_EF_OCCUPANCY = {
    "EFHS": "COM6", "EFHM": "COM6", "EFHL": "COM6", "MDFLT": "COM6",
    "EFMC": "COM7",
    "EFEO": "GOV2", "EFFS": "GOV2", "EFPS": "GOV2", "EDFLT": "GOV2", "FDFLT": "GOV2", "PDFLT": "GOV2",
    "EFS1": "EDU1", "SDFLT": "EDU1",
    "EFS2": "EDU2",
}


def ef_occupancy(ef_class: str) -> str:
    """Occupancy used for an essential facility's content-to-structure ratio (``GetOccupancyEF``)."""
    return _EF_OCCUPANCY.get(ef_class.strip(), "")


def udf_specific_occupancy_key(occupancy: str, num_stories: int | None, foundation_type: str) -> tuple[str, str, str, bool]:
    """(table, occupancy, stories label, has_basement) used to resolve a UDF's specific occupancy.

    The tables are the Hazus flood specific-occupancy tables (``Res3``, ``Res12``, ``NonRes``).
    Foundation type "4" is a basement.
    """
    has_basement = foundation_type == "4"
    occ = occupancy.strip()
    n = num_stories or 0
    if occ[:4] == "RES3":
        label = "1to2 Stories" if 1 <= n <= 2 else "3to4 Stories" if 3 <= n <= 4 else "5Plus Stories"
        return "Res3", occ, label, has_basement
    if occ in ("RES1", "RES2"):
        label = {1: "1 Story", 2: "2 Story", 255: "Split Level"}.get(n, "3 Story")
        return "Res12", occ, label, has_basement
    label = "Low Rise" if 1 <= n <= 3 else "Mid Rise" if 4 <= n <= 7 else "High Rise"
    return "NonRes", occ, label, has_basement
