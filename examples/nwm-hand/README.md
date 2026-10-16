# Reference workflow: NWM flows → HAND inundation → Hazus flood losses

This example runs a riverine flood loss assessment from national, openly available data: National Water Model
(NWM) flows, NOAA's height-above-nearest-drainage (HAND) inundation mapping, and the Hazus 7.2 flood loss
methodology in pyhazus. It is meant for **relative** evaluation:
- near-real-time assessments, such as the same region under successive analyses or forecasts;
- hypothetical scenarios, such as flows scaled up or down, or different regions under one storm.

It is not meant to reproduce the losses of a historical event. The case below is the remnants of Hurricane Ida,
1–3 September 2021, in New Castle County, Delaware. It is a historical event, so the results can also be checked
against observations.

[workflow.py](workflow.py) implements the workflow, one subcommand per step. [ida-brandywine.toml](ida-brandywine.toml)
is the configuration for this case. The two `requirements-*.txt` files pin the environments, and
[ida-brandywine.provenance.json](ida-brandywine.provenance.json) records the reference run.

## Design rules

These were fixed before the reference run, and the workflow applies them the same way everywhere.

1. **One method everywhere.** No step depends on where a reach or block is, beyond the data that describes it.
   No region-specific settings.
2. **Only inputs that exist when the assessment is made.**
   - Near real time: NWM analysis and forecast flows.
   - Hypothetical scenarios: a flow per reach.
   - Static data: HAND, 3DEP elevations, NOAA tidal datums and the Hazus inventory.
   - Observations collected after an event (high-water marks, insurance claims) are used only to evaluate a
     historical run, never to adjust it.
3. **Published tools as published.** NOAA's HAND data and inundation code are used without event calibration,
   pinned to a data version and a code commit. pyhazus reproduces Hazus 7.2.
4. **Domain from the study region.** Every HUC-8 watershed that intersects the region and has HAND data.

## The workflow

| # | Step | Inputs | Output |
|---|---|---|---|
| 1 | `region` | Hazus state inventory | study region (Parquet tables, one per Hazus table) |
| 2 | `hucs` | region boundary, USGS Watershed Boundary Dataset | HUC-8 watersheds intersecting the region |
| 3 | `hand` | CIROH HAND bucket, NOAA-OWP/inundation-mapping | HAND data per watershed, OWP code at a pinned commit |
| 4 | `flows` | NWM analysis & assimilation (or a flow table) | peak flow per NWM reach over the event window |
| 5 | `tidal` | HAND reach network, 3DEP elevations, NOAA tidal datums | tidal segments; HAND rating tables without them |
| 6 | `inundate` | masked HAND data, flows | depth grid per watershed (m) |
| 7 | `losses` | region, depth grids | Hazus flood results (damage, losses, shelter, debris, facilities) |
| 8 | `evaluate` | USGS gauges, USGS high-water marks, NFIP claims | diagnostics (historical events only) |

### 1. Study region

`hazus region create` builds the region from FEMA's state inventory, the same way Hazus's study-region aggregation
does (see [docs/region.md](../../docs/region.md)). The state inventory is exported once from FEMA's state database
with `hazus inventory export-state`. Here the region is New Castle County (FIPS 10003): 7,538 census blocks.

### 2. Watersheds

The USGS Watershed Boundary Dataset service returns the HUC-8s that intersect the region's boundary. Those with
published HAND data are kept. The hazard is mapped over whole watersheds, and losses are counted within the
region.

### 3. HAND data and inundation code

- **HAND data:** NOAA OWP's HAND rasters, catchments and synthetic rating curves (`hand_fim_4_9_9_0`), copied
  anonymously from the CIROH bucket.
- **Inundation code:** NOAA-OWP/inundation-mapping at commit `bf70e85` (v4.10.2.1).

### 4. Flows

