"""Study regions from a state inventory: port of the Hazus 7.2 study-region aggregation.

Hazus builds a study region entirely in T-SQL. ``syHazus..AggregateByCounty`` (or ``…ByTract``,
``…ByCensusBlock``) runs ``AggregateAll`` in a copy of the region template, followed by the boundary step and
``AggregatePostProcess``. This module produces the tables the flood model reads (see
``hazus.inventory.sqlserver.FLOOD_TABLES``) from a state inventory exported with
``hazus.inventory.sqlserver.export_state``:

* **Selection.** Counties → their tracts → their blocks, unless tracts or blocks are given explicitly
  (``AggregateAll``).
* **Copies.** Every copied table is converted to the region template's column types, as the aggregation's
  INSERTs do (:func:`to_region_types`). For example, the state's float exposure becomes ``int`` (truncated).
  County, tract and block tables are filtered by the selection. Building schemes are those named
  by the selected tracts. The flood mapping-scheme definitions (``flSchemeInfo``, ``flSchemeRiverine``,
  ``flSchemeCoastal``) are copied for the whole state. Facilities are filtered by tract.
* **Facility flood attributes.** The ``abst_INSfl*`` insert triggers replace them with the facility class's
  defaults (:func:`hazus.flood.facility_analysis.flood_defaults`). Classes without defaults keep the state's
  values. The Hazus utility triggers assign defaults in arbitrary order (see ``docs/flood-dataflow.md``);
  here every utility gets its own class's defaults.
* **Post-processing.** ``absp_StudyRgnCreationPostProcessScheme``: a null or 0 ``EntryDate`` becomes 2009.
  ``absp_CLBlocks``: the scheme of a coastal (C) or Great Lakes (L) block gets that letter as its third
  character. ``absp_BldgDepExpOccupB`` / ``absp_ContDepExpOccupB``: depreciated exposure (see
  :func:`depreciated_exposure`).
* **Boundary.** ``AggregateStudyRegionBoundary``: the union of the selected counties, tracts or TIGER blocks,
  ``Reduce(0.00005)`` (Douglas-Peucker), then ``STBuffer(0.00001)``.

The depreciated exposure depends on the year the region is created. Hazus uses
``DATEPART(year, GETDATE()) - MedianYearBuilt`` as the building age.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd

from hazus.data import table
from hazus.flood.facility_analysis import ESSENTIAL_FACILITIES, UTILITIES, FacilityKind, flood_defaults
from hazus.flood.gbs import sql_round

BLOCK_TABLES = ["hzDemographicsB", "hzExposureOccupB", "hzExposureContentOccupB", "hzBldgCountOccupB",
                "hzSqFootageOccupB"]
STATE_WIDE_TABLES = ["flSchemeInfo", "flSchemeRiverine", "flSchemeCoastal"]
OCCUPANCIES = ["RES1", "RES2", "RES3A", "RES3B", "RES3C", "RES3D", "RES3E", "RES3F", "RES4", "RES5", "RES6",
               "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "COM10",
               "IND1", "IND2", "IND3", "IND4", "IND5", "IND6", "AGR1", "REL1", "GOV1", "GOV2", "EDU1", "EDU2"]
# hzRES1ConstructionWt.SortOrder by IncomeRatio band (absp_BldgDepExpOccupB); bounds compared as REAL
INCOME_BANDS = [(0.0, 0.5, 1), (0.5, 0.85, 2), (0.85, 1.25, 3), (1.25, 2.0, 4), (2.0, None, 5)]


INT_TYPES = {"int", "smallint", "tinyint", "bigint"}
DECIMAL_TYPES = {"decimal", "numeric", "money", "smallmoney"}
TEXT_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}


def _to_decimal_scale(values: pd.Series, scale: int) -> pd.Series:
    """Round to ``scale`` decimals half away from zero. ``repr`` recovers the decimal the float came from."""
    q = Decimal(1).scaleb(-scale)
    out = [None if v is None or v != v else float(Decimal(repr(float(v))).quantize(q, ROUND_HALF_UP))
           for v in values]
    return pd.Series(out, index=values.index, dtype="float64")


def _convert(values: pd.Series, sql_type: str, scale: int) -> pd.Series:
    """One column as SQL Server converts it on INSERT into a column of ``sql_type``."""
    if sql_type in TEXT_TYPES:
        return values.astype("string").str.rstrip()
    if sql_type in INT_TYPES:  # float/numeric -> int truncates toward zero
        x = pd.to_numeric(values, errors="coerce").astype("float64")
        return pd.Series(np.trunc(x), index=values.index).astype("Int64")
    if sql_type == "bit":
        x = pd.to_numeric(values, errors="coerce")
        return (x != 0).astype("boolean").where(x.notna())
    if sql_type == "real":
        x = pd.to_numeric(values, errors="coerce").astype("float64")
        return x.astype(np.float32).astype("float64")
    if sql_type == "float":
        return pd.to_numeric(values, errors="coerce").astype("float64")
    if sql_type in DECIMAL_TYPES:
        return _to_decimal_scale(pd.to_numeric(values, errors="coerce"), scale)
    return values


def _computed_columns(df: pd.DataFrame, schema: pd.DataFrame) -> pd.DataFrame:
    """Add the template's computed columns. In the flood tables they are all sums of other columns
    (``([RES1I]+[RES2I])+…``); NULL in any term gives NULL, as in SQL."""
    import re

    computed = schema[schema["is_computed"].astype(bool)].sort_values("column_id")
    lower = {c.lower(): c for c in df.columns}  # SQL identifiers are case-insensitive ([EDU2i])
    for col, sql_type, definition in computed[["column", "type", "definition"]].itertuples(index=False):
        terms = [lower.get(t.lower()) for t in re.findall(r"\[(\w+)\]", definition or "")]
        if not terms or re.sub(r"[\[\]\w+() ]", "", definition) or None in terms:
            continue  # not a plain sum of available columns
        if sql_type == "real":  # real + real stays real: float32 after every addition
            total = df[terms[0]].astype("float32")
            for term in terms[1:]:
                total = (total + df[term].astype("float32")).astype("float32")
            df[col] = total.astype("float64")
        else:
            total = df[terms[0]]
            for term in terms[1:]:
                total = total + df[term]
            df[col] = total
    order = [c for c in schema.sort_values("column_id")["column"] if c in df.columns]
    return df[order]


def to_region_types(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """A state table with the region template's columns and types (column names matched case-insensitively).

    Columns the template does not have are dropped, and computed columns are evaluated
    (:func:`_computed_columns`). Geometry is kept as ``geometry``.
    """
    schema = table("_region_schema")
    schema = schema[schema["table"].str.lower() == name.lower()]
    s = schema[~schema["is_computed"].astype(bool)]
    if s.empty:
        return df.reset_index(drop=True)
    source = {c.lower(): c for c in df.columns}
    out = {}
    for col, sql_type, scale in s[["column", "type", "scale"]].itertuples(index=False):
        if sql_type in ("geometry", "geography"):
            continue
        src = source.get(col.lower())
        if src is not None:
            out[col] = _convert(df[src].reset_index(drop=True), sql_type, int(scale))
    result = _computed_columns(pd.DataFrame(out), schema)
    if "geometry" in df.columns:
        import geopandas as gpd

        return gpd.GeoDataFrame(result, geometry=df.geometry.reset_index(drop=True), crs=df.crs)
    return result


def _strip(s: pd.Series) -> pd.Series:
    return s.astype("string").str.rstrip()


def _concat_states(states: Mapping[str, pd.DataFrame] | Sequence[Mapping[str, pd.DataFrame]]) -> dict:
    """One inventory from one or more state inventories (later states never replace earlier rows)."""
    if isinstance(states, Mapping):
        return dict(states)
    out: dict[str, pd.DataFrame] = {}
    for st in states:
        for name, df in st.items():
            out[name] = df if name not in out else pd.concat([out[name], df], ignore_index=True)
    return out


def select(state: Mapping[str, pd.DataFrame], counties: Iterable[str] | None = None,
           tracts: Iterable[str] | None = None, blocks: Iterable[str] | None = None) -> tuple[set, set, set]:
    """County, tract and block ids of the region, as ``AggregateAll`` derives them."""
    counties, tracts, blocks = (None if ids is None else [str(i) for i in ids] for ids in (counties, tracts, blocks))
    blocks = {b.strip() for b in blocks} if blocks else None
    tracts = {t.strip() for t in tracts} if tracts else ({b[:11] for b in blocks} if blocks else None)
    counties = {c.strip() for c in counties} if counties else ({t[:5] for t in tracts} if tracts else None)
    if counties is None:
        counties = set(_strip(state["hzCounty"]["CountyFips"]))
    if tracts is None:
        t = state["hzTract"]
        tracts = set(_strip(t["Tract"])[_strip(t["CountyFips"]).isin(counties)])
    if blocks is None:
        b = state["hzCensusBlock"]
        blocks = set(_strip(b["CensusBlock"])[_strip(b["Tract"]).isin(tracts)])
    return counties, tracts, blocks


def _filter(df: pd.DataFrame, column: str, ids: set, name: str | None = None) -> pd.DataFrame:
    """Rows whose ``column`` is in ``ids``, converted to the region types of table ``name`` if given."""
    rows = df[_strip(df[column]).isin(ids)].reset_index(drop=True)
    return to_region_types(name, rows) if name else rows


def _dedupe(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """``NOT EXISTS`` inserts: for a multi-state region, the first state's row wins."""
    k = df[keys].apply(lambda s: _strip(s) if s.dtype == object or str(s.dtype).startswith("string") else s)
    return df[~k.duplicated()].reset_index(drop=True)


