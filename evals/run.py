"""Run the evaluation set against the live agent (needs ANTHROPIC_API_KEY; costs API tokens).

  python -m evals.run                 # all cases
  python -m evals.run --case dallas_why
  python -m evals.run --dry-run       # no LLM calls: show the expected values per case

Results are printed as a table and written to evals/results/latest.json.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import yaml

from app import agent, metrics, scoring, store

HERE = Path(__file__).parent


def expected_ranking(spec: dict) -> list[str]:
    hz = None if spec["hazard"] == "overall" else spec["hazard"]
    return [r["hub_id"] for r in scoring.rank(hz, spec.get("region"))][: spec["top_n"]]


def expected_figure(spec: dict):
    stat = metrics.weather_stat(spec["hub_id"], spec["metric"], spec.get("year"),
                                spec.get("threshold"))
    return stat.get(spec["field"]), stat


def model_score(hub_id: str, hazard: str) -> float:
    b = scoring.score_hub(hub_id)
    if hazard == "overall":
        return b.overall_score
    return next(h.score for h in b.hazards if h.hazard == hazard)


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
    if "figure" in case:
        want, stat = expected_figure(case["figure"])
        if want is None:
            checks.append(("figure", None, f"SKIP: no data ({stat.get('error')})"))
        else:
            vals = [f["value"] for f in a["figures"]]
            ok = any(abs(v - want) <= case["figure"]["tol"] for v in vals)
            checks.append(("figure value", ok, f"want={want} got={vals}"))

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
    for turn in case["turns"]:
        result = agent.chat(turn, sid)
    checks = check_case(case, result)
    decided = [c for c in checks if c[1] is not None]
    return {"id": case["id"], "passed": all(c[1] for c in decided), "skipped": len(decided) < len(checks),
            "seconds": round(time.time() - t0, 1), "checks": checks,
            "answer": result["answer"], "tool_calls": result["tool_calls"], "usage": result["usage"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    store.build_from_cache()
    cases = yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]

    if args.dry_run:
        for c in cases:
            exp = {}
            if "ranking" in c:
                exp["ranking"] = expected_ranking(c["ranking"])
            if "figure" in c:
                exp["figure"] = expected_figure(c["figure"])[0]
            print(f"{c['id']:24} tools={c.get('tools', [])} in_scope={c.get('in_scope')} {exp}")
        return

    results = []
    for c in cases:
        r = run_case(c)
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{status}  {c['id']:24} {r['seconds']:>5}s")
        for name, ok, detail in r["checks"]:
            mark = "skip" if ok is None else ("ok" if ok else "FAIL")
            print(f"        [{mark:4}] {name} {detail}")
    n = sum(r["passed"] for r in results)
    tokens = sum(r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in results)
    print(f"\n{n}/{len(results)} cases passed; total tokens {tokens:,}")
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "latest.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
