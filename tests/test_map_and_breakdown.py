"""US hub map (projection, map asset, /map/hubs) and the headline/detail figure split."""
import json
import re

import pytest
from pydantic import ValidationError

from app import geo, main, scoring, store
from app.config import ROOT, hubs
from app.schemas import AgentAnswer, answer_json_schema
from evals import run as eval_run

MAP = json.loads((ROOT / "app" / "static" / "us-states.json").read_text(encoding="utf-8"))
STATE_NAMES = {"IL": "Illinois", "MN": "Minnesota", "MI": "Michigan", "CO": "Colorado",
               "TX": "Texas", "FL": "Florida"}
# Reference positions from d3-geo 3.1.1: geoAlbersUsa().scale(1300).translate([487.5, 305]),
# the projection us-atlas states-albers-10m.json was built with.
D3_REFERENCE = {"chicago": (638.2196, 226.1123), "minneapolis": (541.8521, 161.002),
                "detroit": (713.2013, 207.2458), "denver": (342.8454, 273.8846),
                "dallas": (482.9086, 440.5339), "miami": (822.7383, 572.5408),
                "houston": (510.4277, 509.2311)}


def setup_module():
    store.build_from_cache()


# ---------- projection and map asset ----------

@pytest.mark.parametrize("hub", hubs(), ids=lambda h: h["id"])
def test_projection_matches_d3_reference(hub):
    x, y = geo.albers_usa(hub["lon"], hub["lat"])
    rx, ry = D3_REFERENCE[hub["id"]]
    assert abs(x - rx) < 0.1 and abs(y - ry) < 0.1


def test_projection_rejects_points_outside_the_contiguous_us():
    with pytest.raises(ValueError):
        geo.albers_usa(-149.9, 61.2)  # Anchorage (an inset on the map, not supported)


def test_map_asset_has_all_states_and_attribution():
    assert MAP["viewBox"] == "0 0 975 610"
    assert len(MAP["states"]) == 51  # 50 states + DC
    assert "us-atlas" in MAP["attribution"] and "Census" in MAP["attribution"]
    assert all(s["d"].startswith("M") and s["d"].endswith("Z") for s in MAP["states"])


def _rings(d):
    for ring in re.findall(r"M([^Z]+)Z", d):
        yield [tuple(map(float, p.split())) for p in ring.split("L")]


def _inside(pt, ring):  # even-odd ray casting
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


@pytest.mark.parametrize("hub", hubs(), ids=lambda h: h["id"])
def test_each_hub_marker_lands_inside_its_own_state(hub):
    """Catches any mismatch between the map shapes and the hub projection."""
    pt = geo.albers_usa(hub["lon"], hub["lat"])
    shape = next(s for s in MAP["states"] if s["name"] == STATE_NAMES[hub["state"]])
    assert sum(_inside(pt, r) for r in _rings(shape["d"])) % 2 == 1
    others = [s["name"] for s in MAP["states"] if s["name"] != STATE_NAMES[hub["state"]]
              and sum(_inside(pt, r) for r in _rings(s["d"])) % 2 == 1]
    assert others == []


def test_map_hubs_endpoint_positions_and_scores():
    rows = main.map_hubs()
    ranked = {r["hub_id"]: r for r in scoring.rank()}
    assert [r["id"] for r in rows] == [h["id"] for h in hubs()]
    for r in rows:
        assert 0 <= r["x"] <= 975 and 0 <= r["y"] <= 610
        assert (r["overall_score"], r["tier"], r["rank"]) == (
            ranked[r["id"]]["overall_score"], ranked[r["id"]]["tier"], ranked[r["id"]]["rank"])
        assert {"city", "state", "region", "county"} <= r.keys()


def test_static_map_file_is_mounted():
    from pathlib import Path
    mount = next(r for r in main.app.routes if getattr(r, "path", "") == "/static")
    assert (Path(mount.app.directory) / "us-states.json").exists()


# ---------- headline / detail figures ----------

