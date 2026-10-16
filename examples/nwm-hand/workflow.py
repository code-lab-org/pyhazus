"""Reference workflow: National Water Model flows -> NOAA OWP HAND inundation -> Hazus flood losses (pyhazus).

    python workflow.py CONFIG.toml STEP [STEP ...]

Steps, in order (``all`` runs them all; ``evaluate`` only makes sense for a historical event):

    region     study region from a state inventory (pyhazus ``hazus region create``)
    hucs       HUC-8 watersheds intersecting the region that have HAND data
    hand       HAND data per watershed (anonymous S3) and NOAA OWP's inundation code at a pinned commit
    flows      peak flow per NWM reach over the event window (NWM analysis & assimilation, or a CSV)
    tidal      head-of-tide walk per watershed; masked copy of the HAND rating tables
    inundate   OWP inundation with depth output per watershed, converted to metres
    losses     pyhazus ``hazus flood run`` on all depth grids (single riverine event)
    evaluate   diagnostics against USGS gauges, USGS high-water marks and NFIP claims
    provenance write provenance.json (also written after every invocation)

Run it with the Python of an environment built from requirements-hand.txt (this script's imports plus those of
NOAA OWP's inundation code); pyhazus is called as a separate program through ``[tools] hazus``. Every step writes
under ``[paths] work`` and skips what is already there, so a step can be rerun after deleting its outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio

FT = 3.28084  # feet per metre
ALBERS = 5070  # CRS of the HAND data
HAND_BUCKET = "ciroh-owp-hand-fim"
OWP_REPO = "https://github.com/NOAA-OWP/inundation-mapping.git"
WBD_HUC8 = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/4/query"
DEP_SAMPLES = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/getSamples"
COOPS = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
NWM_GCS = "https://storage.googleapis.com/national-water-model"
USGS_OGC = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
STN = "https://api.waterdata.usgs.gov/stnservices"
NCAT = "https://geodesy.noaa.gov/api/ncat/llh"
OPENFEMA = "https://www.fema.gov/api/open/v3/NfipClaims"


# --------------------------------------------------------------------------------------------- helpers

class Run:
    def __init__(self, config: Path):
        self.config_path = config
        self.cfg = tomllib.loads(config.read_text(encoding="utf-8"))
        self.work = Path(self.cfg["paths"]["work"])
        self.work.mkdir(parents=True, exist_ok=True)
        self.region = self.work / "region"
        self.hand = self.work / "hand"  # hand/flood_<huc>/<huc>/... as published
        self.masked = self.work / "hand_masked"
        self.cache = self.work / "cache"
        self.cache.mkdir(exist_ok=True)
        ev = self.cfg["event"]
        self.start = _utc(ev["start"])
        self.end = _utc(ev["end"])

    def hucs(self) -> list[str]:
        return pd.read_csv(self.work / "hucs.csv", dtype={"huc8": str})["huc8"].tolist()

    def path(self, *parts) -> Path:
        p = self.work.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


USER_AGENT = {"User-Agent": "pyhazus-nwm-hand-example/1.0"}  # some services refuse Python's default agent
MAX_WAIT_S = 3600  # longest rate-limit wait honoured


def get_json(url: str, params: dict | None = None, timeout: int = 120, tries: int = 3):
    """GET JSON; waits out HTTP 429 (Retry-After). USGS water data API: 1,000 requests/hour without a key,
    more with one in the environment variable USGS_API_KEY."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = dict(USER_AGENT)
    if "api.waterdata.usgs.gov" in url and os.environ.get("USGS_API_KEY"):
        headers["X-Api-Key"] = os.environ["USGS_API_KEY"]
    i = 0
    while True:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            wait = int(e.headers.get("Retry-After") or 0) if e.code == 429 else 0
            if 0 < wait <= MAX_WAIT_S:
                print(f"  rate limited by {urllib.parse.urlsplit(url).netloc}; waiting {wait} s", flush=True)
                time.sleep(wait + 5)
                continue
            i += 1
            if i >= tries or e.code in (400, 403, 404):
                raise
            time.sleep(10 * i)  # server errors (502, 503) are usually brief
        except Exception:  # noqa: BLE001 - retried, then raised
            i += 1
            if i >= tries:
                raise
            time.sleep(10 * i)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Cache:
    """A JSON file of small web answers (elevations, datums, gauge series), so reruns do not query again.

    ``_meta`` records, per service, when answers were first and last retrieved and how many, for provenance.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}
        self.meta = self.data.pop("_meta", {})

    def note(self, service: str, n: int = 1):
        if n:
            m = self.meta.setdefault(service, {"first_retrieved": now_utc(), "answers": 0})
            m["last_retrieved"] = now_utc()
            m["answers"] += n

    def get(self, key, fn, service: str):
        if self.data.get(key) is None:
            self.data[key] = fn()
            self.note(service)
        return self.data[key]

    def save(self):
        self.path.write_text(json.dumps({**self.data, "_meta": self.meta}))


def ground_elevations(cache: Cache, points) -> list[float | None]:
    """3DEP elevations (m NAVD88, best available resolution) at (lon, lat) points.

    The 3DEP image service's getSamples (the data behind the elevation point query service), 500 points per
    request, four requests at a time; answers are cached by point.
    """
    keys = [f"{x:.6f},{y:.6f}" for x, y in points]
    todo = sorted({k for k in keys if cache.data.get(k) is None})

    def batch(ks):
        pts = [[float(v) for v in k.split(",")] for k in ks]
        q = dict(geometry=json.dumps({"points": pts, "spatialReference": {"wkid": 4326}}),
                 geometryType="esriGeometryMultipoint", returnFirstValueOnly="true", f="json")
        for i in range(3):
            try:
                req = urllib.request.Request(DEP_SAMPLES, data=urllib.parse.urlencode(q).encode(), headers=USER_AGENT)
                with urllib.request.urlopen(req, timeout=600) as r:
                    samples = json.load(r)["samples"]
                out = {}
                for s in samples:
                    try:
                        out[ks[s["locationId"]]] = float(s["value"])
                    except (TypeError, ValueError):
                        pass
                return out
            except Exception:  # noqa: BLE001 - retried, then the batch stays missing
                if i == 2:
                    return {}
    chunks = [todo[i:i + 500] for i in range(0, len(todo), 500)]
    with ThreadPoolExecutor(4) as pool:
        for n, got in enumerate(pool.map(batch, chunks), 1):
            cache.data.update(got)
            cache.note("3DEP elevation (getSamples)", len(got))
            if n % 8 == 0:
                cache.save()
    cache.save()
    return [cache.data.get(k) for k in keys]


def segments(huc_dir: Path) -> gpd.GeoDataFrame:
    """HAND reach segments of every branch: branch, HydroID, NextDownID, feature_id, LengthKm, geometry."""
    parts = []
    for b in sorted((huc_dir / "branches").iterdir()):
        g = gpd.read_file(b / f"demDerived_reaches_split_filtered_addedAttributes_crosswalked_{b.name}.gpkg")
        parts.append(g[["HydroID", "NextDownID", "feature_id", "LengthKm", "geometry"]].assign(branch=int(b.name)))
    seg = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=parts[0].crs)
    for c in ("HydroID", "NextDownID", "feature_id"):
        seg[c] = seg[c].astype("int64")
    return seg


def run_cmd(cmd: list[str], **kw):
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    exe = Path(cmd[0])
    exe = str(exe.resolve()) if exe.exists() else (shutil.which(str(cmd[0])) or str(cmd[0]))
    subprocess.run([exe] + [str(c) for c in cmd[1:]], check=True, **kw)


# --------------------------------------------------------------------------------------------- steps

def step_region(r: Run):
    """Study region from a state inventory exported with ``hazus inventory export-state``."""
    if (r.region / "hzCensusBlock.parquet").exists():
        return print("  region exists")
    reg = r.cfg["region"]
    cmd = [r.cfg["tools"]["hazus"], "region", "create", *reg["states"], r.region]
    for k in ("counties", "tracts", "blocks"):
        if reg.get(k):
            cmd += [f"--{k}", ",".join(reg[k])]
    if reg.get("year"):
        cmd += ["--year", str(reg["year"])]
    run_cmd(cmd)


def step_hucs(r: Run):
    """Every HUC-8 that intersects the region boundary and has HAND data."""
    import s3fs

    boundary = gpd.read_parquet(r.region / "hzBoundary.parquet").to_crs(4326).union_all()
    x0, y0, x1, y1 = boundary.bounds
    q = dict(geometry=f"{x0},{y0},{x1},{y1}", geometryType="esriGeometryEnvelope", inSR=4326,
             spatialRel="esriSpatialRelIntersects", outFields="huc8,name,states", returnGeometry="true", outSR=4326,
             f="geojson")
    wbd = gpd.GeoDataFrame.from_features(get_json(WBD_HUC8, q, tries=5)["features"], crs=4326)
    wbd = wbd[wbd.intersects(boundary)].copy()
    region_area = gpd.GeoSeries([boundary], crs=4326).to_crs(ALBERS).area.iloc[0]
    wbd["share_of_region"] = wbd.intersection(boundary).to_crs(ALBERS).area / region_area
    fs = s3fs.S3FileSystem(anon=True)
    version = r.cfg["hand"]["version"]
    wbd["hand_files"] = [len(fs.ls(f"{HAND_BUCKET}/{version}/{h}")) if fs.exists(f"{HAND_BUCKET}/{version}/{h}") else 0
                         for h in wbd.huc8]
    wbd.to_file(r.path("hucs.gpkg"))
    keep = wbd[wbd.hand_files > 0]
    keep[["huc8", "name", "states", "share_of_region"]].to_csv(r.path("hucs.csv"), index=False)
    print(wbd[["huc8", "name", "share_of_region", "hand_files"]].round(4).to_string(index=False))
    print(f"  {len(keep)} watersheds with HAND data")


def step_hand(r: Run):
    """HAND data per watershed and NOAA OWP's code at the pinned commit."""
    import s3fs

    code = Path(r.cfg["hand"]["code"])
    if not (code / "tools" / "inundate_mosaic_wrapper.py").exists():
        run_cmd(["git", "clone", "--quiet", OWP_REPO, code])
    run_cmd(["git", "-C", code, "checkout", "--quiet", r.cfg["hand"]["commit"]])
    fs = s3fs.S3FileSystem(anon=True)
    version = r.cfg["hand"]["version"]
    manifest = []
    for huc in r.hucs():
        dst = r.hand / f"flood_{huc}" / huc
        src = f"{HAND_BUCKET}/{version}/{huc}"
        files = fs.find(src, detail=True)
        manifest += [dict(huc8=huc, key=k, bytes=v["size"], etag=str(v.get("ETag", "")).strip('"'),
                          last_modified=str(v.get("LastModified", ""))) for k, v in files.items()]
        todo = [(k, dst / k[len(src) + 1:]) for k, v in files.items()
                if not (dst / k[len(src) + 1:]).exists() or (dst / k[len(src) + 1:]).stat().st_size != v["size"]]
        for k, local in todo:
            local.parent.mkdir(parents=True, exist_ok=True)
            fs.get(k, str(local))
        shutil.copy2(dst / "branch_ids.csv", dst.parent / "fim_inputs.csv")
        size = sum(v["size"] for v in files.values()) / 1e9
        print(f"  {huc}: {len(files)} files, {size:.2f} GB ({len(todo)} downloaded)")
    pd.DataFrame(manifest).assign(listed=now_utc()).to_csv(r.path("hand", "manifest.csv"), index=False)