- **Real events:** NWM *analysis & assimilation*, the hourly `tm00` files of each operational cycle. This is the
  model's near-real-time estimate of current flows, and it nudges the flows towards USGS gauge observations. It is
  what an assessment made during the event would have had. For Ida these are the files of NWM v2.1, the version
  operational in 2021.
- **The event peak:** for each reach, the maximum over the event window (here 1 September 00:00 to 3 September 23:00
  UTC). A depth map of per-reach peaks is an envelope: reaches peak at different times.
- **Hypothetical scenarios:** a table of `feature_id, discharge` (m³/s) replaces this step (`flows = "csv"`).

### 5. Tidal mask

HAND converts a reach's flow into a water level with a rating curve. That is not valid where the tide sets the
water level, so tidal channel segments are removed from HAND's rating tables and get no riverine water. Coastal
and tidal flooding is a separate hazard; pyhazus takes it as a coastal depth grid.

- **Head of tide:** a segment is tidal when its channel elevation is at or below mean higher high water (MHHW).
  - **Channel elevation:** the median 3DEP elevation of points at 25, 50 and 75 % along the segment.
  - **MHHW:** in NAVD88, from the nearest NOAA water-level station that publishes both datums.
- **Search:** in each HAND branch, the walk starts at the segments draining out of the network and goes upstream
  only through tidal segments. Each path stops at its first segment above MHHW, which is its head of tide.

This rule was added after the first Ida run, which spread a river rating curve over the tidal Christina River. It
uses no observation of Ida. It encodes a documented limit of HAND, and it applies to any tidal coast.

### 6. Inundation

`inundate_mosaic_wrapper.py` (NOAA OWP) is run per watershed on the masked HAND data. Its depth output is in
millimetres (16-bit integers). The workflow converts it to metres and marks dry cells as NoData.

### 7. Losses

`hazus flood run` takes the depth grids of all watersheds as one riverine event and merges them cell by cell. It
then runs the Hazus 7.2 flood analyses:
- building stock damage and economic loss;
- essential facilities and utilities;
- debris and shelter.

The results are the Hazus result tables (`flFR*`), one Parquet file each.

### 8. Evaluation (historical events only)

The diagnostics do not change the run.

- **USGS gauges:** NWM peak flow against the observed peak. HAND's water level at the NWM peak against the
  observed peak water level, converted from NGVD29 to NAVD88 with NOAA's NCAT where needed.
- **USGS high-water marks** (Short-Term Network, event 312):
  - *Observed depth:* mark elevation minus the 3DEP ground at the mark.
  - *Modeled depth:* the depth grid at the mark, 0 where dry.
  - *Position check:* a mark is used only if the water is not below the ground there and the lidar depth agrees
    within 3 ft with any height measured in the field. Marks located by phone GPS on steep banks fail it.
- **NFIP claims** (OpenFEMA): by census block group, claims and assessed building and contents damage, compared
  with the modeled building and contents loss. NFIP covers only insured buildings, so its total is a floor. The
  useful comparison is where the damage is.

## Running it

Two Python environments are used, both pinned:

- **The workflow's own environment** ([requirements-hand.txt](requirements-hand.txt)): the script's imports, plus
  what NOAA OWP's inundation code imports at the pinned commit. The code itself is not installed: the `hand` step
  clones it.
- **pyhazus** ([requirements-pyhazus.txt](requirements-pyhazus.txt), plus this repository): called as a program for
  steps 1 and 7.

The pins are the versions of the reference run below: Python 3.12.15 on Windows 11.
[requirements-hand.in](requirements-hand.in) records how the first lock was compiled. FIMserv is not needed.

```bash
uv venv --python 3.12 work/hand-env
```

```bash
uv pip install --python work/hand-env/Scripts/python.exe -r examples/nwm-hand/requirements-hand.txt
```

```bash
uv venv --python 3.12 work/pyhazus-env
```

```bash
uv pip install --python work/pyhazus-env/Scripts/python.exe -r examples/nwm-hand/requirements-pyhazus.txt
```