def answer(**figure):
    return {"in_scope": True, "answer": "43 of 365 days.", "hubs": [], "reasoning_steps": [],
            "data_sources": [], "assumptions": ["x"], "confidence": "medium",
            "follow_up_suggestions": [],
            "figures": [{"label": "Share of days", "value": 11.8, "unit": "%",
                         "source_tool": "weather_stat", **figure}]}


def test_figure_role_is_required_and_enforced_by_the_schema():
    AgentAnswer.model_validate(answer(role="headline"))
    AgentAnswer.model_validate(answer(role="detail"))
    with pytest.raises(ValidationError):
        AgentAnswer.model_validate(answer())
    with pytest.raises(ValidationError):
        AgentAnswer.model_validate(answer(role="summary"))
    item = answer_json_schema()["properties"]["figures"]["items"]
    assert "role" in item["required"] and item["properties"]["role"]["enum"] == ["headline", "detail"]


DENVER = {"id": "denver_snow_last_year", "turns": ["q"], "in_scope": True,
          "figure": {"hub_id": "denver", "metric": "snowfall", "year": 2025, "threshold": None,
                     "field": "percent_of_days", "tol": 0.5, "role": "headline"},
          "no_month_list": True}


def checks_for(text, role):
    result = {"tool_calls": [], "answer": {**answer(role=role), "answer": text}}
    return {name: ok for name, ok, _ in eval_run.check_case(DENVER, result)}


def test_headline_role_check():
    assert checks_for("43 of 365 days (11.8%).", "headline")["figure is headline"] is True
    assert checks_for("43 of 365 days (11.8%).", "detail")["figure is headline"] is False


@pytest.mark.parametrize("text,ok", [
    ("Snow fell on 43 of 365 days, mostly from January to April.", True),
    ("It may snow; most fell in January, February and March.", True),          # 3 months: ok
    ("January 11, February 10, March 9, April 7, May 1, November 2, December 3.", False),
    ("Jan: 11, Feb: 10, Mar: 9, Apr: 7", False),
    ("2025-01: 11, 2025-02: 10, 2025-03: 9, 2025-12: 3", False),
])
def test_no_month_list_check(text, ok):
    assert checks_for(text, "headline")["no month list in answer"] is ok


def test_system_prompt_asks_for_main_result_first_and_detail_figures():
    from app import agent
    p = agent._system_prompt()
    assert 'collapsed "Breakdown"' in p and 'role "detail"' in p and 'role "headline"' in p


# ---------- atlas redesign: full state names, graticule, favicon ----------

def test_map_hubs_include_full_state_names():
    names = {r["id"]: r["state_name"] for r in main.map_hubs()}
    assert names["houston"] == "Texas" and names["denver"] == "Colorado"
    assert names["miami"] == "Florida" and names["minneapolis"] == "Minnesota"


def test_state_names_cover_every_map_state():
    assert set(geo.STATE_NAMES.values()) == {s["name"] for s in MAP["states"]}


def test_map_asset_has_lower48_clip_list_and_graticule():
    assert len(MAP["lower48"]) == 49 and "Alaska" not in MAP["lower48"] and "Hawaii" not in MAP["lower48"]
    assert len(MAP["graticule"]) >= 15
    for d in MAP["graticule"]:  # lines may run past the edge (the UI clips them to land),
        pts = [tuple(map(float, p.split())) for p in d[1:].split("L")]  # but each crosses the map
        assert any(0 <= x <= 975 and 0 <= y <= 610 for x, y in pts)


def test_favicon_is_a_teal_pin_on_navy_and_served():
    svg = (ROOT / "app" / "static" / "favicon.svg").read_text(encoding="utf-8")
    assert 'fill="#14213d"' in svg and 'fill="#2bb3a3"' in svg  # navy tile, teal pin
    resp = main.favicon()
    assert resp.media_type == "image/png" and str(resp.path).endswith("favicon-32.png")
    png = (ROOT / "app" / "static" / "favicon-32.png").read_bytes()
    assert png.startswith(b"\x89PNG") and int.from_bytes(png[16:20], "big") == 32
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'rel="icon" href="/static/favicon.svg"' in html and 'rel="apple-touch-icon"' in html
