"""Tool-round limit and repeated calls.

- An identical repeated call (same tool, same input) in one turn is not run again; its result
  points to the earlier call.
- After llm.max_tool_rounds (8) rounds, one more request is sent with tools off
  (tool_choice none) and a system notice, so the model answers from the data it has.
- Reaching the limit is reported as a status, never as an out-of-scope question.
"""
import json
import uuid

import anthropic
import httpx2
from anthropic.types import TextBlock, ToolUseBlock

from app import agent, store, tools
from app.config import settings
from evals import run as eval_run
from tests.fakes import HAIKU, SONNET, FakeClient, answer_json, msg, overloaded

MAX = settings()["llm"]["max_tool_rounds"]
BREAKDOWN = {"hub_id": "dallas"}


def setup_module():
    store.build_from_cache()


def text(t):
    return TextBlock(type="text", text=t, citations=None)


def tool_use(name, args, id_):
    return ToolUseBlock(type="tool_use", id=id_, name=name, input=args)


def breakdown_round(i):
    return msg([tool_use("get_score_breakdown", BREAKDOWN, f"t{i}")], "tool_use")


def final(**over):
    return msg([text(answer_json(**over))], "end_turn")


def sid():
    return uuid.uuid4().hex


def counting_execute(monkeypatch):
    calls = []
    real = tools.execute

    def execute(name, args):
        calls.append((name, args))
        return real(name, args)

    monkeypatch.setattr(tools, "execute", execute)
    return calls


def test_limit_is_still_eight_rounds():
    assert MAX == 8


# ---------- repeated identical calls ----------

def test_identical_repeat_is_not_run_again_and_points_to_the_first_call(monkeypatch):
    calls = counting_execute(monkeypatch)
    fake = FakeClient([breakdown_round(1), breakdown_round(2), final()])
    out = agent.chat("Why is Dallas high?", sid(), client=fake)

    assert calls == [("get_score_breakdown", BREAKDOWN)]  # run once
    assert out["tool_calls"] == [
        {"tool": "get_score_breakdown", "input": BREAKDOWN, "is_error": False},
        {"tool": "get_score_breakdown", "input": BREAKDOWN, "is_error": False,
         "repeated": True, "same_as_call": 1}]
    repeat = fake.requests[2]["messages"][-1]["content"][0]
    assert repeat["tool_use_id"] == "t2" and repeat["is_error"] is False
    body = json.loads(repeat["content"])
    assert body["repeated_call"] is True and body["same_as_call"] == 1
    assert "unchanged" in body["note"]
    assert out["status"] == "answered" and out["tool_rounds"] == 2


def test_same_tool_with_a_different_input_runs_normally(monkeypatch):
    calls = counting_execute(monkeypatch)
    fake = FakeClient([
        msg([tool_use("get_score_breakdown", {"hub_id": "dallas"}, "a"),
             tool_use("get_score_breakdown", {"hub_id": "houston"}, "b")], "tool_use"),
        final()])
    out = agent.chat("Compare", sid(), client=fake)
    assert [c[1]["hub_id"] for c in calls] == ["dallas", "houston"]
    assert not any(t.get("repeated") for t in out["tool_calls"])


def test_repeat_detection_ignores_key_order_and_is_per_turn(monkeypatch):
    calls = counting_execute(monkeypatch)
    s = sid()
    a = {"hub_id": "denver", "metric": "snowfall", "year": 2025, "threshold": None}
    b = dict(reversed(list(a.items())))  # same input, other key order
    agent.chat("q1", s, client=FakeClient([
        msg([tool_use("weather_stat", a, "x1"), tool_use("weather_stat", b, "x2")], "tool_use"),
        final()]))
    assert len(calls) == 1
    # a new turn may call it again (e.g. "show me that again"): it runs
    out = agent.chat("q2", s, client=FakeClient([
        msg([tool_use("weather_stat", a, "y1")], "tool_use"), final()]))
    assert len(calls) == 2 and not out["tool_calls"][0].get("repeated")