def hand_feature_ids(r: Run, huc: str) -> np.ndarray:
    ht = pd.read_feather(r.hand / f"flood_{huc}" / huc / "hydrotable.feather", columns=["feature_id"])
    return np.unique(ht["feature_id"].astype("int64"))


def step_flows(r: Run):
    """Peak flow (m3/s) per NWM reach over the event window, one CSV per watershed."""
    src = r.cfg["event"]["flows"]
    out_dir = r.path("flows", "x").parent
    hucs = r.hucs()
    if all((out_dir / f"{h}.csv").exists() for h in hucs):
        return print("  flows exist")
    fids = {h: hand_feature_ids(r, h) for h in hucs}
    wanted = np.unique(np.concatenate(list(fids.values())))
    if src == "nwm_ana":
        peak = nwm_ana_peaks(r, wanted)
    elif src == "csv":
        peak = pd.read_csv(r.cfg["event"]["flows_csv"]).set_index("feature_id")["discharge"]
    else:
        raise ValueError(f"unknown [event] flows source {src!r}")
    for h in hucs:
        q = peak.reindex(fids[h]).dropna()
        q.rename("discharge").rename_axis("feature_id").reset_index().to_csv(out_dir / f"{h}.csv", index=False)
        print(f"  {h}: flows for {len(q)} of {len(fids[h])} HAND reaches; max {q.max():.0f} m3/s")