```bash
uv pip install --python work/pyhazus-env/Scripts/python.exe --no-deps -e .
```

On Linux or macOS, use `bin/python` in place of `Scripts/python.exe`, and `work/pyhazus-env/bin/hazus` in the
configuration. Then, from the repository root:

```bash
work/hand-env/Scripts/python.exe examples/nwm-hand/workflow.py examples/nwm-hand/ida-brandywine.toml all
```

Each step writes under `[paths] work` and skips work already done, so steps can be run one at a time.

- **Downloads:** about 2.1 GB of HAND data for the five watersheds, and about 1 GB of hourly NWM files, read and
  discarded one at a time.
- **Disk:** the masked copy of the HAND data takes as much again, about 4 GB in all.
- **Another scenario in the same work directory:** delete `flows/`, `depth/` and `results/`, then rerun from
  `flows`. The tidal mask (`tidal/`, `hand_masked/`) depends only on static data and is kept.
- **Rate limit:** the evaluation's USGS requests are limited to 1,000 an hour without an API key. The script waits
  when told to. An api.data.gov key in the environment variable `USGS_API_KEY` raises the limit.

## Provenance

Every invocation writes `provenance.json` in the work directory. It records:

- **Configuration:** its text and sha256.
- **Workflow:** the script's sha256, and the repository commit with a flag for uncommitted changes.
- **Steps:** when each step ran and how long it took (also in `steps.json`).
- **Environments:** the Python version, platform and every installed package, for both environments.
- **Data:**
  - the state inventory, as a sha256 over its files;
  - the HAND data: bucket, version, file count, bytes, newest modification time, and a per-file manifest with
    ETags (`hand/manifest.csv`);
  - NOAA OWP's code commit;
  - the NWM files: count, valid times and archive modification times, with a per-file manifest
    (`flows/nwm_ana_files.csv`), or the sha256 of a flow table.
- **Web services:** for each service (3DEP elevations, NOAA tidal datums, USGS gauges, USGS high-water marks,
  NOAA NCAT, OpenFEMA), when its answers were first and last retrieved and how many.

Some of those services answer differently over time:
- 3DEP serves the best available elevation, which changes when new lidar is added;
- tidal datums move to new epochs;
- USGS revises gauge records;
- FEMA updates NFIP claims.

The answers a run used are kept in `cache/web.json`; keep that file with the results to rerun the same inputs.
[ida-brandywine.provenance.json](ida-brandywine.provenance.json) is the record of the reference run below.

## The Ida case

### The run

Times are those of a run from scratch in the pinned environments, with an empty web cache
(see [ida-brandywine.provenance.json](ida-brandywine.provenance.json)). The HAND data had been downloaded once
before, which takes about 5 minutes; the run copied it locally, and its manifest lists the bucket's ETags.

| Step | Result | Time |
|---|---|---|
| region | New Castle County, 7,538 census blocks | 20 s |
| hucs | 6 HUC-8s intersect the county; 5 have HAND data (Delaware Bay, 0.1 % of the county, has none) | 6 s |
| hand | 1,356 files, 2.1 GB of HAND data; OWP code at `bf70e85` | 1 s |
| flows | 72 hourly analysis files (1 GB); flows for 4,369 of 4,414 HAND reaches (99 %) | 31 s |
| tidal | 8,384 segments in 102 branches, 25,177 elevations; 2,082 tidal segments (1,530 km of channel in branch 0) | 6.6 min |
| inundate | 184 km² wet over the five watersheds | 2.8 min |
| losses | Hazus flood analyses on the merged grids | 3.0 min |
| evaluate | 46 gauges, 103 high-water marks, 97 NFIP claims | 80 s |

The same outputs, cell for cell and row for row, were produced earlier in an environment built from FIMserv.