def test_get_methodology_with_no_input_repeats_are_caught(monkeypatch):
    calls = counting_execute(monkeypatch)
    fake = FakeClient([msg([tool_use("get_methodology", {}, f"m{i}")], "tool_use")
                       for i in range(3)] + [final()])
    out = agent.chat("Denver snow?", sid(), client=fake)
    assert calls == [("get_methodology", {})]
    assert [t.get("repeated", False) for t in out["tool_calls"]] == [False, True, True]


# ---------- the tools-off final reply after the limit ----------

def test_after_eight_rounds_the_final_request_turns_tools_off_and_answers():
    s = sid()
    fake = FakeClient([breakdown_round(i) for i in range(MAX)] + [final(answer="Dallas is Low.")])
    out = agent.chat("Why is Dallas high?", s, client=fake)

    assert len(fake.requests) == MAX + 1
    assert all(r["tool_choice"] is None for r in fake.requests[:MAX])
    last = fake.requests[MAX]
    assert last["tool_choice"] == {"type": "none"}
    assert last["tools"] == fake.requests[0]["tools"]    # tools and system unchanged
    assert last["system"] == fake.requests[0]["system"]
    notice = last["messages"][-1]
    assert notice["role"] == "system" and f"all {MAX} tool rounds" in notice["content"]
    assert last["messages"][-2]["role"] == "user"        # follows the last tool results

    assert out["status"] == "answered_at_tool_limit"
    assert out["tool_rounds"] == MAX and out["max_tool_rounds"] == MAX
    assert out["answer"]["answer"] == "Dallas is Low." and out["answer"]["in_scope"] is True
    assert sum(t.get("repeated", False) for t in out["tool_calls"]) == MAX - 1

    stored = store.load_messages(s)
    assert [m["role"] for m in stored[-3:]] == ["user", "system", "assistant"]
    # a follow-up sends a valid, append-only history: the notice is followed by the answer
    follow = FakeClient([final()])
    out2 = agent.chat("and Houston?", s, client=follow)
    sent = follow.requests[0]["messages"]
    assert sent[:-1] == stored and sent[-1] == {"role": "user", "content": "and Houston?"}
    assert follow.requests[0]["tool_choice"] is None and out2["status"] == "answered"


def test_answer_before_the_limit_has_no_notice_and_no_tool_choice():
    fake = FakeClient([breakdown_round(i) for i in range(MAX - 1)] + [final()])
    out = agent.chat("q", sid(), client=fake)
    assert out["status"] == "answered" and out["tool_rounds"] == MAX - 1
    assert all(r["tool_choice"] is None for r in fake.requests)
    assert all(m["role"] != "system" for r in fake.requests for m in r["messages"])


def test_repair_after_the_limit_keeps_tools_off():
    fake = FakeClient([breakdown_round(i) for i in range(MAX)]
                      + [msg([text('{"answer": "partial"}')], "end_turn"), final()])
    out = agent.chat("q", sid(), client=fake)
    assert out["status"] == "answered_at_tool_limit"
    assert [r["tool_choice"] for r in fake.requests[MAX:]] == [{"type": "none"}] * 2
    assert sum(m["role"] == "system" for m in fake.requests[-1]["messages"]) == 1  # sent once


def test_no_valid_answer_after_the_limit_reports_the_limit_not_out_of_scope():
    s = sid()
    bad = msg([text('{"answer": "partial"}')], "end_turn")
    fake = FakeClient([breakdown_round(i) for i in range(MAX)] + [bad, bad])
    out = agent.chat("q", s, client=fake)
    assert out["status"] == "tool_limit"
    assert out["answer"]["in_scope"] is None   # not judged: never "out of scope"
    assert f"Stopped after {MAX} tool rounds without a final answer" in out["answer"]["answer"]
    assert store.load_messages(s)[-1]["role"] == "assistant"


def test_cut_off_after_the_limit_drops_the_notice_so_the_next_turn_is_valid():
    s = sid()
    fake = FakeClient([breakdown_round(i) for i in range(MAX)]
                      + [msg([text('{"in_scope": tr')], "max_tokens")])
    out = agent.chat("q", s, client=fake)
    assert out["status"] == "cut_off" and out["answer"]["in_scope"] is None
    stored = store.load_messages(s)
    assert stored[-1]["role"] == "user" and all(m["role"] != "system" for m in stored)
    follow = FakeClient([final()])
    agent.chat("again", s, client=follow)
    roles = [m["role"] for m in follow.requests[0]["messages"]]
    assert "system" not in roles and roles[-1] == "user"


