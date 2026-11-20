"""Monthly counts: in the Breakdown for an annual question, in the answer when asked for.

weather_stat takes by_month. With false (annual or whole-period questions) the model gets the
totals only, so it can't list months in the answer text; the app adds one 'detail' figure per
month to the answer from the same deterministic result, without another model call. With true
(a monthly breakdown was asked for) the model gets the monthly counts and lists them.
"""
import json
import subprocess
import uuid

import pytest
import yaml
from anthropic.types import TextBlock, ToolUseBlock

from app import agent, metrics, store, tools
from evals import run as eval_run
from tests.fakes import FakeClient, answer_json, msg

ANNUAL = {"hub_id": "denver", "metric": "snowfall", "year": 2025, "threshold": None,
          "by_month": False}
MONTHLY = {**ANNUAL, "by_month": True}
HEADLINE = [{"label": "Share of 2025 days with daily snowfall > 0 cm (any snowfall)",
             "value": 11.8, "unit": "%", "source_tool": "weather_stat", "role": "headline"},
            {"label": "Days with daily snowfall > 0 cm (any snowfall), 2025", "value": 43,
             "unit": "of 365 days", "source_tool": "weather_stat", "role": "headline"}]
ANNUAL_TEXT = "In 2025, Denver had snowfall on **43 of 365 days (11.8%)** (daily snowfall > 0 cm)."


def setup_module():
    store.build_from_cache()


def text(t):
    return TextBlock(type="text", text=t, citations=None)


def call(args, id_="w1"):
    return msg([ToolUseBlock(type="tool_use", id=id_, name="weather_stat", input=args)], "tool_use")


def final(answer=ANNUAL_TEXT, figures=HEADLINE):
    return msg([text(answer_json(answer=answer, figures=figures))], "end_turn")


def sid():
    return uuid.uuid4().hex


def denver_months():
    return metrics.weather_stat("denver", "snowfall", 2025, None)["by_month"]


# ---------- the tool ----------

def test_by_month_is_a_required_boolean_on_the_strict_tool():
    t = next(t for t in tools.TOOL_DEFS if t["name"] == "weather_stat")
    assert t["strict"] and "by_month" in t["input_schema"]["required"]
    assert t["input_schema"]["properties"]["by_month"] == {"type": "boolean"}
    assert "true only when the user asks for a monthly breakdown" in t["description"]


def test_annual_call_returns_totals_without_the_monthly_counts():
    out, is_error = tools.execute("weather_stat", ANNUAL)
    r = json.loads(out)
    assert not is_error
    assert (r["days_meeting_condition"], r["days_with_data"], r["percent_of_days"]) == (43, 365, 11.8)
    assert r["by_month"] == tools.BY_MONTH_OMITTED
    assert not eval_run.months_named(out)  # nothing month-shaped reaches the model


def test_monthly_call_returns_the_monthly_counts():
    r = json.loads(tools.execute("weather_stat", MONTHLY)[0])
    assert r["by_month"] == denver_months() and sum(r["by_month"].values()) == 43


def test_monthly_figures_match_the_data_and_quote_the_condition():
    figs = tools.monthly_figures(ANNUAL)
    months = denver_months()
    assert [f["value"] for f in figs] == list(months.values())
    assert figs[0]["label"] == "Denver, January 2025: daily snowfall > 0 cm (any snowfall)"
    assert all(f["role"] == "detail" and f["source_tool"] == "weather_stat" for f in figs)
    assert [f["unit"] for f in figs] == ["day" if n == 1 else "days" for n in months.values()]
    assert tools.monthly_figures({**ANNUAL, "year": None}) == []           # no monthly data
    assert tools.monthly_figures({**ANNUAL, "threshold": -1}) == []        # error result


# ---------- annual question: months only in the Breakdown ----------

def test_annual_question_gets_the_months_in_the_breakdown_not_the_text():
    fake = FakeClient([call(ANNUAL), final()])
    out = agent.chat("What percentage of days in Denver last year had snowfall?", sid(), client=fake)

    sent = fake.requests[1]["messages"][-1]["content"][0]["content"]
    assert json.loads(sent)["by_month"] == tools.BY_MONTH_OMITTED  # the model saw totals only
    a = out["answer"]
    assert a["answer"] == ANNUAL_TEXT                               # text as the model wrote it
    detail = [f for f in a["figures"] if f["role"] == "detail"]
    assert [f["value"] for f in detail] == list(denver_months().values())
    assert [f for f in a["figures"] if f["role"] == "headline"] == HEADLINE
    assert len(fake.requests) == 2                                   # no extra model call


