"""SQLite storage. Built from the raw JSON cache (data/cache/), so it can always be rebuilt.

Tables
  daily_weather  one row per hub per local calendar day (Open-Meteo gridded estimates)
  grid_points    the grid cell Open-Meteo returned for each hub
  nri_county     the FEMA NRI record for each hub county (JSON)
  nri_caps       national 99th-percentile cap per NRI frequency field
  messages       chat history per session (Anthropic message blocks as JSON)
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from app.config import hubs, path, settings
from app.sources.fema_nri import HAZARDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_weather (
  hub_id TEXT, date TEXT, snowfall_cm REAL, precip_mm REAL, tmax_c REAL, tmin_c REAL,
  gust_kmh REAL, PRIMARY KEY (hub_id, date));
CREATE TABLE IF NOT EXISTS grid_points (hub_id TEXT PRIMARY KEY, lat REAL, lon REAL, elevation REAL);
CREATE TABLE IF NOT EXISTS nri_county (fips TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS nri_caps (field TEXT PRIMARY KEY, cap REAL);
CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT, seq INTEGER, message TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (session_id, seq));
"""


@contextmanager
def connect():
    conn = sqlite3.connect(path("db_path"))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (same as numpy's default)."""
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def build_from_cache() -> dict:
    """(Re)load every cached source file into SQLite. Returns what was loaded."""
    cache = path("cache_dir")
    loaded = {"weather_hubs": [], "nri": False}
    with connect() as db:
        db.executescript(SCHEMA)
        for h in hubs():
            f = cache / f"open_meteo_{h['id']}.json"
            if not f.exists():
                continue
            body = json.loads(f.read_text(encoding="utf-8"))
            d = body["daily"]
            rows = list(zip([h["id"]] * len(d["time"]), d["time"], d["snowfall_sum"],
                            d["precipitation_sum"], d["temperature_2m_max"],
                            d["temperature_2m_min"], d["wind_gusts_10m_max"]))
            db.execute("DELETE FROM daily_weather WHERE hub_id=?", (h["id"],))
            db.executemany("INSERT INTO daily_weather VALUES (?,?,?,?,?,?,?)", rows)
            db.execute("INSERT OR REPLACE INTO grid_points VALUES (?,?,?,?)",
                       (h["id"], body["latitude"], body["longitude"], body.get("elevation")))
            loaded["weather_hubs"].append(h["id"])
        hub_file, nat_file = cache / "nri_hubs.json", cache / "nri_national.json"
        if hub_file.exists() and nat_file.exists():
            for fips, rec in json.loads(hub_file.read_text(encoding="utf-8")).items():
                db.execute("INSERT OR REPLACE INTO nri_county VALUES (?,?)", (fips, json.dumps(rec)))
            national = json.loads(nat_file.read_text(encoding="utf-8"))
            p = settings()["normalisation"]["nri_cap_percentile"]
            for hz in HAZARDS:
                # A null frequency means FEMA does not consider the hazard applicable -> 0.
                vals = [r.get(f"{hz}_AFREQ") or 0.0 for r in national]
                db.execute("INSERT OR REPLACE INTO nri_caps VALUES (?,?)",
                           (f"{hz}_AFREQ", percentile(vals, p)))
            loaded["nri"] = True
    return loaded


# ---------- reads ----------

def daily(hub_id: str, year: int | None = None) -> list[sqlite3.Row]:
    q, args = "SELECT * FROM daily_weather WHERE hub_id=?", [hub_id]
    if year is not None:
        q += " AND substr(date,1,4)=?"
        args.append(str(year))
    with connect() as db:
        return db.execute(q + " ORDER BY date", args).fetchall()


def grid_point(hub_id: str) -> dict | None:
    with connect() as db:
        r = db.execute("SELECT * FROM grid_points WHERE hub_id=?", (hub_id,)).fetchone()
        return dict(r) if r else None


def nri(fips: str) -> dict | None:
    with connect() as db:
        r = db.execute("SELECT data FROM nri_county WHERE fips=?", (fips,)).fetchone()
        return json.loads(r["data"]) if r else None


def nri_caps() -> dict[str, float]:
    with connect() as db:
        return {r["field"]: r["cap"] for r in db.execute("SELECT * FROM nri_caps")}


def has_weather(hub_id: str) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM daily_weather WHERE hub_id=? LIMIT 1",
                          (hub_id,)).fetchone() is not None


# ---------- chat history ----------

def load_messages(session_id: str) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT message FROM messages WHERE session_id=? ORDER BY seq",
                          (session_id,)).fetchall()
        return [json.loads(r["message"]) for r in rows]


def append_messages(session_id: str, msgs: list[dict]) -> None:
    """Append-only: earlier turns are never edited (keeps thinking blocks valid)."""
    with connect() as db:
        start = db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE session_id=?",
                           (session_id,)).fetchone()[0]
        db.executemany("INSERT INTO messages (session_id, seq, message) VALUES (?,?,?)",
                       [(session_id, start + i, json.dumps(m)) for i, m in enumerate(msgs)])


def ensure_schema() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
