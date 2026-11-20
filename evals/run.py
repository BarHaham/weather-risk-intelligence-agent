"""Run the evaluation set against the live agent (needs ANTHROPIC_API_KEY, from .env or the
environment; costs API tokens).

  python -m evals.run                 # all cases
  python -m evals.run --case dallas_why
  python -m evals.run --dry-run       # no LLM calls: show the expected values per case

Results are printed as a table (with the model that actually answered each case) and written
to evals/results/latest.json.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from pathlib import Path

import anthropic
import yaml

from app import agent, config, metrics, scoring, store
from app.config import settings

HERE = Path(__file__).parent


def expected_ranking(spec: dict) -> list[str]:
    hz = None if spec["hazard"] == "overall" else spec["hazard"]
    return [r["hub_id"] for r in scoring.rank(hz, spec.get("region"))][: spec["top_n"]]


def expected_figure(spec: dict):
    stat = metrics.weather_stat(spec["hub_id"], spec["metric"], spec.get("year"),
                                spec.get("threshold"))
    return stat.get(spec["field"]), stat


# A figure must carry the right unit as well as the right number: "11.8 days" is not "11.8%".
FIELD_UNITS = {"percent_of_days": "%", "days_meeting_condition": "days", "days_with_data": "days"}
_PERCENT_RE = re.compile(r"%|\bper ?cent(age)?\b|\bpct\b", re.IGNORECASE)
_DAYS_RE = re.compile(r"\bdays?\b", re.IGNORECASE)


def expected_unit(spec: dict) -> str:
    unit = spec.get("unit") or FIELD_UNITS.get(spec["field"])
    if unit not in ("%", "days"):
        raise ValueError(f"figure spec needs unit '%' or 'days' (field {spec['field']!r})")
    return unit


def unit_matches(unit: str, want: str) -> bool:
    """'%', 'percent', '% of days' count as %; 'days', 'of 365 days' count as days."""
    unit = unit or ""
    if want == "%":
        return bool(_PERCENT_RE.search(unit))
    return bool(_DAYS_RE.search(unit)) and not _PERCENT_RE.search(unit)


def model_score(hub_id: str, hazard: str) -> float:
    b = scoring.score_hub(hub_id)
    if hazard == "overall":
        return b.overall_score
    return next(h.score for h in b.hazards if h.hazard == hazard)


def check_tool_input(spec: dict, tool_calls: list[dict]) -> tuple[str, bool, str]:
    """At least one call of spec['tool'], and every such call used exactly the given inputs."""
    want = {k: v for k, v in spec.items() if k != "tool"}
    used = [t["input"] for t in tool_calls if t["tool"] == spec["tool"]]

    def matches(inp: dict) -> bool:
        for k, v in want.items():
            got = inp.get(k)
            if isinstance(v, (int, float)) and isinstance(got, (int, float)):
                if float(got) != float(v):  # 1 and 1.0 are the same value; 0.99 is not
                    return False
            elif got != v:
                return False
        return True

    ok = bool(used) and all(matches(u) for u in used)
    return (f"{spec['tool']} input exactly {want}", ok, f"used={used}")


MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
MAX_MONTHS_IN_ANSWER = 3  # more than this means a month-by-month list in the answer text
_MONTH_RE = re.compile(r"\b(" + "|".join(
    [m for m in MONTHS if m != "may"] + [m[:3] for m in MONTHS if m != "may"] + ["sept"])
    + r")\b\.?", re.IGNORECASE)
_MAY_RE = re.compile(r"\bMay\b")  # case-sensitive, so the verb "may" is not a month
_YYYY_MM_RE = re.compile(r"\b20\d\d-(0[1-9]|1[0-2])\b")


def months_named(text: str) -> list[str]:
    """Distinct months named in text (full or abbreviated names, or 2025-01 style)."""
    found = set()
    for m in _MONTH_RE.findall(text):
        key = m.lower().rstrip(".")[:3]
        found.add(MONTHS[[x[:3] for x in MONTHS].index(key)])
    if _MAY_RE.search(text):
        found.add("may")
    for mm in _YYYY_MM_RE.findall(text):
        found.add(MONTHS[int(mm) - 1])
    return sorted(found, key=MONTHS.index)


def check_case(case: dict, result: dict) -> list[tuple[str, bool, str]]:
    a = result["answer"]
    calls = [t["tool"] for t in result["tool_calls"]]
    checks: list[tuple[str, bool, str]] = []

    for tool in case.get("tools", []):
        checks.append((f"called {tool}", tool in calls, f"calls={calls}"))
    if "in_scope" in case:
        checks.append(("in_scope", a["in_scope"] == case["in_scope"], f"got {a['in_scope']}"))
    for hid in case.get("hubs_mentioned", []):
        got = [h["hub_id"] for h in a["hubs"]]
        checks.append((f"mentions {hid}", hid in got, f"hubs={got}"))
    if "ranking" in case:
        spec = case["ranking"]
        want = expected_ranking(spec)
        got = [h["hub_id"] for h in a["hubs"] if h["hazard"] == spec["hazard"]]
        checks.append(("ranking order", got[: len(want)] == want, f"want={want} got={got}"))
    if "tool_input" in case:
        checks.append(check_tool_input(case["tool_input"], result["tool_calls"]))
    if "figure" in case:
        spec = case["figure"]
        want, stat = expected_figure(spec)
        unit = expected_unit(spec)
        if want is None:  # a required figure that cannot be computed fails the case
            checks.append(("figure available", False,
                           f"required figure could not be computed: {stat.get('error')}"))
        else:
            # Required: some figure with the right number AND the right unit (in answer.figures,
            # not just in the text). A missing figure or a wrong unit fails the case.
            match = [f for f in a["figures"] if abs(f["value"] - want) <= spec["tol"]
                     and unit_matches(f.get("unit", ""), unit)]
            got = ", ".join(f"{f['value']:g} {f.get('unit', '')!r}" for f in a["figures"])
            checks.append((f"figure {want:g} {unit}", bool(match),
                           f"want {want:g} {unit} (tol {spec['tol']}); got {got or 'no figures'}"))
            if spec.get("role"):
                roles = [f.get("role") for f in match]
                checks.append((f"figure is {spec['role']}", spec["role"] in roles, f"roles={roles}"))
    if case.get("no_month_list"):
        months = months_named(a["answer"])
        checks.append(("no month list in answer", len(months) <= MAX_MONTHS_IN_ANSWER,
                       f"months={months}"))

    # Always: scores in the answer must match the deterministic model (no invented numbers).
    bad = [(h["hub_id"], h["hazard"], h["score"], model_score(h["hub_id"], h["hazard"]))
           for h in a["hubs"] if abs(h["score"] - model_score(h["hub_id"], h["hazard"])) > 0.1]
    checks.append(("scores grounded", not bad, f"mismatches={bad}" if bad else ""))
    if a["in_scope"]:
        checks.append(("assumptions stated", bool(a["assumptions"]), ""))
    return checks


def run_case(case: dict) -> dict:
    sid = f"eval-{case['id']}-{uuid.uuid4().hex[:8]}"
    t0 = time.time()
    result = None
    turn_models = []
    for turn in case["turns"]:
        try:
            result = agent.chat(turn, sid)
        except Exception as e:
            e.turn_models = turn_models  # models of the turns that did complete
            raise
        turn_models.append(result["models"])
    checks = check_case(case, result)
    decided = [c for c in checks if c[1] is not None]
    return {"id": case["id"], "passed": all(c[1] for c in decided),
            "skipped": len(decided) < len(checks), "seconds": round(time.time() - t0, 1),
            "checks": checks, "answered_by": result["models"]["answered_by"],
            "models": turn_models, "answer": result["answer"],
            "tool_calls": result["tool_calls"], "usage": result["usage"]}


def failed_case(case: dict, reason: str, models: list | None = None) -> dict:
    return {"id": case["id"], "passed": False, "skipped": False, "seconds": 0,
            "checks": [("api call", False, reason)], "answered_by": None,
            "models": models or [], "answer": None, "tool_calls": [],
            "usage": {"input_tokens": 0, "output_tokens": 0}}


def _ascii(text: str) -> str:
    # A Windows console redirected to a file is cp1252; keep printed lines plain ASCII.
    return str(text).encode("ascii", "replace").decode("ascii")


def format_case_line(r: dict) -> str:
    status = "PASS" if r["passed"] else "FAIL"
    line = f"{status}  {r['id']:24} {r['seconds']:>5}s  answered_by={r['answered_by'] or 'none'}"
    turns = r.get("models") or []
    for i, m in enumerate(turns, 1):
        fb = m.get("fallback")
        if fb:
            where = f" (turn {i})" if len(turns) > 1 else ""
            line += f" [fallback from {fb['from']}{where}: {fb['reason']}]"
    return _ascii(line)


def format_check_lines(r: dict) -> list[str]:
    lines = []
    for name, ok, detail in r["checks"]:
        mark = "skip" if ok is None else ("ok" if ok else "FAIL")
        lines.append(_ascii(f"        [{mark:4}] {name} {detail}"))
    return lines


def summary_line(results: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        key = r["answered_by"] or "no answer"
        counts[key] = counts.get(key, 0) + 1
    text = "Answered by: " + ", ".join(f"{m} x{n}" for m, n in counts.items())
    fallback_ids = [r["id"] for r in results if any(m.get("fallback") for m in r.get("models") or [])]
    if fallback_ids:
        text += f" (fallback: {', '.join(fallback_ids)})"
    return _ascii(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    config.load_env_file()
    store.build_from_cache()
    cases = yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    llm = settings()["llm"]
    print(_ascii(f"Models: {llm['model']} (fallback once: {llm.get('fallback_model') or 'none'}); "
                 f"API key: {config.api_key_source}"))
    simulated = agent.simulated_outage_models()
    if simulated:
        print(_ascii(f"NOTE: {agent.SIMULATE_ENV} is set - simulating an outage for "
                     f"{', '.join(sorted(simulated))}"))

    if args.dry_run:
        for c in cases:
            exp = {}
            if "ranking" in c:
                exp["ranking"] = expected_ranking(c["ranking"])
            if "figure" in c:
                exp["figure"] = f"{expected_figure(c['figure'])[0]} {expected_unit(c['figure'])}"
            print(f"{c['id']:24} tools={c.get('tools', [])} in_scope={c.get('in_scope')} {exp}")
        return

    if not cases:
        raise SystemExit(f"no case with id '{args.case}' in evals/cases.yaml")

    results = []
    for c in cases:
        try:
            r = run_case(c)
        except TypeError as e:  # SDK raises this when no credentials can be resolved
            if "authentication" not in str(e).lower():
                raise
            raise SystemExit("No Anthropic credentials found: put ANTHROPIC_API_KEY in .env or "
                             "set it in this window (only --dry-run works without it).")
        except anthropic.AuthenticationError as e:
            raise SystemExit(f"Anthropic rejected the API key: {agent.describe_error(e)}")
        except agent.FallbackFailedError as e:  # both models failed: keep both real errors
            r = failed_case(c, e.describe(), getattr(e, "turn_models", []) + [{
                                               "requested": e.primary_model, "answered_by": None,
                                               "calls": [], "fallback": {
                                                   "from": e.primary_model, "to": e.fallback_model,
                                                   "reason": agent.describe_error(e.primary_error),
                                                   "fallback_error": agent.describe_error(e.fallback_error)}}])
        except anthropic.APIError as e:  # keep going; record the case as failed
            r = failed_case(c, agent.describe_error(e), getattr(e, "turn_models", []))
        results.append(r)
        print(format_case_line(r))
        for line in format_check_lines(r):
            print(line)
    n = sum(r["passed"] for r in results)
    tokens = sum(r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in results)
    print(f"\n{n}/{len(results)} cases passed; total tokens {tokens:,}")
    print(summary_line(results))
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "latest.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