| Watershed | Share of the county | NWM reaches | Peak flow (m³/s) | Tidal segments | Wet (km²) |
|---|---|---|---|---|---|
| 02040205 Brandywine-Christina | 77.5 % | 735 | 1,195 | 174 | 83.0 |
| 02060002 Chester-Sassafras | 10.9 % | 1,133 | 334 | 757 | 36.1 |
| 02040207 Broadkill-Smyrna | 9.6 % | 635 | 85 | 297 | 7.6 |
| 02040202 Lower Delaware | 1.7 % | 1,029 | 5,828 | 386 | 49.6 |
| 02040206 Cohansey-Maurice | 0.2 % | 882 | 8 | 468 | 7.2 |

Each segment takes MHHW from its nearest NOAA station that publishes it in NAVD88. On the tidal segments this ranges
from 0.20 to 1.09 m (median 0.87 m):
- **Delaware estuary:** 0.87–1.09 m (Reedy Point, Marcus Hook, Philadelphia).
- **Delaware Bay:** 0.62–0.74 m (Lewes, Cape May).
- **Chesapeake side:** 0.20–0.50 m (Chesapeake City, Tolchester Beach, Cambridge, Annapolis).

### Losses (New Castle County)

| | |
|---|---|
| Flooded census blocks | 660 |
| Direct economic loss | $312 M |
| – building and contents | $159 M |
| Damaged buildings (more than 50 % damaged) | 46 (9) |
| Displaced people | 1,821 |
| Short-term shelter | 1,054 people |
| Debris | 20 kt |
| Share of the loss in the five largest blocks | 48 % |

### Evaluation

**USGS gauges.** The five watersheds hold 46 gauges. Two are on tidal segments the mask removes. Peak flows are
compared at 40 gauges and peak water levels at 41.

- **Flow:** NWM's analysis peak is within ±10 % of the observed peak at 70 % of gauges (median ratio 0.98). Its
  data assimilation pulls most gauged reaches close.
  - Seven gauges are below 0.8. Five are on the Brandywine and its branches: East Branch 0.39–0.42, Chadds Ford
    0.52, Modena 0.64, Wilmington 0.74. The others are Big Elk Creek (0.56) and Pennypack Creek (0.72). These are
    the largest, fastest-rising floods in the domain.
- **Water level:** HAND's water level at NWM's peak flow is within 1 ft of the observed peak at 51 % of gauges and
  within 2 ft at 68 % (median absolute error 0.7 ft).
  - The exceptions are local:
    - Brandywine at Wilmington: 12.4 ft too high.
    - Marsh Creek below its reservoir: 27.8 ft too high.
    - Salem River at Woodstown: 18 ft too low.
    - North Branch Rancocas at Mount Holly: 6.4 ft too low.
    - West Branch at Coatesville: 5.0 ft too high.
    - Chadds Ford: 4.3 ft too low.
  - The two largest are below a reservoir dam (Marsh Creek) and in the Brandywine's gorge through Wilmington.
    HAND's synthetic rating curves do not represent controls like these.

**High-water marks.** 103 USGS marks for Ida fall in the five watersheds, 78 of which pass the position check:
56 on the Brandywine, 10 on its East Branch, 12 on Big Elk Creek (Maryland). Errors are modeled minus observed
depth.

| Marks | n | Mean error | Mean abs. error | RMSE | Within ±2 ft | Inside the modeled flood |
|---|---|---|---|---|---|---|
| All | 78 | +1.1 ft | 3.7 ft | 4.9 ft | 40 % | 79 % |
| Delaware | 29 | +3.2 ft | 5.5 ft | 7.0 ft | 34 % | 83 % |
| Maryland | 12 | −0.9 ft | 1.9 ft | 2.2 ft | 67 % | 50 % |
| Pennsylvania | 37 | +0.2 ft | 2.8 ft | 3.4 ft | 35 % | 86 % |

