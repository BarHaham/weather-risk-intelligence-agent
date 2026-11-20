"""Agent-loop mechanics with a fake Anthropic client (no network, no API key).

Checks: tool calls are executed and returned in one user message, the history is replayed
on the next request, the final JSON is validated, and follow-ups see earlier turns.
"""
import json
import uuid
from types import SimpleNamespace

from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock

from app import agent, store


def answer_json(**over):
    a = {"in_scope": True, "answer": "Houston ranks first.", "hubs": [],
         "figures": [], "reasoning_steps": ["called rank_hubs"], "data_sources": ["FEMA NRI"],
         "assumptions": ["weights from config"], "confidence": "medium",
         "follow_up_suggestions": []}
    a.update(over)
    return json.dumps(a)


def msg(content, stop):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop, model="fake", usage=usage)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"], default=str)))
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        assert all(t["strict"] for t in kwargs["tools"])
        return self.responses.pop(0)


def setup_module():
    store.build_from_cache()


def test_tool_round_then_validated_answer():
    fake = FakeClient([
        msg([BetaToolUseBlock(type="tool_use", id="t1", name="rank_hubs",
                              input={"hazard": "overall", "region": None})], "tool_use"),
        msg([BetaTextBlock(type="text", text=answer_json())], "end_turn"),
    ])
    sid = uuid.uuid4().hex
    out = agent.chat("Which hub is riskiest?", sid, client=fake)
    assert out["answer"]["answer"] == "Houston ranks first."
    assert out["tool_calls"] == [{"tool": "rank_hubs", "input": {"hazard": "overall", "region": None},
                                  "is_error": False}]
    second = fake.requests[1]
    assert second[-1]["role"] == "user" and second[-1]["content"][0]["type"] == "tool_result"
    assert json.loads(second[-1]["content"][0]["content"])["ranked_by"] == "overall"


def test_follow_up_replays_history():
    sid = uuid.uuid4().hex
    agent.chat("first", sid, client=FakeClient([msg([BetaTextBlock(type="text", text=answer_json())], "end_turn")]))
    fake = FakeClient([msg([BetaTextBlock(type="text", text=answer_json())], "end_turn")])
    agent.chat("and Detroit?", sid, client=fake)
    sent = fake.requests[0]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "first" and sent[-1]["content"] == "and Detroit?"


def test_invalid_json_gets_one_repair_attempt():
    fake = FakeClient([
        msg([BetaTextBlock(type="text", text='{"answer": "missing fields"}')], "end_turn"),
        msg([BetaTextBlock(type="text", text=answer_json(answer="fixed"))], "end_turn"),
    ])
    out = agent.chat("q", uuid.uuid4().hex, client=fake)
    assert out["answer"]["answer"] == "fixed"
    assert "did not validate" in fake.requests[1][-1]["content"]


def test_unknown_tool_input_is_reported_as_tool_error():
    fake = FakeClient([
        msg([BetaToolUseBlock(type="tool_use", id="t1", name="get_score_breakdown",
                              input={"hub_id": "seattle"})], "tool_use"),
        msg([BetaTextBlock(type="text", text=answer_json(in_scope=False))], "end_turn"),
    ])
    out = agent.chat("Seattle?", uuid.uuid4().hex, client=fake)
    assert out["tool_calls"][0]["is_error"] is True
    assert fake.requests[1][-1]["content"][0]["is_error"] is True
