"""Continuing a conversation stored before an app update: the API rejects its saved reasoning
blocks ("Invalid `signature` in `thinking` block"); /chat answers 409 conversation_expired so the
UI can offer a fresh start, and other 400s keep their normal handling."""
import uuid

import anthropic
import pytest
from fastapi import HTTPException

from app import agent, main, store
from tests.fakes import HAIKU, SONNET, FakeClient, api_error, overloaded

BOUND = ("messages.5.content.0: Invalid `signature` in `thinking` block. The block is bound to a "
         "different conversation. Remove the block, or set "
         "`thinking.block_binding.prefix_mismatch_behavior` to \"drop_block\".")
TAMPERED = "messages.1.content.0: Invalid `signature` in `thinking` block"


def setup_module():
    store.build_from_cache()


def bad_request(message):
    return api_error(anthropic.BadRequestError, 400, "invalid_request_error", message)


@pytest.mark.parametrize("message", [BOUND, TAMPERED, "Invalid signature in thinking block"])
def test_invalid_thinking_signature_is_an_expired_conversation(message):
    assert agent.is_expired_conversation(bad_request(message))


@pytest.mark.parametrize("err", [
    bad_request("max_tokens: Input should be greater than 0"),
    api_error(anthropic.AuthenticationError, 401, "authentication_error", "invalid x-api-key"),
    overloaded(),
])
def test_other_errors_are_not(err):
    assert not agent.is_expired_conversation(err)


def chat_error(monkeypatch, exc):
    def boom(message, session_id):
        raise exc
    monkeypatch.setattr(agent, "chat", boom)
    with pytest.raises(HTTPException) as e:
        main.chat(main.ChatRequest(message="And Detroit?", session_id="old-session"))
    return e.value


def test_chat_returns_409_with_a_short_message_and_code(monkeypatch):
    e = chat_error(monkeypatch, bad_request(BOUND))
    assert e.status_code == 409
    assert e.detail["code"] == "conversation_expired"
    assert "Start a new conversation" in e.detail["message"] and len(e.detail["message"]) < 160
    assert "signature" not in e.detail["message"].lower()  # no API jargon for the user


def test_expired_reported_by_the_fallback_model_is_also_409(monkeypatch):
    e = chat_error(monkeypatch, agent.FallbackFailedError(SONNET, overloaded(), HAIKU, bad_request(TAMPERED)))
    assert e.status_code == 409 and e.detail["code"] == "conversation_expired"


def test_other_400s_keep_the_normal_502(monkeypatch):
    e = chat_error(monkeypatch, bad_request("max_tokens: Input should be greater than 0"))
    assert e.status_code == 502 and "BadRequestError 400" in e.detail


def test_fallback_failures_keep_their_503_or_502(monkeypatch):
    assert chat_error(monkeypatch, agent.FallbackFailedError(SONNET, overloaded(), HAIKU, overloaded())).status_code == 503
    perm = api_error(anthropic.PermissionDeniedError, 403, "permission_error", "no access")
    assert chat_error(monkeypatch, agent.FallbackFailedError(SONNET, overloaded(), HAIKU, perm)).status_code == 502


def test_signature_error_is_not_retried_on_haiku_and_stores_nothing():
    s = uuid.uuid4().hex
    fake = FakeClient([bad_request(BOUND)])
    with pytest.raises(anthropic.BadRequestError):
        agent.chat("And Detroit?", s, client=fake)
    assert fake.models == [SONNET]          # a 400 is not an outage: no fallback
    assert store.load_messages(s) == []     # the failed turn is not stored
