"""General building stock depth-damage functions: the Hazus ``flDmgFinal`` table.

Port of ``absp_DmgDoIt`` → ``absp_DmgFnNorm``, ``absp_DmgBaseSplit``, ``absp_DmgFinal`` and the
``absf_DmgPct*`` table functions (Hazus 7.2). The result has one row per
(ZoneID, FirmID, BlockTypeID, HazardTypeID, FoundationID, STORYID, OCCID, DepthID). ``PctDmgStr``,
``PctDmgCont`` and ``PctDmgInv`` are the damage percentages at depth-in-structure ``DISID``, in hundredths
of a foot.

The joins mirror the SQL exactly, including which xref hazard flag each coastal zone uses and rows that
come out duplicated. See ``docs/flood-gbs.md``, section 1.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from hazus.data import table

HAZ_R, HAZ_A, HAZ_CA, HAZ_CV = 1, 2, 3, 4
WHOLE_FEET = [("ft04m", -400), ("ft03m", -300), ("ft02m", -200), ("ft01m", -100)] + \
    [(f"ft{i:02d}", i * 100) for i in range(0, 25)]

FINAL_COLUMNS = ["ZoneID", "FirmID", "BlockTypeID", "HazardTypeID", "FoundationID", "STORYID", "OCCID",
                 "DepthID", "DISID", "PctDmgStr", "PctDmgCont", "PctDmgInv"]


def normalize(fn: pd.DataFrame, id_col: str, *, min_depth: int = -400) -> pd.DataFrame:
    """Damage function → (id, Depth, Val) every half foot from ``min_depth`` to 24 ft (``absp_DmgFnNorm``).

    Half-foot values are the mean of the neighbouring whole-foot columns. Structure and content functions
    start at -4 ft. Inventory functions (``absf_BldgInvDmgFn``) start at 0 ft, so inventory has no damage
    rows at negative depth in structure.
    """
    ids = fn[id_col].astype("int64")
    feet = [(c, d) for c, d in WHOLE_FEET if d >= min_depth]
    parts = []
    for k, (col, depth) in enumerate(feet):
        parts.append(pd.DataFrame({id_col: ids, "Depth": depth, "Val": fn[col].astype("float64")}))
        if k + 1 < len(feet):
            mid = (fn[col].astype("float64") + fn[feet[k + 1][0]].astype("float64")) / 2
            parts.append(pd.DataFrame({id_col: ids, "Depth": depth + 50, "Val": mid}))
    return pd.concat(parts, ignore_index=True)


def damage_base(hazard_type_id: int, ffe_set: int = 0) -> pd.DataFrame:
    """``flDmgBase{R,A,CA,V}``: depth × foundation/first-floor-height × occupancy-story (``absp_DmgBaseSplit``)."""
    depth = table("flDepth")
    foundation = table("flFoundation")
    key = table("flFFEKey")
    ffe = table("flFFEValues")
    stories = table("flSOccupStoriesXRef")
    occ = table("flSOccupIDBase")

    fk = foundation.merge(key[key.HazardTypeID == hazard_type_id], on="FoundationID") \
        .merge(ffe[ffe.FFESet == ffe_set], on="FFEID")
    so = stories.merge(occ, left_on="SOccupID", right_on="ID", suffixes=("", "_base"))
    base = fk.merge(so, how="cross").merge(depth.rename(columns={"ID": "DepthID"}), how="cross")
    base["ZoneID"] = np.where(base["HazardTypeID"] == HAZ_CV, 2, 1)
    base["OCCID"] = base["SOccupID"].astype("int64")
    base["STORYID"] = base["StoryID"].astype("int64")
    base["SOCCUPID"] = base["SOccupID_base"].str.strip() + base["Story"].str.strip() + base["BaseText"].str.strip()
    # CONVERT(INT, (d.Depth - v.First_Fl_Ht) * 100): both columns are REAL, so the arithmetic is float32,
    # and the conversion truncates toward zero.
    dis = (base["Depth"].to_numpy(np.float32) - base["First_Fl_Ht"].to_numpy(np.float32)) * np.float32(100)
    base["DISID"] = np.trunc(dis).astype("int64")
    return base[["FirmID", "BlockTypeID", "HazardTypeID", "ZoneID", "FoundationID", "STORYID", "OCCID",
                 "SOCCUPID", "DepthID", "DISID"]]


def final_xref(name: str) -> pd.DataFrame:
    """``flBldg*DmgFinal``: xref rows with ``Selected = 1``, else the ``DefaultFn = 1`` rows (``absp_DmgFinal``)."""
    x = table(name)
    x["SOccupId"] = x["SOccupId"].str.strip()
    selected = x[x["Selected"].astype(bool)]
    default = x[x["DefaultFn"].astype(bool) & ~x["SOccupId"].isin(selected["SOccupId"])]
    return pd.concat([selected, default], ignore_index=True)


def _join_fn(base: pd.DataFrame, xref: pd.DataFrame, flag: str, norm: pd.DataFrame, fn_id: str,
             val_name: str) -> pd.DataFrame:
    x = xref[xref[flag].astype(bool)][["SOccupId", fn_id]]
    out = base.merge(x, left_on="SOCCUPID", right_on="SOccupId")
    out = out.merge(norm.rename(columns={"Val": val_name}), left_on=[fn_id, "DISID"], right_on=[fn_id, "Depth"])
    return out.drop(columns=["SOccupId", fn_id, "Depth"])


# (base hazard type, min DISID, structure flag, content flag, inventory flag) per absf_DmgPct* function
_SPECS = {
    "Riv": (HAZ_R, -400, ("HazardR", "HazardR", "HazardR"), ("HazardR", "HazardR")),
    "CoastA": (HAZ_A, -4, ("HazardR", "HazardR", "HazardR"), ("HazardR", "HazardR")),
    "CoastCA": (HAZ_CA, -4, ("HazardCV", "HazardCA", "HazardCA"), ("HazardCV", "HazardCA")),
    "CoastV": (HAZ_CV, -4, ("HazardCA", "HazardCA", "HazardCA"), ("HazardCV", "HazardCV")),
}


def damage_pct(kind: str, *, with_inv: bool | None, ffe_set: int = 0, cache: dict | None = None) -> pd.DataFrame:
    """One ``absf_DmgPct{kind}{WithInv|NoInv|Zero}`` function. ``with_inv=None`` gives the Zero variant."""
    cache = {} if cache is None else cache
    haz, min_disid, inv_flags, noinv_flags = _SPECS[kind]
    if ("base", haz) not in cache:
        cache[("base", haz)] = damage_base(haz, ffe_set)
    base = cache[("base", haz)]
    if with_inv is None:
        z = base[base["DISID"] < 0].copy()
        z["PctDmgStr"] = z["PctDmgCont"] = z["PctDmgInv"] = 0.0
        return z
    if "norm" not in cache:
        cache["norm"] = {
            "s": normalize(table("flBldgStructDmgFn").rename(columns={"BldgDmgFnID": "BldgDmgFnId"}), "BldgDmgFnId"),
            "c": normalize(table("flBldgContDmgFn"), "ContDmgFnId"),
            "i": normalize(table("flBldgInvDmgFn"), "InvDmgFnId", min_depth=0),
        }
        cache["xs"] = final_xref("flBldgStructDmgXRef")
        cache["xc"] = final_xref("flBldgContDmgXRef")
        cache["xi"] = final_xref("flBldgInvDmgXRef")
    norm, xs, xc, xi = cache["norm"], cache["xs"], cache["xc"], cache["xi"]

    b = base[base["DISID"] >= min_disid]
    if with_inv:
        s_flag, c_flag, i_flag = inv_flags
    else:
        s_flag, c_flag = noinv_flags
        inv_occ = table("flGBSOccupInv")
        b = b[b["OCCID"].isin(inv_occ.loc[~inv_occ["Inv"].astype(bool), "ID"])]
    out = _join_fn(b, xs, s_flag, norm["s"], "BldgDmgFnId", "PctDmgStr")
    out = _join_fn(out, xc, c_flag, norm["c"], "ContDmgFnId", "PctDmgCont")
    if with_inv:
        out = _join_fn(out, xi, i_flag, norm["i"], "InvDmgFnId", "PctDmgInv")
    else:
        out["PctDmgInv"] = 0.0
    return out


def build_dmg_final(haz_type: int, ffe_set: int = 0) -> pd.DataFrame:
    """The Hazus ``flDmgFinal`` table for scenario hazard type 1 (riverine), 2 (coastal) or 3 (both)."""
    cache: dict = {}
    pieces: list[pd.DataFrame] = []
    if haz_type in (1, 3):
        pieces += [damage_pct("Riv", with_inv=True, ffe_set=ffe_set, cache=cache),
                   damage_pct("Riv", with_inv=False, ffe_set=ffe_set, cache=cache)]
    if haz_type in (2, 3):
        for kind in ("CoastA", "CoastCA", "CoastV"):
            pieces += [damage_pct(kind, with_inv=True, ffe_set=ffe_set, cache=cache),
                       damage_pct(kind, with_inv=False, ffe_set=ffe_set, cache=cache),
                       damage_pct(kind, with_inv=None, ffe_set=ffe_set, cache=cache)]
    out = pd.concat(pieces, ignore_index=True)[FINAL_COLUMNS]
    # @DMG table columns are REAL in Hazus
    for c in ("PctDmgStr", "PctDmgCont", "PctDmgInv"):
        out[c] = out[c].astype("float32")
    return out