The Delaware errors come from two places:
- **Too high around the Wilmington gauge** (7–14 ft at most marks). There HAND's rating curve gives too high a
  stage, even though the NWM flow is low.
- **Dry below the Brandywine's head of tide**, where marks show up to 11 ft of water: the tidal mask.

**NFIP claims.** 97 claims for Ida in the county ($7.4 M assessed building and contents damage) fall in 16 block
groups. The model has losses in 8 of them, which hold 26 % of the claims. 63 % of the modeled building and
contents loss ($159 M) lies in block groups with claims. The largest cluster, 43 claims in block group
100030030021 on the lowest Brandywine, gets no modeled loss: it is below the head of tide.

### Reading these results

For relative work, what matters is that the workflow's errors are systematic and stay put. In this case:
- HAND's rating curves overestimate stage in the Brandywine gorge.
- The tidal mask removes the river's flooding on its lowest reach.
- NWM's peaks are low on the upper Brandywine.

Scenarios compared with this workflow share these errors. They cancel only partly, because damage functions are
non-linear in depth. A scenario that moves water into or out of the affected reaches is the case to read with care.

## What the reference leaves out, and why

These were tried interactively on this case before the reference was fixed. Those exploratory runs are not part
of this example, and their numbers are not repeated here. Each needs observations that a near-real-time or
hypothetical assessment would not have, or a choice made after seeing results.

- **Flows conditioned on observed gauge peaks.** They need the event's gauge records, and a scenario has none.
  In near real time, NWM's analysis already nudges flows towards the gauges.
- **Shifting HAND's rating curves to match observed gauge stages.** This needs observed stages, and in the
  exploratory runs the corrections did not carry over from one gauge to its neighbours. The gauge check above
  shows how large HAND's stage errors are without it, for example 12 ft at Wilmington.
- **Calibrating on high-water marks.** The marks are collected after an event. In split-sample tests, calibration
  improved held-out marks on some reaches and worsened them on others. Using it only where it helped would be a
  choice made from the validation results.
- **Filling tidal segments to the observed tide, or sloping the water level from the head of tide to the tide.**
  These need event water levels. The sloped version also spread the river's level back over the tidal Christina.
- **Mapping only the Brandywine–Christina watershed.** That watershed was chosen because Ida's flooding was known
  to be there. The reference maps every watershed that intersects the region.

## Limitations

- **HAND's synthetic rating curves** can be several metres off in confined, regulated valleys, for example the
  Brandywine gorge with its falls and dams, and below reservoirs. The gauges show the size of this error.
- **HAND's level-path branches** can assign cells beside one river to the catchment of another. The mosaic's maximum
  then floods them from the wrong river.
- **The tidal mask removes the river's flooding below the head of tide,** even when the river, not the tide,
  controls the water level, as on the lowest Brandywine during Ida.
- **Pluvial (rainfall) flooding** away from the NWM network, and flooding from small streams HAND does not map, are
  not represented.
- **Mismatched networks:** NWM v2.1 flows (2021) are mapped onto HAND data built on the NWM v3.0 network. A few
  reaches without a matching ID get no flow.
- **Aggregate inventory:** Hazus spreads each census block's buildings uniformly over its developed area, so losses
  depend on the flooded share of a block, not on where individual buildings stand.

## Using it for other assessments

- **Another historical event:** change `[region]`, `[event]` and, for evaluation, `[evaluate] stn_event`.
- **Near real time:** set the window to the hours of interest. This workflow reads NWM's public archive.
  Operational output appears first on NOAA's NOMADS server, so real-time use would point the `flows` step there.
  Forecast flows (NWM short- and medium-range) would be a further `flows` source, taking each reach's peak over the
  forecast in the same way. It is not implemented here.
- **Hypothetical scenarios:** write a flow table and set `flows = "csv"`, for example the reference peaks multiplied
  by a factor, or NWM recurrence-interval flows. Keep everything else fixed, and compare runs against each other
  rather than against observations.
