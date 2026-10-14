"""Essential-facility and utility analysis over inventory tables, producing the ``flFR*`` result tables.

Port of the essential-facility and utility loops in ``Hazus.Logic.Flood.AnalysisLogic.RunAsync`` (Hazus 7.2)
and of the damage-function lookups they make (``flGetDamageXRefIdEssntFltyStruct``,
``flGetDamageXRefIdEssntFltyCont`` and ``flGetDamageXRefIdUtilFlty``).

For every facility in the base (``hz*``) table and every return period of the scenario:

1. The depth is the maximum over that return period's depth grids at the facility point. The maximum starts at
   0.0 with hazard "R", so a facility outside every grid is evaluated at depth 0
   (:func:`hazus.flood.hazard.max_depth_at_points`).
2. The damage function is the one whose id is in the flood (``fl*``) table. If no function has that id, the
   default for the facility class comes from the xref table. The flag column is ``HazardR`` for riverine,
   ``HazardCV`` for coastal deeper than 6 ft and ``HazardCA`` otherwise, among rows that are ``Selected`` or
   ``DefaultFn``. That depth is the flood depth, not the depth above the first floor.
3. Damage and loss come from :mod:`hazus.flood.facilities`. Lookups are made in the same order as the C#, so
   a facility that returns early (dry, or protected) never needs its damage functions.

Where Hazus would stop with an unhandled exception, this module raises :class:`LookupError` naming the facility.
That happens when the flood-table row, the damage function (after the fallback), the content-to-structure
ratio or the utility functional depth is missing, or when the fallback finds more than one function.
The one exception is the essential-facility restoration row. It is missing whenever the depth above the first
floor is below -4 ft (a dry facility with a first floor more than 4 ft up). Hazus crashes there; this port
keeps the initial values (1 day to full restoration, functional depth 0).

Hazus 7.2 never sets ``AnalysisOptId`` on these results, so it is always 0.

Flood attributes (first-floor or equipment height, damage-function ids, flood protection) come from the flood
table. A region without one gets the class defaults that Hazus assigns to new facilities
(:func:`flood_defaults`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from hazus.flood.facilities import (
    _protected,
    ef_occupancy,
    essential_facility_damage,
    utility_damage,
)


@dataclass(frozen=True)
class FacilityKind:
    base: str  # hz table: class, cost, location
    flood: str  # fl table: heights, damage-function ids, flood protection
    result: str  # flFR result table
    id_col: str


ESSENTIAL_FACILITIES = {
    "care": FacilityKind("hzCareFlty", "flCareFlty", "flFRCareFlty", "CareFltyId"),
    "police": FacilityKind("hzPoliceStation", "flPoliceStation", "flFRPoliceStation", "PoliceStationId"),
    "fire": FacilityKind("hzFireStation", "flFireStation", "flFRFireStation", "FireStationId"),
    "emergency": FacilityKind("hzEmergencyCtr", "flEmergencyCtr", "flFREmergencyCtr", "EocId"),
    "school": FacilityKind("hzSchool", "flSchool", "flFRSchool", "SchoolId"),
}
UTILITIES = {
    "potable_water": FacilityKind("hzPotableWaterFlty", "flPotableWaterFlty", "flFRPotableWaterFlty",
                                  "PotableWaterFltyId"),
    "waste_water": FacilityKind("hzWasteWaterFlty", "flWasteWaterFlty", "flFRWasteWaterFlty", "WasteWaterFltyId"),
    "oil": FacilityKind("hzOilFlty", "flOilFlty", "flFROilFlty", "OilFltyId"),
    "natural_gas": FacilityKind("hzNaturalGasFlty", "flNaturalGasFlty", "flFRNaturalGasFlty", "NaturalGasFltyId"),
    "electric_power": FacilityKind("hzElectricPowerFlty", "flElectricPowerFlty", "flFRElectricPowerFlty",
                                   "ElectricPowerFltyId"),
}


def _strip(v) -> str:
    return "" if v is None or v is pd.NA or (isinstance(v, float) and v != v) else str(v).strip()


def _parse_id(v) -> int | None:
    """``long.Parse`` of a damage-function id. A null or unparsable id is treated as "not found"."""
    try:
        return int(_strip(v))
    except ValueError:
        return None


def _functions(df: pd.DataFrame, id_col: str) -> dict[int, dict[str, float]]:
    cols = [c for c in df.columns if c.startswith("ft")]
    return {int(i): dict(zip(cols, row)) for i, row in zip(df[id_col], df[cols].itertuples(index=False))}


class FacilityDamageFunctions:
    """Facility damage functions, their default assignments and the facility parameters.

    The Hazus 7.2 tables in :mod:`hazus.data` are used by default. ``tables`` overrides any of them by name,
    for example a study region's copy of an xref table with different ``Selected`` flags.
    """

    def __init__(self, tables: Mapping[str, pd.DataFrame] | None = None):
        from hazus import data

        tables = tables or {}

        def get(name: str) -> pd.DataFrame:
            return tables[name] if name in tables else data.table(name)

        self._fns = {
            "struct": _functions(get("flEssntFltyStructDmgFn"), "BldgDmgFnID"),
            "cont": _functions(get("flEssntFltyContDmgFn"), "ContDmgFnId"),
            "util": _functions(get("flUtilFltyDmgFn"), "UtilFltyDmgFnID"),
        }
        self._xrefs = {
            "struct": (get("flEssntFltyStructDmgXRef"), "BldgDmgFnId"),
            "cont": (get("flEssntFltyContDmgXRef"), "ContDmgFnId"),
            "util": (get("flUtilFltyDmgXRef"), "UtilFltyDmgFnId"),
        }
        rs = get("flRsFnEssntFlty")
        self._restoration = rs.assign(EfClass=rs["EfClass"].map(_strip))
        details = get("flUtilFltyDmgUnionDetails")
        self._functional_depth = {_strip(o): d for o, d in zip(details["Occupancy"], details["Fnctdepth"])}
        ratios = get("hzPctContentOfStructureValue")
        self._content_ratio = {_strip(o): p for o, p in zip(ratios["Occupancy"], ratios["ContentValPct"])}

    def default_id(self, kind: str, hazard: str, depth: float, occupancy: str) -> int | None:
        """The ``flGetDamageXRefId*`` procedure: the xref default for an occupancy, hazard and depth."""
        xref, id_col = self._xrefs[kind]
        flag = "HazardR" if hazard == "R" else "HazardCV" if hazard == "C" and depth > 6 else "HazardCA"
        rows = xref[(xref["SOccupId"].map(_strip) == _strip(occupancy)) & xref[flag].astype(bool)
                    & (xref["Selected"].astype(bool) | xref["DefaultFn"].astype(bool))]
        if len(rows) > 1:  # Dapper's QuerySingleOrDefault throws here
            raise LookupError(f"{len(rows)} default {kind} damage functions for {occupancy!r} ({flag})")
        return int(rows[id_col].iloc[0]) if len(rows) else None

    def function(self, kind: str, fn_id, hazard: str, depth: float, occupancy: str) -> dict[str, float] | None:
        """The function with id ``fn_id``, else the xref default (``kind`` is "struct", "cont" or "util")."""
        fns = self._fns[kind]
        fn = fns.get(_parse_id(fn_id))
        if fn is None:
            fn = fns.get(self.default_id(kind, hazard, depth, occupancy))
        return fn

    def restoration(self, ef_class: str, flooded_depth: float) -> dict | None:
        """``flRsFnEssntFlty`` row with ``Min_Depth <= flooded_depth < Max_Depth`` as ``MaxDays``/``FunctionalDepth``."""
        rs = self._restoration
        rows = rs[(rs["EfClass"] == _strip(ef_class)) & (rs["Min_Depth"] <= flooded_depth)
                  & (rs["Max_Depth"] > flooded_depth)]
        if rows.empty:
            return None
        r = rows.iloc[0]
        days = r["Max_Restor_Days"]
        fd = r["FunctionalDepth"]
        return {"MaxDays": None if pd.isna(days) else int(days),
                "FunctionalDepth": None if pd.isna(fd) else float(np.float32(fd))}

    def functional_depth(self, utility_class: str) -> float | None:
        return self._functional_depth.get(_strip(utility_class))

    def content_ratio_pct(self, occupancy: str) -> float | None:
        return self._content_ratio.get(_strip(occupancy))


def flood_defaults(kind: FacilityKind, base: pd.DataFrame) -> pd.DataFrame:
    """Flood-table rows with the class defaults from ``<flood table>Dflt``, as the ``abst_INSfl*`` insert
    triggers assign them to new facilities. Classes without a default row get nulls.

    The Hazus utility triggers do not join the updated table: adding one utility facility resets *every* row
    of that utility's flood table to the new facility's class defaults, and a multi-row insert assigns them in
    arbitrary order. This function gives each facility its own class defaults, which is what the essential
    facility triggers do and what the utility triggers evidently intend.
    """
    from hazus import data

    utility = kind in UTILITIES.values()
    class_col = "UtilFcltyClass" if utility else "EfClass"
    dflt = data.table(f"{kind.flood}Dflt")
    dflt_class = next(c for c in dflt.columns if c.lower() == class_col.lower())
    dflt = dflt.assign(_class=dflt[dflt_class].map(_strip)).drop(columns=dflt_class)
    out = pd.DataFrame({kind.id_col: base[kind.id_col].map(_strip), "_class": base[class_col].map(_strip)})
    out = out.merge(dflt, on="_class", how="left").drop(columns="_class")
    if utility:  # isnull(def.FoundationType, 7)
        out["FoundationType"] = out["FoundationType"].where(out["FoundationType"].notna(), "7")
    return out


def _flood_rows(flood: pd.DataFrame, id_col: str) -> dict[str, dict]:
    col = next((c for c in flood.columns if c.lower() == id_col.lower()), None)  # flEmergencyCtr has "Eocid"
    if col is None:
        raise KeyError(f"flood table has no {id_col} column")
    return {_strip(i): r for i, r in zip(flood[col], flood.to_dict("records"))}


def _float32(v) -> float:
    return 0.0 if v is None or pd.isna(v) else float(np.float32(v))


def _int(v) -> int:
    return 0 if v is None or pd.isna(v) else int(v)


def _cost(v) -> float:
    return 0.0 if v is None or pd.isna(v) else float(v)


def _rp_number(return_period: str) -> int:
    return 0 if return_period == "mix0" else int(return_period)


def essential_facility_results(kind: FacilityKind, base: pd.DataFrame, flood: pd.DataFrame,
                               depth: Iterable[float], hazard: Iterable[str], *, return_period: str = "-1",
                               study_case_id: int = 1,
                               functions: FacilityDamageFunctions | None = None) -> pd.DataFrame:
    """Result rows (``flFRCareFlty`` etc.) for one essential-facility table.

    ``depth`` and ``hazard`` are the point depth (ft) and controlling hazard ("R"/"C") of each row of ``base``.
    """
    functions = functions or FacilityDamageFunctions()
    flood_by_id = _flood_rows(flood, kind.id_col)
    rp = _rp_number(return_period)
    rows = []
    for (fid, ef_class, cost), d, h in zip(base[[kind.id_col, "EfClass", "Cost"]].itertuples(index=False),
                                          depth, hazard):
        d = float(d)
        out = {"StudyCaseId": study_case_id, "ReturnPeriodId": return_period, kind.id_col: _strip(fid),
               "ControlHazard": h}
        if d < 0:
            res = essential_facility_damage(d, first_floor_ht=0.0, cost=0.0, structure_ddf={}, content_ddf={},
                                            content_ratio_pct=0.0, restoration=None)
        else:
            fl = flood_by_id.get(_strip(fid))
            if fl is None:
                raise LookupError(f"{kind.base} {fid!r} has no row in {kind.flood}")
            d = min(d, 24.0)
            ffh = _float32(fl.get("FirstFloorHt"))
            protection = _int(fl.get("FloodProtection"))
            restoration = functions.restoration(ef_class, d - ffh)
            structure = content = {}
            ratio = 0.0
            if not _protected(protection, rp):
                structure = functions.function("struct", fl.get("BldgDamageFnId"), h, d, ef_class)
                if structure is None:
                    raise LookupError(f"no structure damage function for {kind.base} {fid!r} ({_strip(ef_class)})")
                content = functions.function("cont", fl.get("ContDamageFnId"), h, d, ef_class)
                if content is None:
                    raise LookupError(f"no content damage function for {kind.base} {fid!r} ({_strip(ef_class)})")
                ratio = functions.content_ratio_pct(ef_occupancy(_strip(ef_class)))
                if ratio is None:
                    raise LookupError(f"no content-to-structure ratio for EF class {_strip(ef_class)!r}")
            res = essential_facility_damage(d, first_floor_ht=ffh, cost=_cost(cost), structure_ddf=structure,
                                            content_ddf=content, content_ratio_pct=float(ratio),
                                            restoration=restoration, flood_protection=protection,
                                            return_period=rp)
        rows.append({**out, "BldgDmgPct": res.bldg_dmg_pct, "BldgLoss": res.bldg_loss,
                     "ContDmgPct": res.cont_dmg_pct, "ContLoss": res.cont_loss,
                     "Functionality": res.functionality, "MaxTimetoFullRestoration": res.max_days_to_restoration,
                     "AnalysisOptId": 0})
    cols = ["StudyCaseId", "ReturnPeriodId", kind.id_col, "ControlHazard", "BldgDmgPct", "BldgLoss", "ContDmgPct",
            "ContLoss", "Functionality", "MaxTimetoFullRestoration", "AnalysisOptId"]
    return pd.DataFrame(rows, columns=cols).astype({"MaxTimetoFullRestoration": "Int64"})


def utility_results(kind: FacilityKind, base: pd.DataFrame, flood: pd.DataFrame, depth: Iterable[float],
                    hazard: Iterable[str], *, return_period: str = "-1", study_case_id: int = 1,
                    functions: FacilityDamageFunctions | None = None) -> pd.DataFrame:
    """Result rows (``flFRPotableWaterFlty`` etc.) for one utility table. See :func:`essential_facility_results`."""
    functions = functions or FacilityDamageFunctions()
    flood_by_id = _flood_rows(flood, kind.id_col)
    rp = _rp_number(return_period)
    rows = []
    for (fid, cls, cost), d, h in zip(base[[kind.id_col, "UtilFcltyClass", "Cost"]].itertuples(index=False),
                                     depth, hazard):
        d = float(d)
        if d < 0:
            res = utility_damage(d, equipment_ht=0.0, cost=0.0, ddf={}, functional_depth=0.0)
        else:
            fl = flood_by_id.get(_strip(fid))
            if fl is None:
                raise LookupError(f"{kind.base} {fid!r} has no row in {kind.flood}")
            d = min(d, 24.0)
            functional_depth = functions.functional_depth(cls)
            if functional_depth is None:
                raise LookupError(f"no functional depth for utility class {_strip(cls)!r}")
            protection = _int(fl.get("FloodProtection"))
            ddf = {}
            if not _protected(protection, rp):
                ddf = functions.function("util", fl.get("UtilDamageFnId"), h, d, cls)
                if ddf is None:
                    raise LookupError(f"no damage function for {kind.base} {fid!r} ({_strip(cls)})")
            res = utility_damage(d, equipment_ht=_float32(fl.get("EquipmentHt")), cost=_cost(cost), ddf=ddf,
                                 functional_depth=float(functional_depth), flood_protection=protection,
                                 return_period=rp)
        rows.append({"StudyCaseId": study_case_id, "ReturnPeriodId": return_period, kind.id_col: _strip(fid),
                     "DmgPct": res.dmg_pct, "Loss": res.loss, "Functionality": res.functionality,
                     "AnalysisOptId": 0})
    cols = ["StudyCaseId", "ReturnPeriodId", kind.id_col, "DmgPct", "Loss", "Functionality", "AnalysisOptId"]
    return pd.DataFrame(rows, columns=cols)


def facility_points(base: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Facility x/y in EPSG:4326: the ``geometry`` column if there is one, else ``Longitude``/``Latitude``."""
    if "geometry" in base.columns:
        geom = base["geometry"]
        return np.asarray([g.x for g in geom], dtype="float64"), np.asarray([g.y for g in geom], dtype="float64")
    return base["Longitude"].astype("float64").to_numpy(), base["Latitude"].astype("float64").to_numpy()


