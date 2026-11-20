"""Test doubles for the Anthropic client: scripted responses and real SDK error objects."""
import json
from types import SimpleNamespace

import anthropic
import httpx2

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def answer_json(**over) -> str:
    a = {"in_scope": True, "answer": "Houston ranks first.", "hubs": [], "figures": [],
         "reasoning_steps": ["called rank_hubs"], "data_sources": ["FEMA NRI"],
         "assumptions": ["weights from config"], "confidence": "medium",
         "follow_up_suggestions": []}
    a.update(over)
    return json.dumps(a)


def msg(content, stop, model=None):
    """A scripted response. model=None means 'whatever model was requested'."""
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop, model=model, usage=usage)


def api_error(cls, status: int, etype: str, message: str):
    """A real SDK status error, as the client would raise it."""
    body = {"type": "error", "error": {"type": etype, "message": message}}
    return cls(f"Error code: {status} - {body}", body=body,
               response=httpx2.Response(status, request=REQUEST, json=body))


def overloaded(message="Overloaded"):
    return api_error(anthropic.OverloadedError, 529, "overloaded_error", message)


class _Stream:
    """Mimics MessageStreamManager: the request (and any error) happens on __enter__."""

    def __init__(self, client, kwargs):
        self.client, self.kwargs = client, kwargs

    def __enter__(self):
        return self.client._send(self.kwargs)

    def __exit__(self, *exc):
        return False


class _Final:
    def __init__(self, message):
        self.message = message

    def get_final_message(self):
        return self.message


class FakeClient:
    """Scripted stand-in for anthropic.Anthropic (streaming interface). Each script item is a
    response or an exception to raise. Records every request (model, retries, timeout,
    messages, system, tools, tool_choice)."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self._options: dict = {}
        self.messages = SimpleNamespace(stream=lambda **kw: _Stream(self, kw))

    def with_options(self, **kwargs):
        self._options = kwargs
        return self

    def _send(self, kwargs):
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        assert all(t["strict"] for t in kwargs["tools"])
        assert "fallbacks" not in kwargs and "betas" not in kwargs
        self.requests.append({
            "model": kwargs["model"], "max_retries": self._options.get("max_retries"),
            "timeout": self._options.get("timeout"), "system": kwargs["system"],
            "tools": kwargs["tools"], "tool_choice": kwargs.get("tool_choice"),
            "messages": json.loads(json.dumps(kwargs["messages"], default=str))})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if item.model is None:
            item = SimpleNamespace(**{**vars(item), "model": kwargs["model"]})
        return _Final(item)

    @property
    def models(self) -> list[str]:
        return [r["model"] for r in self.requests]