def nwm_ana_peaks(r: Run, wanted: np.ndarray) -> pd.Series:
    """Hourly NWM analysis & assimilation (tm00, valid at the cycle hour), maximum per reach over the window."""
    import netCDF4

    hours = []
    t = r.start.replace(minute=0, second=0, microsecond=0)
    while t <= r.end:
        hours.append(t)
        t += timedelta(hours=1)
    store = r.path("flows", "nwm_ana_hourly.parquet")
    if store.exists():
        hourly = pd.read_parquet(store)
    else:
        def one(t):
            url = f"{NWM_GCS}/nwm.{t:%Y%m%d}/analysis_assim/nwm.t{t:%H}z.analysis_assim.channel_rt.tm00.conus.nc"
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "f.nc"
                for i in range(3):
                    try:
                        _, headers = urllib.request.urlretrieve(url, p)
                        got = dict(url=url, valid_time=t.isoformat(), bytes=p.stat().st_size,
                                   last_modified=headers.get("Last-Modified"), retrieved=now_utc())
                        break
                    except Exception:  # noqa: BLE001
                        if i == 2:
                            print(f"  missing {url}")
                            return None
                with netCDF4.Dataset(p) as ds:
                    fid = np.ma.filled(ds.variables["feature_id"][:], -1).astype("int64")
                    q = np.ma.filled(ds.variables["streamflow"][:].astype("float64"), np.nan)
            m = np.isin(fid, wanted)
            return got, pd.DataFrame({"feature_id": fid[m], "discharge": q[m], "time": t})
        with ThreadPoolExecutor(4) as pool:
            done = [x for x in pool.map(one, hours) if x is not None]
        pd.DataFrame([g for g, _ in done]).to_csv(r.path("flows", "nwm_ana_files.csv"), index=False)
        parts = [d for _, d in done]
        hourly = pd.concat(parts, ignore_index=True)
        hourly.to_parquet(store, index=False)
        print(f"  {len(parts)} of {len(hours)} hourly analysis files read")
    peak = hourly.groupby("feature_id")["discharge"].max()
    return peak


def coops_stations_navd(r: Run, cache: Cache, lon: float, lat: float, radius_km: float = 150) -> pd.DataFrame:
    """NOAA water-level stations within radius_km that publish MHHW and NAVD88 (MHHW in m NAVD88)."""
    st = cache.get("coops_stations", lambda: get_json(f"{COOPS}/stations.json", {"type": "waterlevels"})["stations"],
                   "NOAA CO-OPS stations and datums")
    st = pd.DataFrame(st)[["id", "name", "lat", "lng"]]
    d = np.hypot((st.lng - lon) * 111.32 * np.cos(np.radians(lat)), (st.lat - lat) * 110.57)
    st = st.assign(dist_km=d)[d <= radius_km].sort_values("dist_km")
    rows = []
    for _, s in st.iterrows():
        dat = cache.get(f"datums_{s.id}", lambda: get_json(f"{COOPS}/stations/{s.id}/datums.json", {"units": "metric"}),
                        "NOAA CO-OPS stations and datums")
        v = {x["name"]: x["value"] for x in (dat or {}).get("datums", []) or []}
        if v.get("MHHW") is not None and v.get("NAVD88") is not None:
            rows.append(dict(id=s.id, name=s["name"], lat=s.lat, lng=s.lng, mhhw_navd88=v["MHHW"] - v["NAVD88"]))
    return pd.DataFrame(rows)


