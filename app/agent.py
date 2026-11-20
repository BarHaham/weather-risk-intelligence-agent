"""Claude tool-use loop.

Each user turn:
  1. send history + new message with the tool list and a JSON-schema output format
  2. while Claude asks for tools, run them (deterministic Python) and send results back
  3. the final text block must be JSON matching AgentAnswer (structured outputs); it is
     validated again with Pydantic before it leaves this module.
History is stored append-only per session, so follow-up questions see earlier turns.
"""
from __future__ import annotations

import json
import uuid

import anthropic
from pydantic import ValidationError

from app import store, tools
from app.config import hubs, settings
from app.schemas import AgentAnswer, answer_json_schema

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _system_prompt() -> str:
    cfg = settings()
    hub_lines = "\n".join(f"- {h['id']}: {h['city']}, {h['state']} ({h['region']}; "
                          f"{h['county']} County)" for h in hubs())
    last_year = cfg["data"]["history_end"][:4]
    return f"""You are the Weather Risk Intelligence Agent for a US logistics company. Analysts \
use you to decide which distribution hubs are most exposed to weather disruption and should be \
prioritised for resilience investment.

Hubs covered (no others):
{hub_lines}

How to work:
- Every number you state must come from a tool result in this conversation. Never estimate, \
recall or invent figures. If a tool returns an error or no data, say so plainly.
- Scores come from a deterministic model (rank_hubs, compare_hubs, get_score_breakdown). \
Report them; do not re-weight or re-rank them yourself.
- For explanations ("why is X high"), name the hazards that contribute the most points and \
the raw inputs behind them.
- For day-count questions use weather_stat. State the exact condition, the count and the \
denominator (e.g. "43 of 365 days"). "Last year" means calendar year {last_year}, the latest \
full year of data. The default snow day is any snowfall above 0 cm; use another threshold \
only when the user asks for one.
- Distinguish hazard frequency (what the score uses) from FEMA risk ratings, which reflect the \
value of everything exposed in the whole county and are context only.
- Questions outside weather risk for these hubs (other cities, wildfire, earthquake, live \
forecasts, unrelated topics): set in_scope=false and explain what is covered.
- Use the conversation history for follow-ups ("and Detroit?" refers to the previous question).

Final answer format (JSON, enforced by schema):
- answer: concise Markdown for an analyst, lead with the conclusion, at most ~200 words.
- hubs: each hub you discuss with the score you cite and which hazard it belongs to.
- figures: each other number you cite (counts, percentages, frequencies) and its source tool.
- reasoning_steps: short steps you took, e.g. which tools and why.
- data_sources, assumptions (thresholds, weights, data caveats), confidence.
- follow_up_suggestions: up to 3 useful next questions."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _blocks_to_dicts(content) -> list[dict]:
    # Keep every block (thinking, tool_use, text, fallback) unchanged for replay.
    return [b.to_dict(mode="json", exclude_none=True) for b in content]


def _create(client, messages: list[dict]):
    cfg = settings()["llm"]
    kwargs = dict(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        system=_system_prompt(),
        tools=tools.TOOL_DEFS,
        messages=messages,
        cache_control={"type": "ephemeral"},
        output_config={"effort": cfg["effort"],
                       "format": {"type": "json_schema", "schema": answer_json_schema()}},
    )
    if cfg.get("refusal_fallback"):
        kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
    return client.beta.messages.create(**kwargs)


def _out_of_band_answer(text: str) -> AgentAnswer:
    return AgentAnswer(in_scope=False, answer=text, hubs=[], figures=[], reasoning_steps=[],
                       data_sources=[], assumptions=[], confidence="low",
                       follow_up_suggestions=[])


def chat(message: str, session_id: str | None = None, client=None) -> dict:
    client = client or _client()
    session_id = session_id or uuid.uuid4().hex
    history = store.load_messages(session_id)
    new: list[dict] = [{"role": "user", "content": message}]
    trace: list[dict] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    answer: AgentAnswer | None = None
    model_used = None
    max_rounds = settings()["llm"]["max_tool_rounds"]
    repaired = False

    for _ in range(max_rounds + 1):
        resp = _create(client, history + new)
        model_used = resp.model
        for k in usage:
            usage[k] += getattr(resp.usage, k, 0) or 0
        new.append({"role": "assistant", "content": _blocks_to_dicts(resp.content)})

        if resp.stop_reason == "tool_use":
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                output, is_error = tools.execute(block.name, args)
                trace.append({"tool": block.name, "input": args, "is_error": is_error})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output, "is_error": is_error})
            new.append({"role": "user", "content": results})  # all results in one message
            continue

        if resp.stop_reason == "pause_turn":
            continue
        if resp.stop_reason == "refusal":
            answer = _out_of_band_answer("I can't help with that request.")
            break
        if resp.stop_reason == "max_tokens":
            answer = _out_of_band_answer("The answer was cut off (max_tokens). Please narrow "
                                         "the question.")
            break

        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            answer = AgentAnswer.model_validate_json(text)
            break
        except ValidationError as e:
            if repaired:
                answer = _out_of_band_answer("The agent returned an invalid response. "
                                             f"Validation error: {e.errors()[:2]}")
                break
            repaired = True  # one repair attempt
            new.append({"role": "user", "content":
                        f"Your final answer did not validate against the schema: {e}. "
                        "Reply again with only the corrected JSON."})
    else:
        answer = _out_of_band_answer("Stopped after too many tool rounds.")

    store.append_messages(session_id, new)
    return {"session_id": session_id, "answer": answer.model_dump(), "tool_calls": trace,
            "model": model_used, "usage": usage}