def study_region_boundary(shapes) -> object:
    """``AggregateStudyRegionBoundary``: ``UnionAggregate(Shape).Reduce(0.00005).STBuffer(0.00001)``."""
    import shapely

    union = shapely.union_all(np.asarray(shapes))
    return shapely.simplify(union, 0.00005, preserve_topology=False).buffer(0.00001)


def depreciated_exposure(blocks: pd.DataFrame, demographics: pd.DataFrame, exposure: pd.DataFrame,
                         year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``flDepExposureOccupB`` and ``flDepExposureContentOccupB`` (``absp_BldgDepExpOccupB``,
    ``absp_ContDepExpOccupB``).

    A block is depreciated if it has demographics, exposure, an income ratio of at least 0 and a building
    age ``year - MedianYearBuilt`` between 0 and 100 (the rows of ``flDepFunction``). Building values are
    ``ROUND((100 - dep%) / 100 × value, 0)``. RES1 mixes the Good / Average / Poor curves by the income-ratio
    weights in ``hzRES1ConstructionWt``. Content values are ``building × ContentValPct / 100`` in integer
    arithmetic, so they are truncated.
    """
    cb = blocks[["CensusBlock", "IncomeRatio"]].assign(CensusBlock=_strip(blocks["CensusBlock"]))
    db = demographics[["CensusBlock", "MedianYearBuilt"]].assign(CensusBlock=_strip(demographics["CensusBlock"]))
    ob = exposure.assign(CensusBlock=_strip(exposure["CensusBlock"]))
    j = cb.merge(db, on="CensusBlock").merge(ob, on="CensusBlock")

    ir = j["IncomeRatio"].to_numpy("float64").astype(np.float32)
    sort = np.zeros(len(j), dtype="int64")
    for lo, hi, order in INCOME_BANDS:
        m = ir >= np.float32(lo)
        if hi is not None:
            m &= ir < np.float32(hi)
        sort[m] = order
    j = j.assign(SortOrder=sort)[sort > 0]
    j = j.assign(Age=year - j["MedianYearBuilt"].astype("float64"))
    dep = table("flDepFunction")
    dep.columns = [c if c in ("Age",) else c.upper() for c in dep.columns]
    j = j.merge(dep.astype({"Age": "float64"}), on="Age")
    wt = table("hzRes1ConstructionWt")
    j = j.merge(wt[["SortOrder", "LuxuryGood", "CustomGood", "AverageAverage", "EconomyPoor"]], on="SortOrder")

    out = pd.DataFrame({"CensusBlock": j["CensusBlock"].to_numpy()})
    f32 = lambda c: j[c].to_numpy("float64").astype(np.float32)  # noqa: E731 (REAL weights)
    good_w = (f32("LuxuryGood") + f32("CustomGood")).astype("float64")
    mix = (f32("AverageAverage").astype("float64") * j["RES1AVERAGE"].to_numpy("float64")
           + good_w * j["RES1GOOD"].to_numpy("float64")
           + f32("EconomyPoor").astype("float64") * j["RES1POOR"].to_numpy("float64"))
    factors = {"RES1": 1 - mix / 100}
    for occ in OCCUPANCIES[1:]:
        factors[occ] = (100 - j[occ].to_numpy("float64")) / 100
    for occ in OCCUPANCIES:
        value = pd.to_numeric(j[f"{occ}I"], errors="coerce").to_numpy("float64")
        out[f"{occ}I"] = pd.array(sql_round(factors[occ] * value, 0), dtype="Float64").astype("Int64")

    pct = dict(zip(_strip(table("hzPctContentOfStructureValue")["Occupancy"]),
                   table("hzPctContentOfStructureValue")["ContentValPct"]))
    content = out[["CensusBlock"]].copy()
    for occ in OCCUPANCIES:
        v = out[f"{occ}I"]
        p = pct.get(occ)
        content[f"{occ}I"] = (v * int(p)) // 100 if p is not None else pd.array([pd.NA] * len(v), dtype="Int64")
    return out, content


def _facility_tables(state: Mapping[str, pd.DataFrame], kind: FacilityKind, tracts: set) -> dict:
    if kind.base not in state:
        return {}
    base = state[kind.base]
    base = base[_strip(base["Tract"]).isin(tracts)] if tracts else base
    base = _dedupe(to_region_types(kind.base, base), [kind.id_col])
    defaults = flood_defaults(kind, base)
    if kind.flood in state:
        own = to_region_types(kind.flood, state[kind.flood])
        id_col = next(c for c in own.columns if c.lower() == kind.id_col.lower())
        own = own.assign(**{id_col: _strip(own[id_col])}).rename(columns={id_col: kind.id_col})
        own = own[own[kind.id_col].isin(set(_strip(base[kind.id_col])))]
        fl = own.set_index(kind.id_col)
        d = defaults.set_index(kind.id_col)
        has_default = d.notna().any(axis=1)
        fl.update(d[has_default].reindex(columns=fl.columns))  # the trigger only updates rows with a default
        fl = fl.reset_index()
    else:
        fl = defaults
    return {kind.base: base, kind.flood: to_region_types(kind.flood, fl)}


def create_region(state, *, counties: Iterable[str] | None = None, tracts: Iterable[str] | None = None,
                  blocks: Iterable[str] | None = None, year: int | None = None) -> dict[str, pd.DataFrame]:
    """A study region's flood tables from one state inventory (or a list of them).

    Give ``counties`` (Hazus "by county"), ``tracts`` ("by tract") or ``blocks`` ("by census block"). The
    boundary is built from the shapes of that level (TIGER block shapes for blocks). ``year`` is the
    creation year used for depreciation (default: this year, as in Hazus).
    """
    import geopandas as gpd

    state = _concat_states(state)
    year = year or datetime.date.today().year
    counties, tracts, blocks = (None if ids is None else list(ids) for ids in (counties, tracts, blocks))
    level = "block" if blocks else "tract" if tracts else "county"
    counties, tracts, blocks = select(state, counties, tracts, blocks)
    region: dict[str, pd.DataFrame] = {}
    region["hzCounty"] = _dedupe(_filter(state["hzCounty"], "CountyFips", counties, "hzCounty"), ["CountyFips"])
    region["hzTract"] = _dedupe(_filter(state["hzTract"], "Tract", tracts, "hzTract"), ["Tract"])
    region["hzCensusBlock"] = _dedupe(_filter(state["hzCensusBlock"], "CensusBlock", blocks, "hzCensusBlock"),
                                      ["CensusBlock"])
    for name in BLOCK_TABLES:
        if name in state:
            region[name] = _dedupe(_filter(state[name], "CensusBlock", blocks, name), ["CensusBlock"])

    schemes = set(_strip(region["hzTract"]["BldgSchemesId"]).dropna())
    region["hzGenBldgSchemes"] = _dedupe(_filter(state["hzGenBldgSchemes"], "BldgSchemesId", schemes,
                                                 "hzGenBldgSchemes"), ["BldgSchemesId"])
    region["hzGenBldgScheme"] = _dedupe(_filter(state["hzGenBldgScheme"], "BldgSchemesId", schemes,
                                                "hzGenBldgScheme"), ["BldgSchemesId", "Occupancy"])
    for name in STATE_WIDE_TABLES:
        if name in state:
            rows = to_region_types(name, state[name])
            keys = ["SchemeId"] if name == "flSchemeInfo" else [
                c for c in ("SchemeId", "SOccup", "FoundationType", "HazardType") if c in rows.columns]
            region[name] = _dedupe(rows, keys)

    sm = _dedupe(_filter(state["flSchemeMapping"], "CensusBlock", blocks, "flSchemeMapping"), ["CensusBlock"])
    entry = pd.to_numeric(sm["EntryDate"], errors="coerce")
    sm["EntryDate"] = entry.mask(entry.isna() | (entry == 0), 2009).astype("Int64")
    btype = _strip(region["hzCensusBlock"]["BlockType"])
    btype.index = _strip(region["hzCensusBlock"]["CensusBlock"])
    bt = _strip(sm["CensusBlock"]).map(btype)
    sid = _strip(sm["SchemeId"])
    cl = bt.isin(["C", "L"])
    sm.loc[cl, "SchemeId"] = sid[cl].str[:2] + bt[cl] + sid[cl].str[3:10]
    region["flSchemeMapping"] = sm

    for kind in [*ESSENTIAL_FACILITIES.values(), *UTILITIES.values()]:
        region.update(_facility_tables(state, kind, tracts))

    dep, dep_content = depreciated_exposure(region["hzCensusBlock"], region["hzDemographicsB"],
                                            region["hzExposureOccupB"], year)
    region["flDepExposureOccupB"] = to_region_types("flDepExposureOccupB", dep)
    region["flDepExposureContentOccupB"] = to_region_types("flDepExposureContentOccupB", dep_content)

    shapes = {"county": region["hzCounty"], "tract": region["hzTract"],
              "block": _filter(state.get("hzCensusBlock_TIGER", state["hzCensusBlock"]), "CensusBlock", blocks)}[level]
    region["hzBoundary"] = gpd.GeoDataFrame({"OBJECTID": [1]},
                                            geometry=[study_region_boundary(shapes.geometry.dropna())],
                                            crs=shapes.crs or "EPSG:4326")
    return region