def step_tidal(r: Run):
    """Mask tidal segments: walk upstream from each branch outlet while the channel is at or below local MHHW."""
    cache = Cache(r.cache / "web.json")
    for huc in r.hucs():
        out = r.path("tidal", f"{huc}_segments.gpkg")
        dst = r.masked / f"flood_{huc}"
        if out.exists() and (dst / huc / "hydrotable.feather").exists():
            print(f"  {huc}: done")
            continue
        huc_dir = r.hand / f"flood_{huc}" / huc
        seg = segments(huc_dir)
        seg["key"] = list(zip(seg.branch, seg.HydroID))
        ll = seg.to_crs(4326)
        c = ll.union_all().centroid
        stations = coops_stations_navd(r, cache, c.x, c.y)
        along = [gpd.GeoSeries(seg.geometry.interpolate(f, normalized=True), crs=seg.crs).to_crs(4326)
                 for f in (0.25, 0.5, 0.75)]
        pts = {k: [(p.x, p.y) for p in ps] for k, *ps in zip(seg["key"], *along)}
        mid = along[1]
        t0 = datetime.now()
        ground_elevations(cache, [xy for v in pts.values() for xy in v])  # all at once; the walk reads the cache
        print(f"  {huc}: elevations at {3 * len(seg)} points ({(datetime.now() - t0).total_seconds():.0f} s)",
              flush=True)
        if len(stations):
            sx, sy = stations.lng.to_numpy(), stations.lat.to_numpy()
            near = [int(np.argmin(np.hypot((sx - p.x) * np.cos(np.radians(p.y)), sy - p.y))) for p in mid]
            seg["mhhw_m"] = stations.mhhw_navd88.to_numpy()[near]
            seg["mhhw_station"] = stations.id.to_numpy()[near]
        else:
            seg["mhhw_m"], seg["mhhw_station"] = -np.inf, ""
        mhhw = dict(zip(seg["key"], seg["mhhw_m"]))

        def elev(keys):
            ground_elevations(cache, [xy for k in keys for xy in pts[k]])
            out = {}
            for k in keys:
                v = [cache.data.get(f"{x:.6f},{y:.6f}") for x, y in pts[k]]
                v = [x for x in v if x is not None]
                out[k] = float(np.median(v)) if v else np.nan
            return out

        el, tidal = {}, set()
        for b, s in seg.groupby("branch"):
            ids = set(s.HydroID)
            upstream = s.groupby("NextDownID")["HydroID"].apply(lambda h, b=b: [(b, x) for x in h]).to_dict()
            frontier = sorted((b, h) for h in s.loc[~s.NextDownID.isin(ids), "HydroID"])
            while frontier:
                el.update(elev(frontier))
                nxt = []
                for k in frontier:
                    if el[k] <= mhhw[k]:
                        tidal.add(k)
                        nxt += upstream.get(k[1], [])
                frontier = sorted(set(nxt) - set(el))
            cache.save()
        seg["channel_elev_m"] = seg["key"].map(el)
        seg["tidal"] = seg["key"].isin(tidal)
        seg.drop(columns="key").to_file(out)
        n_ch = seg.loc[seg.tidal & (seg.branch == 0), "LengthKm"].sum()
        print(f"  {huc}: {len(el)} segments checked, {len(tidal)} tidal ({n_ch:.0f} km of branch-0 channel); "
              f"MHHW from {sorted(set(seg.mhhw_station))}")
        mask_rating_tables(huc_dir, dst, huc, tidal)


