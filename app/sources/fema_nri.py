"""FEMA National Risk Index (county level) via FEMA's public ArcGIS FeatureServer.

Two things are fetched and cached as raw JSON in data/cache/:
  * nri_hubs.json      - the NRI record for each hub's county (frequencies, ratings, context)
  * nri_national.json  - one annual-frequency column per hazard for ALL US counties, used
                         only to compute the national 99th-percentile normalisation caps.
"""
from __future__ import annotations

import json

import requests

from app.config import hubs, path

URL = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
       "National_Risk_Index_Counties/FeatureServer/0/query")

HAZARDS = {  # NRI prefix -> human name
    "HRCN": "Hurricane", "CFLD": "Coastal Flooding", "IFLD": "Inland Flooding",
    "WNTW": "Winter Weather", "ISTM": "Ice Storm", "CWAV": "Cold Wave",
    "TRND": "Tornado", "SWND": "Strong Wind", "HWAV": "Heat Wave",
}
# Context-only fields (never used in scoring).
CONTEXT = ["STCOFIPS", "COUNTY", "STATEABBRV", "NRI_VER", "RISK_SCORE", "RISK_RATNG",
           "SOVI_SCORE", "RESL_SCORE"]
PER_HAZARD = ["AFREQ", "EVNTS", "RISKS", "RISKR"]


def _query(where: str, fields: list[str], offset: int = 0, page: int = 2000) -> dict:
    r = requests.get(URL, params={
        "where": where, "outFields": ",".join(fields), "returnGeometry": "false",
        "f": "json", "resultOffset": offset, "resultRecordCount": page,
        "orderByFields": "STCOFIPS",
    }, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"NRI query failed: {data['error']}")
    return data


def fetch_hub_counties() -> dict:
    fips = ",".join(f"'{h['fips']}'" for h in hubs())
    fields = CONTEXT + [f"{hz}_{p}" for hz in HAZARDS for p in PER_HAZARD]
    data = _query(f"STCOFIPS IN ({fips})", fields)
    records = {f["attributes"]["STCOFIPS"]: f["attributes"] for f in data["features"]}
    missing = {h["fips"] for h in hubs()} - set(records)
    if missing:
        raise RuntimeError(f"NRI returned no record for FIPS {sorted(missing)}")
    return records


def fetch_national_frequencies() -> list[dict]:
    fields = ["STCOFIPS"] + [f"{hz}_AFREQ" for hz in HAZARDS]
    rows, offset = [], 0
    while True:
        data = _query("1=1", fields, offset=offset)
        batch = [f["attributes"] for f in data["features"]]
        rows.extend(batch)
        if not data.get("exceededTransferLimit") and len(batch) < 2000:
            break
        if not batch:
            break
        offset += len(batch)
    return rows


def refresh() -> None:
    cache = path("cache_dir")
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "nri_hubs.json").write_text(json.dumps(fetch_hub_counties(), indent=1))
    national = fetch_national_frequencies()
    (cache / "nri_national.json").write_text(json.dumps(national))
    print(f"NRI: cached {len(hubs())} hub counties and {len(national)} national rows")