def test_stored_history_keeps_the_models_own_answer():
    s = sid()
    agent.chat("q", s, client=FakeClient([call(ANNUAL), final()]))
    stored = json.loads(store.load_messages(s)[-1]["content"][-1]["text"])
    assert stored["figures"] == HEADLINE  # app-added rows are not written into the history


def test_existing_denver_eval_passes_with_the_app_added_rows():
    case = next(c for c in yaml.safe_load((eval_run.HERE / "cases.yaml").read_text("utf-8"))
                if c["id"] == "denver_snow_last_year")
    out = agent.chat(case["turns"][0], sid(), client=FakeClient([call(ANNUAL), final()]))
    checks = eval_run.check_case(case, out)
    assert all(ok for _, ok, _ in checks if ok is not None), checks
    assert ("no month list in answer", True, "months=[]") in checks


def test_repeated_annual_call_adds_the_months_once():
    fake = FakeClient([call(ANNUAL, "a"), call(ANNUAL, "b"), final()])
    out = agent.chat("q", sid(), client=fake)
    assert len([f for f in out["answer"]["figures"] if f["role"] == "detail"]) == len(denver_months())


def test_no_rows_for_errors_full_history_or_answers_that_are_not_answers():
    for args in ({**ANNUAL, "threshold": -1}, {**ANNUAL, "year": None}):
        out = agent.chat("q", sid(), client=FakeClient([call(args), final(figures=[])]))
        assert out["answer"]["figures"] == []
    bad = msg([text('{"answer": "partial"}')], "end_turn")
    out = agent.chat("q", sid(), client=FakeClient([call(ANNUAL), bad, bad]))
    assert out["status"] == "invalid_answer" and out["answer"]["figures"] == []


def test_rows_are_added_to_an_answer_given_at_the_tool_limit():
    script = [call(ANNUAL, "w0")] + [call(ANNUAL, f"w{i}") for i in range(1, 8)] + [final()]
    out = agent.chat("q", sid(), client=FakeClient(script))
    assert out["status"] == "answered_at_tool_limit"
    assert len([f for f in out["answer"]["figures"] if f["role"] == "detail"]) == len(denver_months())


# ---------- monthly follow-up: months in the answer ----------

def test_monthly_follow_up_lists_the_months_in_the_answer():
    s = sid()
    agent.chat("What percentage of days in Denver last year had snowfall?", s,
               client=FakeClient([call(ANNUAL), final()]))

    months = denver_months()
    names = [f"{tools.MONTH_NAMES[int(k[5:7]) - 1]} {n}" for k, n in months.items()]
    monthly_text = "By month in 2025: " + ", ".join(names) + "."
    monthly_figs = [{"label": f"Snow days in {k}", "value": n, "unit": "days",
                     "source_tool": "weather_stat", "role": "detail"} for k, n in months.items()]
    before = store.load_messages(s)
    fake = FakeClient([call(MONTHLY, "m1"), final(answer=monthly_text, figures=monthly_figs)])
    out = agent.chat("Break that down by month", s, client=fake)

    seen = json.loads(fake.requests[1]["messages"][-1]["content"][0]["content"])
    assert seen["by_month"] == months                       # the model got the monthly counts
    assert out["answer"]["answer"] == monthly_text          # and listed them in the answer
    assert len(eval_run.months_named(out["answer"]["answer"])) == len(months)
    assert out["answer"]["figures"] == monthly_figs         # no app-added duplicates
    assert fake.requests[0]["messages"][:-1] == before  # turn 1 replayed unchanged


# ---------- prompt, and the eval is untouched ----------

def test_prompt_makes_the_month_rule_conditional():
    p = agent._system_prompt()
    assert "call weather_stat with by_month=false and don't list months" in p
    assert "When the user asks for a monthly breakdown" in p and "by_month=true" in p


def test_eval_cases_and_checks_are_unchanged():
    root = eval_run.HERE.parent

    def at(rev, path):
        r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=root, capture_output=True,
                           text=True)
        if r.returncode:  # e.g. a shallow clone without that commit
            pytest.skip(f"git revision {rev} not available")
        return r.stdout
    assert (eval_run.HERE / "cases.yaml").read_text("utf-8") == at("a918df7", "evals/cases.yaml")
    import re
    fn = lambda src: re.search(r"^def check_case.*?(?=^def |\Z)", src, re.S | re.M).group(0)  # noqa: E731
    assert fn((eval_run.HERE / "run.py").read_text("utf-8")) == fn(at("a918df7", "evals/run.py"))
