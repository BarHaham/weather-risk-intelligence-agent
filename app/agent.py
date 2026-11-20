"""Claude tool-use loop with a one-time model fallback.

Each user turn:
  1. send history + new message with the tool list and a JSON-schema output format
  2. while Claude asks for tools, run them (deterministic Python) and send results back
  3. the final text block must be JSON matching AgentAnswer (structured outputs); it is
     validated again with Pydantic before it leaves this module.
History is stored append-only per session, so follow-up questions see earlier turns.

Models: every turn starts on llm.model (Claude Sonnet 5.5). If a call fails because that model
is temporarily unavailable (overloaded, 5xx, 429, timeout, connection error) after the SDK's own
retries, the same request is tried once on llm.fallback_model (Claude Haiku 5.5) and the rest of
the turn stays there. If that attempt fails too, FallbackFailedError keeps both real errors.
"""
from __future__ import annotations

import json
import os
import re
import uuid

import anthropic
import httpx2
from pydantic import ValidationError

from app import store, tools
from app.config import hubs, settings
from app.schemas import AgentAnswer, answer_json_schema

SIMULATE_ENV = "WRIA_SIMULATE_OUTAGE"


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
- Tool results are deterministic and complete for their inputs: calling a tool again with the \
same input returns the same data. Don't repeat a call; use the result you already have.
- Scores come from a deterministic model (rank_hubs, compare_hubs, get_score_breakdown). \
Report them; do not re-weight or re-rank them yourself.
- For explanations ("why is X high/low"), call get_score_breakdown once per hub and name the \
hazards that contribute the most points, with the raw inputs behind them. If the question's \
premise doesn't match the model (e.g. it says "high" but the tier is Low), say so first and give the \
actual score and tier. FEMA "Very High" ratings are exposure context, not the score.
- For day-count questions use weather_stat. State the exact condition, the count and the \
denominator (e.g. "43 of 365 days"). "Last year" means calendar year {last_year}, the latest \
full year of data. The default snow day is any snowfall (> 0 cm); use another threshold only \
when the user asks for one. A snowfall threshold X means at least X cm (>= X), so pass the \
user's number exactly as given (1 for "at least 1 cm", never 0.99 or a second variant). Quote \
the tool's "condition" wording in the answer and in the figure labels.
- Distinguish hazard frequency (what the score uses) from FEMA risk ratings, which reflect the \
value of everything exposed in the whole county and are context only.
- Questions outside weather risk for these hubs (other cities, wildfire, earthquake, live \
forecasts, unrelated topics): set in_scope=false and explain what is covered.
- Use the conversation history for follow-ups ("and Detroit?" refers to the previous question).

Final answer format (JSON, enforced by schema). The UI shows the answer text first and puts \
all figures in a collapsed "Breakdown" the analyst can expand:
- answer: concise Markdown for an analyst. Lead with the main result in 1-3 sentences, at most \
~200 words in total. Do not list breakdown numbers (per month, per component, per hub) in the \
answer text; they belong in figures as "detail" and would otherwise appear twice.
- hubs: each hub you discuss with the score you cite and which hazard it belongs to.
- figures: each other number with its source tool. role "headline" for the 1-3 numbers the \
answer text states; role "detail" for breakdown rows (e.g. one per month).
- reasoning_steps: short steps you took, e.g. which tools and why.
- data_sources, assumptions, confidence. Assumptions are the conditions or thresholds and \
the data caveats behind the numbers you cite, plus the weights when you cite scores. Take \
them from the tool results you already have in this conversation; call get_methodology for \
them only when those results don't state what you need.
- follow_up_suggestions: up to 3 useful next questions."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _blocks_to_dicts(content) -> list[dict]:
    # Keep every block (thinking, tool_use, text) unchanged for replay.
    return [b.to_dict(mode="json", exclude_none=True) for b in content]


# ---------- availability and error reporting ----------

def is_unavailable(err: BaseException) -> bool:
    """True for errors that mean "this model is temporarily unavailable", so a fallback can help.
    Configuration and request problems (400, 401, 403, 404, 501) are not in this list."""
    if isinstance(err, anthropic.InternalServerError):  # 500, 503, 504, 529 overloaded, ...
        return getattr(err, "status_code", None) != 501
    return isinstance(err, (anthropic.APIConnectionError,   # includes APITimeoutError
                            anthropic.RateLimitError,       # 429 (separate pool per model)
                            anthropic.RequestTimeoutError))  # 408


