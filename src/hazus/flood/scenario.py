"""A complete flood analysis: port of ``Hazus.Logic.Flood.AnalysisLogic.RunAsync`` (Hazus 7.2).

The order follows Hazus:

1. For each return period, in ascending order:

   * area weighting of each depth grid (``Analysis Area Weighted`` + ``absp_GBSInsertAW``), giving
     ``flAnAreaWeighted`` and ``flAnShelterResult`` rows;
   * essential facilities and utilities at the maximum depth over that return period's grids.

2. General building stock (``absp_GBSDoIt``), debris (``CalculateDebris``) and shelter (``absp_SHRDoIT``).
3. AAL (``absp_AALDoIt``) when there are more than two return periods.

Hazus behaviours reproduced on purpose:

* The return-period id is the period itself when there are several. With a single period it is ``"mix0"`` if
  the grids mix riverine and coastal, else the grid's period (``-1`` for a single event).
* ``absp_GBSInsertAW`` first deletes the rows of the same return period and controlling hazard. So when two
  grids share both, only the *last* one reaches the building-stock, debris and shelter results. Facilities
  still take the maximum over all of them.
* User-defined facilities are not analysed (UDF analysis is switched off for flood in Hazus 7.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pandas as pd

from hazus.flood.hazard import FloodRaster, HazardType, hazard_type_code, return_period_ids


@dataclass(frozen=True)
class AnalysisOptions:
    gbs: bool = True
    essential_facilities: bool = True
    utilities: bool = True
    debris: bool = True
    shelter: bool = True
    aal: bool = True


def area_weighting(blocks, rasters: Sequence[FloodRaster], *, boundary=None, study_case_id: int = 1,
                   analysis_opt_id: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``flAnAreaWeighted`` and ``flAnShelterResult`` for all depth grids of a scenario."""
    from hazus.flood.exposure import area_weighted_depths, area_weighted_rows
    from hazus.flood.shelter import shelter_result

    ids = return_period_ids(rasters)
    aw: dict[tuple[str, str], pd.DataFrame] = {}
    sr: dict[tuple[str, str], pd.DataFrame] = {}
    block_counts: dict = {}  # shared by grids that snap to the same block grid
    for period in sorted(ids):
        for raster in (r for r in rasters if int(r.return_period) == period):
            rp = ids[period]
            hazard = HazardType(raster.hazard_type).controlling_code
            long = area_weighted_depths(blocks, raster, boundary=boundary, block_counts=block_counts)
            # absp_GBSInsertAW replaces earlier rows of the same return period and hazard
            aw[rp, hazard] = area_weighted_rows(long, study_case_id=study_case_id, return_period=rp,
                                                controlling_hazard=hazard, analysis_opt_id=analysis_opt_id)
            sr[rp, hazard] = shelter_result(long, study_case_id=study_case_id, return_period=rp,
                                            control_hazard=hazard, analysis_opt_id=analysis_opt_id)
    return pd.concat(aw.values(), ignore_index=True), pd.concat(sr.values(), ignore_index=True)


def run_scenario(region: Mapping[str, pd.DataFrame], rasters: Sequence[FloodRaster], *,
                 options: AnalysisOptions = AnalysisOptions(), study_case_id: int = 1,
                 area_weighted: pd.DataFrame | None = None,
                 shelter_results: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """All Hazus flood result tables for a study region and a set of depth grids.

    ``region`` maps Hazus table names to inventory tables. ``hzCensusBlock`` must be a GeoDataFrame
    (EPSG:4326) when building-stock, debris or shelter results are requested. ``hzBoundary``, if present,
    masks the depth grids as the study-region boundary does in Hazus. ``rasters`` are depth grids in feet,
    EPSG:4326 (:func:`hazus.flood.hazard.prepare_depth_grid`).

    ``area_weighted`` and ``shelter_results`` skip the raster step when they are already known. That is how
    the SQL differential tests drive this function.
    """
    from hazus.flood.aal import aal_coefficients_decimal, aal_gbs
    from hazus.flood.debris import calculate_debris
    from hazus.flood.facility_analysis import run_facilities
    from hazus.flood.gbs import run_gbs
    from hazus.flood.shelter import run_shelter

    if not rasters:
        raise ValueError("a flood scenario needs at least one depth grid")
    ids = return_period_ids(rasters)
    out: dict[str, pd.DataFrame] = {}
    analysis_opt_id = 0  # RunAsync always uses 0

    needs_aw = options.gbs or options.debris or options.shelter
    if needs_aw and (area_weighted is None or (options.shelter and shelter_results is None)):
        boundary = None
        if "hzBoundary" in region and hasattr(region["hzBoundary"], "geometry"):
            boundary = region["hzBoundary"].to_crs(4326).geometry.union_all()
        area_weighted, shelter_results = area_weighting(region["hzCensusBlock"], rasters, boundary=boundary,
                                                        study_case_id=study_case_id)
    if area_weighted is not None:
        out["flAnAreaWeighted"] = area_weighted

    if options.essential_facilities or options.utilities:
        out.update(run_facilities(region, rasters, study_case_id=study_case_id,
                                  essential=options.essential_facilities, utilities=options.utilities))

    if options.gbs:
        out.update(run_gbs(region, area_weighted, hazard_type_code(rasters), study_case_id=study_case_id,
                           analysis_opt_id=analysis_opt_id))
    if options.debris:
        out["flFRDebris"] = calculate_debris(region, area_weighted, study_case_id=study_case_id,
                                             analysis_opt_id=analysis_opt_id)
    if options.shelter:
        out["flAnShelterResult"] = shelter_results
        out.update(run_shelter(region, shelter_results))
    if options.aal and options.gbs and len(ids) > 2:
        coefficients = aal_coefficients_decimal(ids)
        annual = aal_gbs(out["flFRGBSEcLossBySOccup"], coefficients, region)
        for name, rows in annual.items():
            out[name] = pd.concat([out[name], rows], ignore_index=True)
        out["flReturnPeriod"] = pd.DataFrame({"StudyCaseId": study_case_id, "ReturnPeriod": list(coefficients),
                                              "Coefficient": list(coefficients.values())})
    return out