def test_tool_use_despite_tools_off_is_reported_as_the_limit_and_not_stored():
    s = sid()
    fake = FakeClient([breakdown_round(i) for i in range(MAX + 1)])
    out = agent.chat("q", s, client=fake)
    assert out["status"] == "tool_limit" and out["answer"]["in_scope"] is None
    assert len(fake.requests) == MAX + 1 and out["tool_rounds"] == MAX
    stored = store.load_messages(s)
    assert stored[-1]["role"] == "user" and stored[-1]["content"][0]["type"] == "tool_result"
    assert all(m["role"] != "system" for m in stored)


def test_fallback_on_the_final_request_keeps_tools_off():
    # FakeClient does not retry, so the overloaded error goes straight to the Haiku attempt
    fake = FakeClient([breakdown_round(i) for i in range(MAX)] + [overloaded(), final()])
    out = agent.chat("q", sid(), client=fake)
    assert fake.models[-2:] == [SONNET, HAIKU]
    assert [r["tool_choice"] for r in fake.requests[-2:]] == [{"type": "none"}] * 2
    assert fake.requests[-1]["messages"] == fake.requests[-2]["messages"]
    assert out["status"] == "answered_at_tool_limit" and out["models"]["answered_by"] == HAIKU


# ---------- the real SDK path (MockTransport): replay and tool_choice on the wire ----------

def sse(model, blocks, stop):
    """A streamed Messages response. blocks: ("thinking", sig) | ("tool", id, name, json)
    | ("text", str)."""
    events = [{"type": "message_start", "message": {
        "id": "msg_t", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 9, "output_tokens": 1}}}]
    for i, b in enumerate(blocks):
        if b[0] == "thinking":
            events += [{"type": "content_block_start", "index": i,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                       {"type": "content_block_delta", "index": i,
                        "delta": {"type": "signature_delta", "signature": b[1]}}]
        elif b[0] == "tool":
            events += [{"type": "content_block_start", "index": i, "content_block": {
                "type": "tool_use", "id": b[1], "name": b[2], "input": {}}},
                {"type": "content_block_delta", "index": i,
                 "delta": {"type": "input_json_delta", "partial_json": b[3]}}]
        else:
            events += [{"type": "content_block_start", "index": i,
                        "content_block": {"type": "text", "text": ""}},
                       {"type": "content_block_delta", "index": i,
                        "delta": {"type": "text_delta", "text": b[1]}}]
        events.append({"type": "content_block_stop", "index": i})
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 5}}, {"type": "message_stop"}]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                           content=body.encode())


def sdk_client(handler):
    return anthropic.Anthropic(api_key="test-key", base_url="http://stub.invalid",
                               http_client=anthropic.DefaultHttpxClient(
                                   transport=httpx2.MockTransport(handler)))


def test_sdk_replays_a_no_input_tool_call_and_its_result_unchanged():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return sse(SONNET, [("thinking", "sig1"), ("tool", "toolu_m", "get_methodology", "")],
                       "tool_use")
        return sse(SONNET, [("text", answer_json())], "end_turn")

    out = agent.chat("How is the score calculated?", sid(), client=sdk_client(handler))
    assert out["status"] == "answered"
    assistant, results = bodies[1]["messages"][1], bodies[1]["messages"][2]
    assert assistant == {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "sig1"},
        {"type": "tool_use", "id": "toolu_m", "name": "get_methodology", "input": {}}]}
    assert results["role"] == "user" and results["content"][0]["tool_use_id"] == "toolu_m"
    assert "weights" in json.loads(results["content"][0]["content"])
    assert all("tool_choice" not in b for b in bodies)


def test_sdk_final_request_after_the_limit_sends_tool_choice_none():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) <= MAX:
            return sse(SONNET, [("tool", f"toolu_{len(bodies)}", "get_score_breakdown",
                                 json.dumps(BREAKDOWN))], "tool_use")
        return sse(SONNET, [("text", answer_json())], "end_turn")

    out = agent.chat("Why is Dallas high?", sid(), client=sdk_client(handler))
    assert out["status"] == "answered_at_tool_limit"
    assert len(bodies) == MAX + 1
    assert bodies[-1]["tool_choice"] == {"type": "none"}
    assert bodies[-1]["messages"][-1]["role"] == "system"
    assert bodies[-1]["tools"] == bodies[0]["tools"] and bodies[-1]["system"] == bodies[0]["system"]


