"""Hazus flood model (port of the Hazus 7.2 flood analysis path).

See ``docs/flood-dataflow.md`` for the reconstructed Hazus pipeline that these modules mirror.
"""

from hazus.flood.aal import aal_coefficients
from hazus.flood.exposure import area_weighted_depths, depth_bin, to_area_weighted_table
from hazus.flood.hazard import FloodRaster, HazardType

__all__ = [
    "FloodRaster",
    "HazardType",
    "aal_coefficients",
    "area_weighted_depths",
    "depth_bin",
    "to_area_weighted_table",
]