def describe_error(err: BaseException) -> str:
    """Class, HTTP status and the API's own error type/message, e.g.
    'PermissionDeniedError 403 permission_error: Your key cannot use this model'."""
    parts = [type(err).__name__]
    status = getattr(err, "status_code", None)
    if status:
        parts.append(str(status))
    body = getattr(err, "body", None)
    api_type = api_msg = None
    if isinstance(body, dict):
        detail = body.get("error") if isinstance(body.get("error"), dict) else body
        api_type, api_msg = detail.get("type"), detail.get("message")
    if api_type:
        parts.append(str(api_type))
    text = " ".join(parts)
    if api_msg:
        text += f": {api_msg}"
    elif isinstance(err, anthropic.APIStatusError):
        # Non-JSON body, e.g. a proxy's or gateway's text/HTML page: keep its text.
        raw = body if isinstance(body, str) else getattr(err, "message", "")
        raw = " ".join(str(raw or "").split())
        if raw and not raw.startswith("Error code:"):
            text += f": {raw[:200]}"
    elif not status:
        text += f": {err}"
        # Connection errors hide the real reason (SSL verification, DNS, proxy) in the cause.
        cause = err.__cause__ or err.__context__
        if cause is not None and str(cause):
            text += f" ({type(cause).__name__}: {' '.join(str(cause).split())[:200]})"
    if getattr(err, "simulated", False):
        text += " [simulated]"
    return text


_INVALID_SIGNATURE_RE = re.compile(r"invalid\W+signature", re.IGNORECASE)


def is_expired_conversation(err: BaseException) -> bool:
    """True for the 400 the API returns when a stored conversation's reasoning (thinking) blocks
    no longer validate: "Invalid `signature` in `thinking` block". That happens when an app
    update changed the system prompt or tools after the conversation was stored (preserved
    thinking's history-editing check), or when a signature can't be verified. The conversation
    can't be continued; a new one works."""
    if isinstance(err, FallbackFailedError):
        return is_expired_conversation(err.fallback_error)
    return isinstance(err, anthropic.BadRequestError) and bool(
        _INVALID_SIGNATURE_RE.search(describe_error(err)))


class FallbackFailedError(Exception):
    """The primary model was unavailable and the fallback model failed too. Keeps both
    original SDK exceptions so an outage can be told apart from e.g. a permissions problem."""

    def __init__(self, primary_model: str, primary_error: BaseException,
                 fallback_model: str, fallback_error: BaseException):
        self.primary_model, self.primary_error = primary_model, primary_error
        self.fallback_model, self.fallback_error = fallback_model, fallback_error
        super().__init__(self.describe())

    @property
    def fallback_unavailable(self) -> bool:
        return is_unavailable(self.fallback_error)

    def describe(self) -> str:
        return (f"{self.primary_model} unavailable ({describe_error(self.primary_error)}); "
                f"{self.fallback_model} fallback failed: {describe_error(self.fallback_error)}")


def simulated_outage_models() -> set[str]:
    """Models listed in WRIA_SIMULATE_OUTAGE (comma-separated). Empty or unset = off."""
    raw = os.environ.get(SIMULATE_ENV, "")
    return {m.strip() for m in raw.split(",") if m.strip()}


def _simulated_overload(model: str) -> anthropic.OverloadedError:
    body = {"type": "error", "error": {"type": "overloaded_error",
                                       "message": f"Simulated outage for {model} ({SIMULATE_ENV})"}}
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.OverloadedError(f"Error code: 529 - {body}", body=body,
                                    response=httpx2.Response(529, request=request, json=body))
    err.simulated = True
    return err


# ---------- API call ----------

def _create(client, messages: list[dict], model: str, max_retries: int,
            tool_choice: dict | None = None):
    if model in simulated_outage_models():
        raise _simulated_overload(model)
    cfg = settings()["llm"]
    # Streamed, so the read timeout limits the gap between events, not the whole generation:
    # a slow but healthy answer is not mistaken for an outage, while a stalled call still
    # fails over. (eager_input_streaming is left off on purpose: tool inputs are tiny and
    # strict: true keeps the server-side schema validation of tool arguments.)
    timeout = httpx2.Timeout(connect=10.0, read=float(cfg["stream_idle_timeout_s"]),
                             write=30.0, pool=30.0)
    # system and tools are identical for every model and every request: changing them would
    # invalidate the thinking blocks replayed from earlier turns. tool_choice is outside that
    # check, so the final reply after the tool-round limit can turn tools off (see chat()).
    extra = {"tool_choice": tool_choice} if tool_choice else {}
    with client.with_options(max_retries=max_retries, timeout=timeout).messages.stream(
            model=model,
            max_tokens=cfg["max_tokens"],
            system=_system_prompt(),
            tools=tools.TOOL_DEFS,
            messages=messages,
            cache_control={"type": "ephemeral"},
            output_config={"effort": cfg["effort"],
                           "format": {"type": "json_schema", "schema": answer_json_schema()}},
            **extra,
    ) as stream:
        return stream.get_final_message()