# ---------- prompt and tool description ----------

def test_prompt_says_results_are_deterministic_and_handles_a_false_premise():
    p = agent._system_prompt()
    assert "Don't repeat a call" in p
    assert "premise doesn't match the model" in p
    assert "minimi" not in p.lower()  # no tool-discouraging wording


def test_methodology_is_no_longer_suggested_for_every_answer():
    desc = next(t["description"] for t in tools.TOOL_DEFS if t["name"] == "get_methodology")
    assert "state assumptions" not in desc
    assert "how the score is calculated" in desc


# ---------- eval output: status shown, checks unchanged ----------

def run_eval_case(monkeypatch, tmp_path, script, case):
    monkeypatch.setattr(agent, "_client", lambda: FakeClient(script))
    monkeypatch.setattr(eval_run, "HERE", tmp_path)
    return eval_run.run_case(case)


DALLAS = {"id": "dallas_why", "turns": ["Why is the Dallas hub's weather disruption risk high?"],
          "tools": ["get_score_breakdown"], "in_scope": True, "hubs_mentioned": ["dallas"]}


def test_eval_line_shows_an_answer_given_at_the_tool_limit(monkeypatch, tmp_path):
    answer = final(answer="Dallas is Low (34.9).",
                   hubs=[{"hub_id": "dallas", "hazard": "overall", "score": 34.9}])
    r = run_eval_case(monkeypatch, tmp_path, [breakdown_round(i) for i in range(MAX)] + [answer],
                      DALLAS)
    assert r["passed"], r["checks"]   # same checks: the answer itself is judged
    assert r["status"] == "answered_at_tool_limit" and r["tool_rounds"] == MAX
    line = eval_run.format_case_line(r)
    assert f"status=answered_at_tool_limit ({MAX} of {MAX} tool rounds, {MAX - 1} repeated calls)" in line
    assert "reached the tool-round limit: dallas_why" in eval_run.summary_line([r])


def test_eval_no_answer_at_the_limit_fails_without_claiming_out_of_scope(monkeypatch, tmp_path):
    bad = msg([text('{"answer": "partial"}')], "end_turn")
    r = run_eval_case(monkeypatch, tmp_path, [breakdown_round(i) for i in range(MAX)] + [bad, bad],
                      DALLAS)
    assert not r["passed"]
    checks = {name: (ok, detail) for name, ok, detail in r["checks"]}
    assert checks["in_scope"] == (False, "got None")
    assert "status=tool_limit" in eval_run.format_case_line(r)


def test_eval_line_is_unchanged_for_a_normal_answer(monkeypatch, tmp_path):
    r = run_eval_case(monkeypatch, tmp_path, [breakdown_round(0), final(
        hubs=[{"hub_id": "dallas", "hazard": "overall", "score": 34.9}])], DALLAS)
    line = eval_run.format_case_line(r)
    assert "status=" not in line and "repeated" not in line



# ---------- review follow-ups ----------

def test_invalid_answer_twice_before_the_limit_is_not_out_of_scope():
    bad = msg([text('{"answer": "partial"}')], "end_turn")
    fake = FakeClient([bad, bad])
    out = agent.chat("q", sid(), client=fake)
    assert out["status"] == agent.INVALID_ANSWER
    assert out["answer"]["in_scope"] is None
    assert all(r["tool_choice"] is None for r in fake.requests)


def test_repeat_of_a_failed_call_keeps_the_error_flag(monkeypatch):
    calls = counting_execute(monkeypatch)
    bad = {"hub_id": "denver", "metric": "snowfall", "year": 2025, "threshold": -1}
    fake = FakeClient([msg([tool_use("weather_stat", bad, "e1")], "tool_use"),
                       msg([tool_use("weather_stat", bad, "e2")], "tool_use"), final()])
    out = agent.chat("q", sid(), client=fake)
    assert len(calls) == 1
    assert [t["is_error"] for t in out["tool_calls"]] == [True, True]
    assert fake.requests[2]["messages"][-1]["content"][0]["is_error"] is True


