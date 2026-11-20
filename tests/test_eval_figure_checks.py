"""The eval's required-figure check: right number AND right unit, in answer.figures."""
import pytest

from app import store
from evals import run as eval_run


def setup_module():
    store.build_from_cache()


PCT = {"id": "denver_snow_last_year", "turns": ["q"], "in_scope": True,
       "figure": {"hub_id": "denver", "metric": "snowfall", "year": 2025, "threshold": None,
                  "field": "percent_of_days", "tol": 0.5, "unit": "%", "role": "headline"}}
DAYS = {"id": "denver_snow_threshold", "turns": ["q"], "in_scope": True,
        "figure": {"hub_id": "denver", "metric": "snowfall", "year": 2025, "threshold": 1.0,
                   "field": "days_meeting_condition", "tol": 0, "unit": "days", "role": "headline"}}


def fig(value, unit, role="headline"):
    return {"label": "x", "value": value, "unit": unit, "source_tool": "weather_stat", "role": role}


def result(figures, text="Denver had snowfall on 43 of 365 days (11.8%)."):
    return {"tool_calls": [], "answer": {
        "in_scope": True, "answer": text, "hubs": [], "figures": figures, "reasoning_steps": [],
        "data_sources": [], "assumptions": ["x"], "confidence": "medium",
        "follow_up_suggestions": []}}


def figure_check(case, res):
    return next(c for c in eval_run.check_case(case, res) if c[0].startswith("figure ")
                and not c[0].startswith("figure is"))


def passed(case, res):
    return all(ok for _, ok, _ in eval_run.check_case(case, res) if ok is not None)


@pytest.mark.parametrize("unit", ["%", "percent", "per cent", "% of days", "pct"])
def test_percent_figure_with_a_percent_unit_passes(unit):
    assert figure_check(PCT, result([fig(11.8, unit)]))[1] is True
    assert passed(PCT, result([fig(11.8, unit)]))


@pytest.mark.parametrize("unit", ["days", "day", "", "cm", "of 365 days"])
def test_right_number_with_the_wrong_unit_fails(unit):
    name, ok, detail = figure_check(PCT, result([fig(11.8, unit)]))
    assert ok is False and name == "figure 11.8 %"
    assert "want 11.8 %" in detail and repr(unit) in detail
    assert not passed(PCT, result([fig(11.8, unit)]))


def test_days_figure_requires_a_days_unit():
    assert figure_check(DAYS, result([fig(17, "days")]))[1] is True
    assert figure_check(DAYS, result([fig(17, "of 365 days")]))[1] is True
    assert figure_check(DAYS, result([fig(17, "%")]))[1] is False
    assert figure_check(DAYS, result([fig(17, "% of days")]))[1] is False


@pytest.mark.parametrize("figures", [[], [fig(12.9, "%")], [fig(11.8, "days"), fig(43, "days")]],
                         ids=["no figures", "wrong number", "number only with wrong unit"])
def test_missing_required_figure_fails_the_case(figures):
    assert not passed(PCT, result(figures))


def test_figure_only_in_the_answer_text_is_not_enough():
    res = result([], text="Denver had snowfall on 11.8% of days in 2025.")
    assert figure_check(PCT, res)[1] is False and not passed(PCT, res)


def test_headline_role_is_judged_on_the_figure_with_the_right_unit():
    res = result([fig(11.8, "days", role="headline"), fig(11.8, "%", role="detail")])
    checks = {name: ok for name, ok, _ in eval_run.check_case(PCT, res)}
    assert checks["figure 11.8 %"] is True and checks["figure is headline"] is False


def test_required_figure_that_cannot_be_computed_fails_instead_of_skipping():
    case = {**PCT, "figure": {**PCT["figure"], "hub_id": "nowhere"}}
    checks = eval_run.check_case(case, result([fig(11.8, "%")]))
    avail = next(c for c in checks if c[0] == "figure available")
    assert avail[1] is False and "could not be computed" in avail[2]
    assert not passed(case, result([fig(11.8, "%")]))


def test_unit_defaults_from_the_field_and_unknown_units_are_rejected():
    assert eval_run.expected_unit({"field": "percent_of_days"}) == "%"
    assert eval_run.expected_unit({"field": "days_meeting_condition"}) == "days"
    with pytest.raises(ValueError):
        eval_run.expected_unit({"field": "percent_of_days", "unit": "km"})


def test_every_figure_case_in_the_eval_set_states_its_unit():
    import yaml
    cases = yaml.safe_load((eval_run.HERE / "cases.yaml").read_text(encoding="utf-8"))
    specs = [c["figure"] for c in cases if "figure" in c]
    assert specs and all(s.get("unit") in ("%", "days") for s in specs)