class _Turn:
    """Model state for one user turn: starts on the primary, switches to the fallback at most once."""

    def __init__(self, client):
        cfg = settings()["llm"]
        self.client = client
        self.primary, self.fallback_model = cfg["model"], cfg.get("fallback_model")
        self.max_retries = cfg["max_retries"]
        self.active = self.primary
        self.primary_error: BaseException | None = None
        self.fallback: dict | None = None
        self.calls: list[str] = []

    def call(self, messages: list[dict], tool_choice: dict | None = None):
        try:
            resp = _create(self.client, messages, self.active, self.max_retries, tool_choice)
        except anthropic.APIError as e:
            if self.active != self.primary:  # already on the fallback: report both errors
                raise FallbackFailedError(self.primary, self.primary_error,
                                          self.active, e) from e
            if not self.fallback_model or not is_unavailable(e):
                raise
            resp = self._switch(messages, e, tool_choice)
        self.calls.append(resp.model)
        return resp

    def _switch(self, messages: list[dict], primary_error: BaseException,
                tool_choice: dict | None = None):
        self.primary_error = primary_error
        self.fallback = {"from": self.primary, "to": self.fallback_model,
                         "reason": describe_error(primary_error),
                         "simulated": bool(getattr(primary_error, "simulated", False)),
                         "at_call": len(self.calls) + 1}
        try:  # "try once": a single attempt, no SDK retries
            resp = _create(self.client, messages, self.fallback_model, max_retries=0,
                           tool_choice=tool_choice)
        except anthropic.APIError as e:
            raise FallbackFailedError(self.primary, primary_error, self.fallback_model, e) from e
        self.active = self.fallback_model  # rest of this turn stays on the fallback
        return resp

    def report(self) -> dict:
        return {"requested": self.primary,
                "answered_by": self.calls[-1] if self.calls else None,
                "calls": self.calls, "fallback": self.fallback}


# ---------- one user turn ----------

# chat() status values. Only "answered" and "answered_at_tool_limit" carry the model's answer;
# the others carry a short harness message whose in_scope is None (not determined), so a turn
# that stopped early is never reported as an out-of-scope question.
ANSWERED = "answered"
ANSWERED_AT_TOOL_LIMIT = "answered_at_tool_limit"  # valid answer from the tools-off final reply
TOOL_LIMIT = "tool_limit"            # limit reached and the final reply was not a valid answer
CUT_OFF = "cut_off"                  # stop_reason max_tokens
REFUSED = "refused"                  # stop_reason refusal (not retried on another model)
INVALID_ANSWER = "invalid_answer"    # final JSON failed validation twice
# Not returned by chat(): set on the exception's turn_progress when a request in the turn fails
# (e.g. both models unavailable), so the evals can still record the rounds and calls made.
API_ERROR = "api_error"

NO_TOOLS = {"type": "none"}


def _out_of_band_answer(text: str) -> AgentAnswer:
    """A harness message in place of the model's answer. in_scope=None: the question was not
    judged, so it must not be shown or scored as out of scope."""
    return AgentAnswer(in_scope=None, answer=text, hubs=[], figures=[], reasoning_steps=[],
                       data_sources=[], assumptions=[], confidence="low",
                       follow_up_suggestions=[])


def _limit_notice(max_rounds: int) -> dict:
    # A mid-conversation system message: the operator channel for harness notices (text in the
    # tool-result message could be read as part of the tool output). It follows the last tool
    # results and is followed by the model's reply, so the stored history stays append-only.
    return {"role": "system", "content":
            f"Tool limit reached: this turn has used all {max_rounds} tool rounds, so tools are "
            "off for this reply. Give your final answer now from the tool results above. If "
            "they are not enough to answer fully, say what is missing and lower your "
            "confidence."}


def _call_key(name: str, args: dict) -> str:
    return name + json.dumps(args, sort_keys=True, default=str)


def _repeat_result(call_no: int) -> str:
    return json.dumps({
        "repeated_call": True, "same_as_call": call_no,
        "note": f"Same tool and input as call {call_no} in this turn. Tools are deterministic, "
                "so that result (above) is unchanged. Use it rather than calling again."})