def run_facilities(region: Mapping[str, pd.DataFrame], rasters, *, study_case_id: int = 1,
                   essential: bool = True, utilities: bool = True,
                   functions: FacilityDamageFunctions | None = None) -> dict[str, pd.DataFrame]:
    """Essential-facility and utility result tables for a scenario.

    ``region`` maps Hazus table names (``hzCareFlty``, ``flCareFlty``, ...) to inventory tables. Kinds without
    a base table are skipped, and a missing flood table is replaced by :func:`flood_defaults`. ``rasters`` are :class:`~hazus.flood.hazard.FloodRaster` depth grids. Each
    return period gets its own rows, named by :func:`~hazus.flood.hazard.return_period_ids`.
    """
    from hazus.flood.hazard import max_depth_at_points, return_period_ids

    rasters = list(rasters)
    functions = functions or FacilityDamageFunctions()
    ids = return_period_ids(rasters) if rasters else {}

    kinds = []
    if essential:
        kinds += [(k, essential_facility_results) for k in ESSENTIAL_FACILITIES.values()]
    if utilities:
        kinds += [(k, utility_results) for k in UTILITIES.values()]

    out: dict[str, pd.DataFrame] = {}
    for kind, fn in kinds:
        if kind.base not in region:
            continue
        base = region[kind.base]
        flood = region[kind.flood] if kind.flood in region else flood_defaults(kind, base)
        xs, ys = facility_points(base)
        parts = [fn(kind, base.iloc[:0], flood, [], [], study_case_id=study_case_id, functions=functions)]
        for period in sorted(ids):
            grids = [r for r in rasters if int(r.return_period) == period]
            depth, hazard = max_depth_at_points(grids, xs, ys)
            parts.append(fn(kind, base, flood, depth, hazard, return_period=ids[period],
                            study_case_id=study_case_id, functions=functions))
        out[kind.result] = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    return out