def mask_rating_tables(src_huc_dir: Path, dst: Path, huc: str, tidal: set):
    """Copy of the HAND data without the tidal segments' rating-table rows (no stage, so no inundation there)."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src_huc_dir, dst / huc)
    shutil.copy2(src_huc_dir.parent / "fim_inputs.csv", dst / "fim_inputs.csv")

    def keep(df, branch=None):
        b = df["branch_id"].astype(int) if branch is None else pd.Series(int(branch), index=df.index)
        return df[[(x, int(h)) not in tidal for x, h in zip(b, df["HydroID"])]]

    d = dst / huc
    f = pd.read_feather(d / "hydrotable.feather")
    keep(f).reset_index(drop=True).to_feather(d / "hydrotable.feather")
    if (d / "hydrotable.parquet").exists():
        p = pd.read_parquet(d / "hydrotable.parquet")
        idx = [n for n in p.index.names if n is not None]
        p2 = keep(p.reset_index() if idx else p)
        (p2.set_index(idx) if idx else p2).to_parquet(d / "hydrotable.parquet")
    if (d / "hydrotable.csv").exists():
        keep(pd.read_csv(d / "hydrotable.csv", low_memory=False)).to_csv(d / "hydrotable.csv", index=False)
    for b in (d / "branches").iterdir():
        p = b / f"hydroTable_{b.name}.csv"
        if p.exists():
            keep(pd.read_csv(p, low_memory=False), b.name).to_csv(p, index=False)


def step_inundate(r: Run):
    """OWP inundation (inundate_mosaic_wrapper) per watershed; depth int16 mm -> float32 metres, dry = NoData."""
    code = Path(r.cfg["hand"]["code"]).resolve()
    for huc in r.hucs():
        out = r.path("depth", f"{huc}_depth_m.tif")
        if out.exists():
            print(f"  {huc}: done")
            continue
        tmp = r.path("depth", "owp", f"{huc}_depth.tif")
        env = dict(os.environ, PYTHONPATH=f"{code / 'src'}{os.pathsep}{code}", inputsDir="inputs", outputsDir="output")
        run_cmd([sys.executable, "inundate_mosaic_wrapper.py", "-y", (r.masked / f"flood_{huc}").resolve(), "-u", huc,
                 "-f", (r.work / "flows" / f"{huc}.csv").resolve(), "-i", tmp.with_name(f"{huc}_extent.tif").resolve(),
                 "-d", tmp.resolve()], cwd=code / "tools", env=env, stdout=subprocess.DEVNULL)
        with rasterio.open(tmp) as d:
            a = d.read(1, masked=True).astype("float32").filled(np.nan)
            prof = d.profile
        a = np.where(a > 0, a / 1000.0, np.nan).astype("float32")
        prof.update(dtype="float32", nodata=np.nan, crs=f"EPSG:{ALBERS}", compress="deflate", predictor=3,
                    tiled=True, blockxsize=256, blockysize=256)
        with rasterio.open(out, "w", **prof) as o:
            o.write(a, 1)
        print(f"  {huc}: wet {np.isfinite(a).sum() * abs(prof['transform'].a * prof['transform'].e) / 1e6:.1f} km2")


def step_losses(r: Run):
    out = r.work / "results"
    if (out / "flFRGBSEcLossByTotal.parquet").exists():
        return print("  results exist")
    grids = sorted((r.work / "depth").glob("*_depth_m.tif"))
    cmd = [r.cfg["tools"]["hazus"], "flood", "run", r.region, out, "--units", "meters"]
    for g in grids:
        cmd += ["--riverine", g]
    run_cmd(cmd + ["--work", r.work / "hazus_grids"])
    summarize_losses(r)


def summarize_losses(r: Run):
    res = r.work / "results"
    cols = ["BuildingLoss", "ContentsLoss", "InventoryLoss", "RelocationCost", "IncomeLoss", "RentalIncomeLoss",
            "WageLoss"]
    L = pd.read_parquet(res / "flFRGBSEcLossByTotal.parquet")
    C = pd.read_parquet(res / "flFRGBSDmgCountGOccupB.parquet")
    dmg = [c for c in C.columns if c.startswith("CountDmg")]
    S = pd.read_parquet(res / "flFRShelter.parquet")
    D = pd.read_parquet(res / "flFRDebris.parquet")
    aw = pd.read_parquet(res / "flAnAreaWeighted.parquet")
    s = {"flooded census blocks": int(aw.CensusBlock.nunique()),
         "direct economic loss ($M)": L[cols].to_numpy().sum() / 1e3,
         "  building + contents ($M)": (L.BuildingLoss + L.ContentsLoss).sum() / 1e3,
         "damaged buildings": C[dmg].to_numpy().sum(),
         "  more than 50 % damaged": C[[c for c in dmg if int(c.split("to")[0][8:]) > 50]].to_numpy().sum(),
         "displaced people": S.DisplacedPop.sum(), "short-term shelter": S.ShortTermNeeds.sum(),
         "debris (kt)": D[[c for c in D.columns if c.endswith("Tons")]].to_numpy().sum() / 1e3}
    by_block = L.assign(loss=L[cols].sum(axis=1)).groupby("CensusBlock").loss.sum().sort_values(ascending=False)
    s["share of loss in the top 5 blocks"] = by_block.head(5).sum() / by_block.sum()
    pd.Series(s).to_csv(r.path("summary", "losses.csv"), header=["value"])
    print(pd.Series(s).round(2).to_string())


# --------------------------------------------------------------------------------------------- evaluation

def step_evaluate(r: Run):
    """Diagnostics for a historical event. Nothing here feeds back into the workflow."""
    cache = Cache(r.cache / "web.json")
    evaluate_hwms(r, cache)
    evaluate_nfip(r, cache)
    evaluate_gauges(r, cache)
    cache.save()


def usgs_series(cache: Cache, site: str, param: str, start: datetime, end: datetime) -> list[tuple[str, float]]:
    def fetch():
        q = dict(monitoring_location_id=f"USGS-{site}", parameter_code=param,
                 datetime=f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}", limit=10000,
                 properties="time,value", skipGeometry="true", f="json")
        return [(f["properties"]["time"], float(f["properties"]["value"]))
                for f in get_json(f"{USGS_OGC}/continuous/items", q)["features"]
                if f["properties"]["value"] not in (None, "")]
    return cache.get(f"usgs_{site}_{param}_{start:%Y%m%d}_{end:%Y%m%d}", fetch, "USGS water data (gauge series)")


def usgs_site(cache: Cache, site: str) -> dict:
    return cache.get(f"usgs_site_{site}", lambda: get_json(f"{USGS_OGC}/monitoring-locations/items/USGS-{site}",
                                                           {"f": "json"}), "USGS water data (sites)")


def ngvd29_to_navd88(cache: Cache, lon: float, lat: float) -> float:
    def fetch():
        q = dict(lat=f"{lat:.6f}", lon=f"{lon:.6f}", inDatum="NAD83(2011)", outDatum="NAD83(2011)",
                 inVertDatum="NGVD29", outVertDatum="NAVD88", orthoHt=100, orthoHtUnits="m")
        return float(get_json(NCAT, q)["destOrthoht"]) - 100
    return cache.get(f"ncat_{lat:.5f}_{lon:.5f}", fetch, "NOAA NCAT datum conversion")


def evaluate_gauges(r: Run, cache: Cache):
    """Peak flow (NWM vs observed) and peak water surface (HAND at the NWM flow vs observed) at USGS gauges."""
    lo, hi = r.start - timedelta(days=1), r.end + timedelta(days=1)
    rows = []
    for huc in r.hucs():
        flows = pd.read_csv(r.work / "flows" / f"{huc}.csv").set_index("feature_id")["discharge"]
        d = r.masked / f"flood_{huc}" / huc
        elev = pd.read_csv(d / "usgs_elev_table.csv", dtype={"location_id": str})
        ht = pd.read_feather(d / "hydrotable.feather")
        curves = {(int(b), int(h)): (g.discharge_cms.to_numpy(), g.stage.to_numpy())
                  for (b, h), g in ht.sort_values("discharge_cms").groupby(["branch_id", "HydroID"])}
        elev = elev.dropna(subset=["HydroID", "levpa_id", "feature_id"])
        for site, e in elev[elev.location_id.str.fullmatch(r"\d{8}")].groupby("location_id"):
            e = e.sort_values("levpa_id")  # branch 0 first
            row = e.iloc[0]
            try:
                info = usgs_site(cache, site)
            except Exception as e:  # noqa: BLE001 - not cached, so a rerun tries again
                missing = isinstance(e, urllib.error.HTTPError) and e.code == 404
                rows.append(dict(huc8=huc, gauge=site, note="no site record" if missing else "request failed"))
                continue
            p, (lon, lat) = info["properties"], info["geometry"]["coordinates"][:2]
            try:
                q_obs = usgs_series(cache, site, "00060", lo, hi)
                h_obs = usgs_series(cache, site, "00065", lo, hi)
            except Exception:  # noqa: BLE001 - not cached, so a rerun tries again
                rows.append(dict(huc8=huc, gauge=site, note="request failed"))
                continue
            finally:
                cache.save()
            fid = int(row.feature_id)
            out = dict(huc8=huc, gauge=site, name=p.get("monitoring_location_name"), feature_id=fid,
                       q_nwm_cfs=flows.get(fid, np.nan) * FT ** 3,
                       q_obs_cfs=max(v for _, v in q_obs) if q_obs else np.nan)
            key = (int(row.levpa_id), int(row.HydroID))
            if h_obs and p.get("altitude") is not None and key in curves and fid in flows.index:
                shift = ngvd29_to_navd88(cache, lon, lat) if p.get("vertical_datum") == "NGVD29" else 0.0
                out["wse_obs_ft"] = p["altitude"] + max(v for _, v in h_obs) + shift * FT
                out["wse_hand_ft"] = (row.dem_adj_elevation + np.interp(flows[fid], *curves[key])) * FT
            elif key not in curves:
                out["note"] = "tidal segment (masked)"
            rows.append(out)
    g = pd.DataFrame(rows)
    g["q_ratio"] = g.q_nwm_cfs / g.q_obs_cfs
    if "wse_obs_ft" in g:
        g["wse_error_ft"] = g.wse_hand_ft - g.wse_obs_ft
    g.to_csv(r.path("evaluation", "gauges.csv"), index=False)
    print(g[[c for c in ["gauge", "name", "q_obs_cfs", "q_ratio", "wse_error_ft", "note"] if c in g]]
          .round(2).to_string(index=False))
    ok = g.q_ratio.dropna()
    print(f"  NWM / observed peak flow: median {ok.median():.2f} ({len(ok)} gauges); "
          f"HAND water surface error: median |e| {g.get('wse_error_ft', pd.Series(dtype=float)).abs().median():.1f} ft")


def evaluate_hwms(r: Run, cache: Cache):
    """Depth grid vs USGS high-water marks (STN). Observed depth = mark elevation - 3DEP ground at the mark."""
    ev = r.cfg.get("evaluate", {})
    if not ev.get("stn_event"):
        return
    hucs = gpd.read_file(r.work / "hucs.gpkg")
    hucs = hucs[hucs.huc8.isin(r.hucs())]
    states = sorted({s for v in hucs.states for s in str(v).split(",") if s})
    marks = []
    for st in states:
        marks += cache.get(f"stn_{ev['stn_event']}_{st}", lambda st=st: get_json(
            f"{STN}/hwms/filteredhwms/", {"event": ev["stn_event"], "states": st}, timeout=300),
            "USGS STN high-water marks")
    h = pd.DataFrame(marks)
    h = gpd.GeoDataFrame(h, geometry=gpd.points_from_xy(h.longitude_dd, h.latitude_dd), crs=4326)
    h = h[h.within(hucs.union_all()) & (h.vertical_datum_name == "NAVD88") & h.elev_ft.notna()].copy()
    h["ground_m"] = ground_elevations(cache, list(zip(h.longitude_dd, h.latitude_dd)))
    h["obs_depth_ft"] = h.elev_ft - h.ground_m * FT
    hag = pd.to_numeric(h.height_above_ground, errors="coerce")
    # position check: water cannot be below the ground at a mark, and the lidar depth must agree with the depth
    # measured in the field when there is one; marks failing it are mislocated (phone GPS on steep banks)
    h["qa"] = (h.obs_depth_ft >= -1) & (hag.isna() | ((h.obs_depth_ft - hag).abs() <= 3))
    xy = list(zip(h.to_crs(ALBERS).geometry.x, h.to_crs(ALBERS).geometry.y))
    depth = np.zeros(len(h))
    for g in sorted((r.work / "depth").glob("*_depth_m.tif")):
        with rasterio.open(g) as d:
            v = np.array([s[0] for s in d.sample(xy)], dtype="float64")
        depth = np.fmax(depth, np.nan_to_num(v, nan=0.0))
    h["model_depth_ft"] = depth * FT
    h["error_ft"] = h.model_depth_ft - h.obs_depth_ft
    h.drop(columns="geometry").to_csv(r.path("evaluation", "hwms.csv"), index=False)
    q = h[h.qa]
    rows = []
    for label, m in [("all", q.index)] + [(s, q.index[q.state_name == s]) for s in sorted(q.state_name.unique())]:
        e = q.loc[m, "error_ft"]
        rows.append(dict(marks=label, n=len(e), bias_ft=e.mean(), mae_ft=e.abs().mean(),
                         rmse_ft=np.sqrt((e ** 2).mean()), within_2ft=(e.abs() <= 2).mean(),
                         modeled_wet=(q.loc[m, "model_depth_ft"] > 0).mean()))
    t = pd.DataFrame(rows)
    t.to_csv(r.path("evaluation", "hwm_summary.csv"), index=False)
    print(f"  {len(h)} marks in the watersheds, {int(h.qa.sum())} pass the position check")
    print(t.round(2).to_string(index=False))


def evaluate_nfip(r: Run, cache: Cache):
    """Modeled building + contents loss vs NFIP claims (OpenFEMA, redacted), by census block group."""
    counties = pd.read_parquet(r.region / "hzCounty.parquet")["CountyFips"].astype(str).str.zfill(5)
    lo, hi = (r.start - timedelta(days=1)).date(), (r.end + timedelta(days=1)).date()
    claims = []
    for c in counties:
        q = {"$filter": f"countyCode eq '{c}' and dateOfLoss ge '{lo}T00:00:00.000Z' and "
                        f"dateOfLoss le '{hi}T23:59:59.000Z'", "$top": 10000}
        claims += get_json(OPENFEMA, q, timeout=300)["NfipClaims"]
    cache.note("OpenFEMA NFIP claims (v3)", max(len(claims), 1))
    c = pd.DataFrame(claims)
    c.to_csv(r.path("evaluation", "nfip_claims.csv"), index=False)
    if c.empty:
        return print("  no NFIP claims in the window")
    c["damage"] = c.buildingDamageAmount.fillna(0) + c.contentsDamageAmount.fillna(0)
    nfip = c.groupby("censusGeoid").agg(claims=("id", "size"), damage=("damage", "sum"))
    L = pd.read_parquet(r.work / "results" / "flFRGBSEcLossByTotal.parquet")
    model = (L.assign(bc=(L.BuildingLoss + L.ContentsLoss) * 1e3).groupby(L.CensusBlock.str[:12]).bc.sum())
    j = nfip.join(model.rename("model"), how="outer").fillna(0.0)
    with_claims = j.claims > 0
    s = {"NFIP claims": int(j.claims.sum()), "NFIP assessed building + contents damage ($M)": j.damage.sum() / 1e6,
         "block groups with claims": int(with_claims.sum()),
         "modeled building + contents loss ($M)": j.model.sum() / 1e6,
         "share of claim block groups with modeled loss": (j.loc[with_claims, "model"] > 0).mean(),
         "share of claims in those block groups": j.loc[with_claims & (j.model > 0), "claims"].sum() / j.claims.sum(),
         "share of modeled loss in claim block groups": j.loc[with_claims, "model"].sum() / max(j.model.sum(), 1)}
    j.to_csv(r.path("evaluation", "nfip_block_groups.csv"))
    pd.Series(s).to_csv(r.path("evaluation", "nfip_summary.csv"), header=["value"])
    print(pd.Series(s).round(2).to_string())


# --------------------------------------------------------------------------------------------- provenance

def rel(path) -> str:
    """Path relative to the working directory when it lies below it, so provenance holds no machine paths."""
    p = Path(path).resolve()
    try:
        return p.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree_digest(root: Path) -> dict:
    """sha256 over the relative path and sha256 of every file under root."""
    files = sorted(p for p in root.rglob("*") if p.is_file())
    h = hashlib.sha256()
    for p in files:
        h.update(f"{p.relative_to(root).as_posix()}\t{sha256(p)}\n".encode())
    return {"path": rel(root), "files": len(files), "bytes": sum(p.stat().st_size for p in files),
            "sha256": h.hexdigest()}


def git_info(path: Path) -> dict:
    def g(*args):
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None
    status = g("status", "--porcelain")
    return {"commit": g("rev-parse", "HEAD"), "describe": g("describe", "--tags", "--always"),
            "uncommitted_changes": bool(status) if status is not None else None}


ENV_PROBE = """
import importlib.metadata as m, json, platform, sys
pkgs = sorted({d.metadata["Name"] + "==" + d.version for d in m.distributions()}, key=str.lower)
print(json.dumps({"python": sys.version.split()[0], "platform": platform.platform(), "packages": pkgs}))
"""


def environment(python: str) -> dict:
    """Python version, platform and installed distributions of an environment."""
    out = subprocess.run([python, "-I", "-c", ENV_PROBE], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def nwm_manifest(r: Run) -> pd.DataFrame | None:
    """The NWM files of the flows step. For a run made before manifests were written, the archive's current
    headers (Last-Modified, size) are looked up instead, and marked as such."""
    p = r.work / "flows" / "nwm_ana_files.csv"
    if p.exists():
        return pd.read_csv(p)
    hourly = r.work / "flows" / "nwm_ana_hourly.parquet"
    if r.cfg["event"]["flows"] != "nwm_ana" or not hourly.exists():
        return None
    hours = sorted(pd.read_parquet(hourly, columns=["time"]).time.unique())

    def head(t):
        t = pd.Timestamp(t)
        url = f"{NWM_GCS}/nwm.{t:%Y%m%d}/analysis_assim/nwm.t{t:%H}z.analysis_assim.channel_rt.tm00.conus.nc"
        req = urllib.request.Request(url, method="HEAD", headers=USER_AGENT)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return dict(url=url, valid_time=t.isoformat(), bytes=int(resp.headers.get("Content-Length", 0)),
                        last_modified=resp.headers.get("Last-Modified"), retrieved=None,
                        note=f"headers looked up {now_utc()}; the files were read earlier")
    with ThreadPoolExecutor(8) as pool:
        m = pd.DataFrame(list(pool.map(head, hours)))
    m.to_csv(p, index=False)
    return m


def step_provenance(r: Run):
    """provenance.json: configuration, code and data versions, retrieval times, environments, step timings."""
    script = Path(__file__).resolve()
    prov = {"written": now_utc(),
            "config": {"path": rel(r.config_path), "sha256": sha256(r.config_path),
                       "text": r.config_path.read_text(encoding="utf-8")},
            "workflow": {"script": rel(script), "sha256": sha256(script), "repository": git_info(script.parent)}}
    steps = r.work / "steps.json"
    prov["steps"] = json.loads(steps.read_text()) if steps.exists() else {}

    env = {"workflow": environment(sys.executable)}
    hz = Path(r.cfg["tools"]["hazus"])
    hz = hz.resolve() if hz.exists() else Path(shutil.which(str(hz)) or hz)
    hz_python = next((p for p in (hz.parent / "python.exe", hz.parent / "python") if p.exists()), None)
    if hz_python:
        env["pyhazus"] = environment(str(hz_python))
    prov["environments"] = env

    data = {"state_inventories": [tree_digest(Path(s)) for s in r.cfg["region"]["states"] if Path(s).exists()]}
    hand = r.work / "hand" / "manifest.csv"
    if hand.exists():
        m = pd.read_csv(hand, dtype={"huc8": str})
        data["hand"] = {"bucket": HAND_BUCKET, "version": r.cfg["hand"]["version"],
                        "watersheds": sorted(m.huc8.unique()), "files": len(m), "bytes": int(m.bytes.sum()),
                        "newest_last_modified": m.last_modified.max(), "listed": m.listed.iloc[0],
                        "manifest": rel(hand)}
    code = Path(r.cfg["hand"]["code"])
    if code.exists():
        data["owp_code"] = {"repository": OWP_REPO, "configured_commit": r.cfg["hand"]["commit"], **git_info(code)}
    if r.cfg["event"]["flows"] == "csv":
        f = Path(r.cfg["event"]["flows_csv"])
        data["flows"] = {"source": "csv", "path": rel(f), "sha256": sha256(f) if f.exists() else None}
    else:
        m = nwm_manifest(r)
        if m is not None:
            lm = pd.to_datetime(m.last_modified.dropna(), format="%a, %d %b %Y %H:%M:%S %Z", utc=True)
            data["flows"] = {"source": "NWM analysis & assimilation (tm00, CONUS channel_rt)", "files": len(m),
                             "first_valid": m.valid_time.min(), "last_valid": m.valid_time.max(),
                             "archive_last_modified": [lm.min().isoformat(), lm.max().isoformat()],
                             "manifest": rel(r.work / "flows" / "nwm_ana_files.csv")}
    cache_file = r.cache / "web.json"
    if cache_file.exists():
        c = Cache(cache_file)
        written = datetime.fromtimestamp(cache_file.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        def service(key: str) -> str:
            if key[:1].isdigit() or key[:1] == "-":
                return "3DEP elevation (getSamples)"
            for prefix, name in (("coops_", "NOAA CO-OPS stations and datums"),
                                 ("datums_", "NOAA CO-OPS stations and datums"),
                                 ("usgs_site_", "USGS water data (sites)"), ("usgs_", "USGS water data (gauge series)"),
                                 ("stn_", "USGS STN high-water marks"), ("ncat_", "NOAA NCAT datum conversion")):
                if key.startswith(prefix):
                    return name
            return "other"
        cached = pd.Series([service(k) for k in c.data]).value_counts().to_dict()
        services = {}
        for name in sorted(set(cached) | set(c.meta)):
            services[name] = {"cached_answers": int(cached.get(name, 0)), **c.meta.get(name, {})}
            if name not in c.meta:
                services[name]["note"] = f"retrieved before retrieval times were tracked (cache written {written})"
        data["web_services"] = services
        data["web_cache"] = {"path": rel(cache_file), "entries": len(c.data), "sha256": sha256(cache_file)}
    prov["data"] = data
    out = r.work / "provenance.json"
    out.write_text(json.dumps(prov, indent=1, default=str), encoding="utf-8")
    print(f"  {rel(out)}")


STEPS = {"region": step_region, "hucs": step_hucs, "hand": step_hand, "flows": step_flows, "tidal": step_tidal,
         "inundate": step_inundate, "losses": step_losses, "evaluate": step_evaluate, "provenance": step_provenance}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", type=Path)
    ap.add_argument("steps", nargs="+", choices=[*STEPS, "all"])
    a = ap.parse_args()
    r = Run(a.config)
    todo = [s for s in STEPS if s != "provenance"] if "all" in a.steps else a.steps
    steps_file = r.work / "steps.json"
    for s in todo:
        print(f"[{s}]", flush=True)
        t, started = datetime.now(), now_utc()
        STEPS[s](r)
        seconds = (datetime.now() - t).total_seconds()
        print(f"[{s}] {seconds:.0f} s", flush=True)
        if s != "provenance":
            log = json.loads(steps_file.read_text()) if steps_file.exists() else {}
            log[s] = {"started": started, "finished": now_utc(), "seconds": round(seconds, 1)}
            steps_file.write_text(json.dumps(log, indent=1))
    if "provenance" not in todo:
        print("[provenance]", flush=True)
        step_provenance(r)


if __name__ == "__main__":
    main()