def chat(message: str, session_id: str | None = None, client=None) -> dict:
    """Run one user turn. Returns {session_id, answer, status, tool_rounds, max_tool_rounds,
    tool_calls, models, usage}; see the status constants above.

    Tool rounds are capped at llm.max_tool_rounds. After the last allowed round, one more
    request is sent with tools turned off (tool_choice none) and a notice, so the model answers
    from the data it already has instead of stopping without an answer. An identical repeated
    call (same tool, same input) in the same turn is not run again: its result points to the
    earlier one."""
    client = client or _client()
    session_id = session_id or uuid.uuid4().hex
    history = store.load_messages(session_id)
    new: list[dict] = [{"role": "user", "content": message}]
    trace: list[dict] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    answer: AgentAnswer | None = None
    status = ""
    turn = _Turn(client)
    max_rounds = settings()["llm"]["max_tool_rounds"]
    limit_text = (f"Stopped after {max_rounds} tool rounds without a final answer. Try asking "
                  "again, or narrow the question.")
    rounds = 0           # tool rounds run this turn (pause_turn counts too, to stay bounded)
    seen: dict[str, tuple[int, bool]] = {}  # call key -> (1-based call number, is_error)
    repaired = notice_sent = False

    while True:
        final = rounds >= max_rounds
        if final and not notice_sent:
            new.append(_limit_notice(max_rounds))
            notice_sent = True
        try:
            resp = turn.call(history + new, NO_TOOLS if final else None)
        except Exception as e:  # raised to the caller; nothing of this turn is stored
            e.turn_progress = {"status": API_ERROR, "tool_rounds": rounds,
                               "max_tool_rounds": max_rounds, "tool_calls": trace}
            raise
        for k in usage:
            usage[k] += getattr(resp.usage, k, 0) or 0

        # A refusal can have empty content and a max_tokens cut-off can end in a tool_use with
        # no result; storing either would make every later request in the session a 400.
        # Dropping this last message keeps the history append-only and valid.
        if resp.stop_reason == "refusal":
            answer, status = _out_of_band_answer("I can't help with that request."), REFUSED
            break
        if resp.stop_reason == "max_tokens":
            answer = _out_of_band_answer(
                (f"Reached the {max_rounds}-round tool limit, and the final answer was then "
                 "cut off (max_tokens). Please narrow the question.") if final else
                "The answer was cut off (max_tokens). Please narrow the question.")
            status = CUT_OFF
            break
        if final and resp.stop_reason in ("tool_use", "pause_turn"):
            # Tools are off for this request, so this should not happen; a tool_use without
            # its result must not be stored either way.
            answer, status = _out_of_band_answer(limit_text), TOOL_LIMIT
            break

        new.append({"role": "assistant", "content": _blocks_to_dicts(resp.content)})

        if resp.stop_reason == "tool_use":
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                args = block.input if isinstance(block.input, dict) else json.loads(block.input)
                key = _call_key(block.name, args)
                if key in seen:  # same result as before (an error stays an error)
                    first, is_error = seen[key]
                    output = _repeat_result(first)
                    trace.append({"tool": block.name, "input": args, "is_error": is_error,
                                  "repeated": True, "same_as_call": first})
                else:
                    output, is_error = tools.execute(block.name, args)
                    trace.append({"tool": block.name, "input": args, "is_error": is_error})
                    seen[key] = (len(trace), is_error)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output, "is_error": is_error})
            new.append({"role": "user", "content": results})  # all results in one message
            rounds += 1
            continue

        if resp.stop_reason == "pause_turn":
            rounds += 1
            continue

        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            answer = AgentAnswer.model_validate_json(text)
            status = ANSWERED_AT_TOOL_LIMIT if final else ANSWERED
            break
        except ValidationError as e:
            if repaired:
                detail = f"Validation error: {e.errors()[:2]}"
                if final:
                    answer = _out_of_band_answer(f"{limit_text} (The final reply did not match "
                                                 f"the answer format. {detail})")
                    status = TOOL_LIMIT
                else:
                    answer = _out_of_band_answer(f"The agent returned an invalid response. "
                                                 f"{detail}")
                    status = INVALID_ANSWER
                break
            repaired = True  # one repair attempt (tools stay off if the limit was reached)
            new.append({"role": "user", "content":
                        f"Your final answer did not validate against the schema: {e}. "
                        "Reply again with only the corrected JSON."})

    # A system message must be followed by an assistant turn (or be last in the request).
    # If the reply after the limit notice was dropped (refusal, cut-off), drop the notice too
    # so the next turn's request stays valid; nothing was generated after it.
    if new[-1]["role"] == "system":
        new.pop()
    store.append_messages(session_id, new)
    return {"session_id": session_id, "answer": answer.model_dump(), "status": status,
            "tool_rounds": rounds, "max_tool_rounds": max_rounds, "tool_calls": trace,
            "models": turn.report(), "usage": usage}