def test_cut_off_after_the_limit_names_the_limit_and_counts_as_reaching_it(monkeypatch, tmp_path):
    script = [breakdown_round(i) for i in range(MAX)] + [msg([text('{"in_')], "max_tokens")]
    out = agent.chat("q", sid(), client=FakeClient(script))
    assert out["status"] == "cut_off"
    assert out["answer"]["answer"].startswith(f"Reached the {MAX}-round tool limit")
    r = run_eval_case(monkeypatch, tmp_path, script, DALLAS)
    assert "status=cut_off" in eval_run.format_case_line(r)
    assert "reached the tool-round limit: dallas_why" in eval_run.summary_line([r])


def test_cut_off_before_the_limit_is_not_reported_as_the_limit(monkeypatch, tmp_path):
    r = run_eval_case(monkeypatch, tmp_path, [msg([text('{"in_')], "max_tokens")], DALLAS)
    assert "status=cut_off (0 of 8 tool rounds)" in eval_run.format_case_line(r)
    assert "tool-round limit" not in eval_run.summary_line([r])


TWO_TURNS = {"id": "follow_up_detroit", "turns": ["Midwest winter?", "What drives Dallas?"],
             "tools": ["get_score_breakdown"], "in_scope": True, "hubs_mentioned": ["dallas"]}
HUB = [{"hub_id": "dallas", "hazard": "overall", "score": 34.9}]


def test_multi_turn_eval_line_shows_each_turn_and_keeps_every_turns_calls(monkeypatch, tmp_path):
    fake = FakeClient([breakdown_round(i) for i in range(MAX)] + [final(hubs=HUB)]
                      + [msg([tool_use("get_score_breakdown", BREAKDOWN, "a"),
                              tool_use("get_score_breakdown", BREAKDOWN, "b")], "tool_use"),
                         final(hubs=HUB)])
    monkeypatch.setattr(agent, "_client", lambda: fake)  # one script across both turns
    monkeypatch.setattr(eval_run, "HERE", tmp_path)
    r = eval_run.run_case(TWO_TURNS)
    assert r["passed"], r["checks"]
    assert [t["status"] for t in r["turns"]] == ["answered_at_tool_limit", "answered"]
    assert [len(t["tool_calls"]) for t in r["turns"]] == [MAX, 2]  # turn 1's calls are kept
    line = eval_run.format_case_line(r)
    assert f"[turn 1: status=answered_at_tool_limit ({MAX} of {MAX} tool rounds, {MAX - 1} repeated calls)]" in line
    assert "[turn 2: 1 repeated call]" in line  # an answered turn with a repeat
    assert "reached the tool-round limit: follow_up_detroit" in eval_run.summary_line([r])
    json.dumps(r, default=str)  # latest.json stays serialisable


def test_failed_later_turn_keeps_the_status_of_the_turns_that_completed(monkeypatch, tmp_path):
    fake = FakeClient([breakdown_round(i) for i in range(MAX)] + [final(hubs=HUB)]
                      + [overloaded(), overloaded()])  # turn 2: Sonnet and Haiku both down
    monkeypatch.setattr(agent, "_client", lambda: fake)
    monkeypatch.setattr(eval_run, "HERE", tmp_path)
    try:
        eval_run.run_case(TWO_TURNS)
        raise AssertionError("turn 2 should have failed")
    except agent.FallbackFailedError as e:
        r = eval_run.failed_case(TWO_TURNS, e.describe(), e.turn_models, e.turn_infos)
    assert r["status"] == "answered_at_tool_limit" and r["tool_rounds"] == MAX
    assert len(r["turns"]) == 1 and len(r["models"]) == 1
    assert "reached the tool-round limit: follow_up_detroit" in eval_run.summary_line([r])
    assert "[turn 1: status=answered_at_tool_limit" in eval_run.format_case_line(r)
    plain = eval_run.failed_case(TWO_TURNS, "boom")
    assert plain["status"] is None and plain["turns"] == [] and "status=" not in eval_run.format_case_line(plain)
