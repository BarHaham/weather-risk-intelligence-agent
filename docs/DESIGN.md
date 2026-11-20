# Design: Weather Risk Intelligence Agent

## 1. What it does, and what it doesn't
Analysts ask questions in a chat UI, for example "Which Midwest hubs are most exposed to
winter disruption?" or "Why is Dallas high?". The numbers in every answer come from a
**deterministic Python risk model**. Claude decides which model functions to call, then
explains the results. It never produces scores itself.

**Scope (deliberately narrow):**
- **Hubs:** 7 (Chicago, Minneapolis, Detroit, Denver, Dallas, Miami, Houston).
- **Hazards:** 5 (winter, flood, hurricane, wind/tornado, heat).
- **Data sources:** 2 public sources.
- **Out of scope:** wildfire, earthquake, drought, hail, live forecasts and alerts, other cities.
- **Voice input:** the browser's built-in speech recognition (Web Speech API, English) fills the
  question box; the user edits and sends it (see "Chat UI and hub map").
- **Score changes** on `main` open a GitHub issue (see "Score-change monitoring"); alerts beyond
  that are a next step (§9).

## 2. System architecture

```
 Browser (app/static/index.html: chat + clickable US hub map)
   │  POST /chat {message, session_id}          GET /rankings, /map/hubs, /static/us-states.json, ...
   ▼                                              ▲
 FastAPI (app/main.py) ───────────────────────────┘
   │
   ▼
 Agent loop (app/agent.py)  ── Claude Messages API: claude-sonnet-5-5,
   │                             falls back once to claude-haiku-5-5 when Sonnet is unavailable
   │   • sends: system prompt, 6 strict tools, history, output_config.format = AgentAnswer JSON schema
   │   • loop: model asks for tools → run them → send results → … → final JSON answer
   │   • validates the final JSON with Pydantic (one repair retry), stores history append-only
   ▼
 Tools (app/tools.py) ── thin wrappers, no logic of their own
   ├─ rank_hubs / compare_hubs / get_score_breakdown ──▶ app/scoring.py  (risk model)
   ├─ weather_stat                                     ──▶ app/metrics.py  (day counts)
   └─ list_hubs / get_methodology                      ──▶ config.yaml, data/hubs.json
   ▼
 SQLite (app/store.py, data/weather_risk.db)  ◀── built from ──  data/cache/*.json (raw API responses)
                                                                     ▲
 scripts/refresh_data.py ── app/sources/open_meteo.py (Open-Meteo daily gridded estimates, 2020-2025)
                         └─ app/sources/fema_nri.py   (FEMA National Risk Index, counties)
```

**How the components talk**
1. The UI only talks to FastAPI, over JSON/HTTP. The ranking panel calls `/rankings`, and the
   hub map calls `/map/hubs` (positions, scores and tiers) and `/static/us-states.json` (state
   outlines), so the deterministic numbers are visible without any LLM call.
2. `/chat` calls `agent.chat()`. Each user turn is one Claude tool-use loop, which usually makes
   2–4 API calls.
3. Tools return JSON produced by `scoring.py` and `metrics.py`. Those modules read SQLite only.
   Nothing reaches the network while the app is answering a question.
4. The public APIs are called only by `scripts/refresh_data.py`. It writes raw responses to
   `data/cache/` and rebuilds SQLite from them.

## 3. The LLM contract (structured output)
- **Tool inputs:** every tool has `strict: true`, `additionalProperties: false`, all fields required,
  and optional values written as nullable. Hub IDs, hazards, regions and metrics are enums, so the
  model can't request an unknown hub. A tool error comes back as `is_error` and the model explains it.
- **Final answer:** enforced with `output_config.format` (JSON schema `AgentAnswer` in
  `app/schemas.py`), then re-validated with Pydantic. Fields:
  - `in_scope`, `answer` (markdown). `in_scope` is `null` only in the harness's own short
    messages when there is no answer (see the tool-round limit below).
  - `hubs[{hub_id, hazard, score}]`, `figures[{label, value, unit, source_tool}]`
  - `reasoning_steps`, `data_sources`, `assumptions`, `confidence`, `follow_up_suggestions`
- **Change from the plan:** the plan used a forced `submit_answer` tool. The current Claude models
  reject forced `tool_choice` (the first build used Opus 5.5; Sonnet 5.5 rejects it too), so the
  schema is enforced through structured outputs instead. The intent is unchanged: a JSON schema
  between the LLM and the code.
- **Grounding:** the system prompt says every number must come from a tool result. The evals
  check that every `hubs[].score` equals the model's score (±0.1).
- **Follow-ups:** history is stored per `session_id` in SQLite and replayed in full, including
  thinking and tool blocks. It is append-only, which the model's thinking blocks require.
- **Conversations from an earlier app version:** each stored thinking block is tied to the exact
  system prompt and tools it was produced with.
  - When an update changes them (as the snowfall-rule and breakdown changes did), continuing an
    older chat fails with "Invalid `signature` in `thinking` block". That is the API's
    history-editing check, enforced for newer accounts.
  - `/chat` turns that error into a 409 `conversation_expired`, and the UI shows a short message
    with a **Start a new conversation** button. The button returns to the opening screen with
    the question already in the box. Other 400s keep their normal 502.
  - An alternative would be the `thinking-binding-controls` beta with `drop_block`, which
    continues the old chat without its earlier reasoning. It isn't used, to stay off the beta.
- **Refusals and cut-offs:** `stop_reason` is checked for `refusal`, `max_tokens` and `pause_turn`.
  A refusal is not retried on another model: the earlier server-side refusal fallback was replaced
  by the availability fallback below. The refused (or cut-off) assistant message is not stored, so
  an empty turn or a dangling `tool_use` can't break later requests in the session.
- **Tool-round limit (`llm.max_tool_rounds`, 8):**
  - **What went wrong:** in one live eval run, `denver_snow_last_year` called `get_methodology`
    seven times and `dallas_why` kept calling `get_score_breakdown`. Both stopped at the limit
    with no answer. The replay was checked and is not the cause: streamed tool calls (including a
    no-input call) and their results go back to the API unchanged, and a mock-transport test
    keeps that checked. The causes were in the prompt and the loop:
    - `get_methodology` was described as the tool to call "when you need to state assumptions",
      and every answer has an `assumptions` field.
    - Nothing said that tool results are deterministic.
    - The Dallas question assumes the hub is "high". The model scores it Low (34.9, just under
      Moderate at 35), while FEMA's context ratings say "Very High", and the prompt didn't say
      how to handle a premise the data doesn't support.
    - Every request allowed tools, so a model that kept checking never had to answer.
  - **Prompt:** tool results are deterministic and complete, so a call isn't repeated.
    Assumptions come from the conditions and caveats already returned. For "why is X high", call
    `get_score_breakdown` once per hub, and if the premise doesn't match the model, say so and
    give the actual score and tier. Assumptions are the conditions or thresholds and caveats
    behind the cited numbers, plus the weights only when scores are cited, taken from results
    already in the conversation. `get_methodology` is for questions about the method, and for
    weights, thresholds or tiers a score answer needs that the results don't state (`rank_hubs`
    states none; `get_score_breakdown` has weights, sources and caveats but not the day
    thresholds). It is not needed for day counts: `weather_stat` returns its condition, source
    and caveat. Its description says one call is enough and an earlier turn's result still
    applies.
    Nothing tells the model to use fewer tools: Sonnet 5.5 follows such wording literally and
    may then skip a needed call.
  - **Repeated identical calls:** within one turn, a call with the same tool and the same input
    as an earlier call isn't run again. Its result says it repeats call *n* and that the earlier
    result is unchanged; a repeat of a failed call keeps `is_error`. The trace marks it
    `repeated` (and `same_as_call`), and the UI numbers the tool calls so *n* can be found.
  - **Finishing at the limit:** after 8 tool rounds, one more request is sent with
    `tool_choice: {"type": "none"}` and a mid-conversation system message saying the limit was
    reached and to answer from the results above. `system` and `tools` stay the same, so replayed
    thinking blocks stay valid; changing `tool_choice` only costs the messages cache for that one
    request. The reply is validated like any answer (with the one repair attempt, tools still off).
    The notice stays in the stored history, followed by the answer, so the history remains
    append-only. If that final reply is dropped (refusal, cut-off), the notice is dropped with it
    so the next request is still valid.
  - **Reporting:** `/chat` returns `status`, `tool_rounds` and `max_tool_rounds`:
    - `answered`: the normal case.
    - `answered_at_tool_limit`: answered by the tools-off final reply. The UI shows "Reached the
      8-round tool limit …" under the answer.
    - `tool_limit`: no valid answer even then.
    - `cut_off`, `refused` and `invalid_answer`: the other early stops. A cut-off after the
      limit says so in its message.

    The early stops carry a short message with `in_scope: null` (not judged), so the UI and the
    evals no longer report them as out-of-scope questions. Eval lines show any status other than
    `answered` and the number of repeated calls, and the summary lists every case with a turn
    that used all 8 rounds, whatever the final reply was. `latest.json` keeps each turn's
    status, rounds and tool calls (`turns`). A turn that failed with an API error is recorded
    too, as `api_error` with the rounds and calls it made (the exception carries them). The
    eval checks themselves are unchanged.

### Reviewer access code (public deploys)
- **What is gated:** `POST /chat`, the only route that calls Claude (a test checks that no other
  route calls the agent). The data endpoints are deterministic, cost nothing and stay open.
- **How the check works:** `app/access.py`, run as a FastAPI dependency on `/chat`, so a
  refused request never reaches `agent.chat()`.
  - With `REVIEWER_ACCESS_CODE` set, the `X-Access-Code` header must match. Both values are
    trimmed and compared in constant time (`hmac.compare_digest`).
  - Missing or wrong code: 401, with `access_code_required` or `access_code_invalid`.
  - Without a code on Render (detected from `RENDER=true` or `RENDER_SERVICE_ID`): 503
    `chat_disabled`, so a deploy can't spend API credits before the code is set.
  - Without a code anywhere else: open, so local runs and tests work as before.
- **UI:** a 401 shows a password field. The question is kept and sent again once a code is
  entered. The code lives in `sessionStorage` for that tab.
- **What it doesn't do:** there is no per-user identity and no rate limiting of failed attempts,
  so the code should be long and random. It is never logged or returned, and `/health` reports
  only the mode.

### Models and availability fallback
- **Primary:** `claude-sonnet-5-5` at effort `medium`, Sonnet 5.5's recommended start for multistep
  tool use. Thinking stays adaptive (on); it isn't disabled.
- **Fallback:** `claude-haiku-5-5`. It is tried **once** (a single attempt) when the primary is
  temporarily unavailable after the SDK's own retries (`llm.max_retries`). Calls are streamed, so
  only a stall longer than `llm.stream_idle_timeout_s` counts as a timeout, not a slow but healthy
  answer. "Temporarily unavailable" means 529 or another 5xx except 501, 429,
  408, a timeout or a connection error. 400, 401, 403, 404 and 501 are raised as they are.
- **Once switched:** the rest of that turn stays on Haiku. The next turn starts on Sonnet again.
- **If Haiku fails too:** `FallbackFailedError` keeps both original SDK exceptions. `/chat` returns
  503 if Haiku was also unavailable, or 502 for anything else (e.g. a 403 on the Haiku model), and
  the detail names both errors.
- **What doesn't change between models:** `system` and `tools` are identical for both models, and
  the history is replayed unchanged. Haiku can't read Sonnet 5.5's thinking blocks; the API drops
  them silently, and appending only (never editing) keeps the remaining blocks valid.
- **Reporting:** `/chat` returns `models = {requested, answered_by, calls, fallback}`, where
  `answered_by` is the model the API reports for the final call. The UI shows it under each
  answer, and every eval line and the eval summary show it too.
- **Testing:** `WRIA_SIMULATE_OUTAGE=<model ids>` makes those models fail with a synthetic 529
  before any request is sent. The tests also drive the real SDK HTTP stack through a mock
  transport, which checks the SDK's retries against Sonnet and Haiku's single attempt.
- **API key:** read from the git-ignored `.env` in the repo root, at app startup and when the eval
  runner starts. A non-empty environment variable takes precedence. UTF-8, UTF-8 BOM, UTF-16 and
  cp1252 files are all decoded. `/health` reports `api_key_source`, never the key.

### Chat UI and hub map
- **Voice input** (`index.html`, no server change).
  - **How it works:** each question box (opening screen, follow-up bar, hub card) gets a mic
    button when the browser has `SpeechRecognition` / `webkitSpeechRecognition`. That covers
    Chrome, Edge and Safari; elsewhere there is no button.
  - **Recognition:** `lang = "en-US"`, interim results on, one utterance per click.
  - **The text:** words go into the box as they are recognised, added after any typed text,
    within the box's 2000-character limit. Nothing is submitted, so the analyst edits and presses
    Ask.
  - **Stopping:** the recognizer ends by itself after a pause, and the mic button stops it and
    keeps what was heard. Typing, moving to another text field, sending, a new conversation,
    picking another hub or starting another mic abort at once, and late results are ignored.
    Only one mic runs at a time. When it ends, focus returns to the box only if the user hasn't
    moved on.
  - **While a question is running,** the mic stays usable: only the Ask buttons are disabled.
  - **Errors** (microphone blocked, no microphone, no speech, network, start failure) show a
    short `role="status"` note under the box.
  - **Privacy:** the browser handles the audio; Chrome uses Google's speech service. The app's
    server only ever receives the text that is sent. The mic is shown only in a secure context
    (HTTPS or localhost).
  - **Accessibility:** the mic is a toggle button with one fixed name; `aria-pressed` carries
    the listening state. There is a forced-colors style for Windows High Contrast and no pulse
    with reduced motion.
- **Main result first.** Each answer shows its text first. Every number then sits in one
  collapsed **Breakdown** section: headline figures first, then the detail rows.
  - The answer schema makes the model mark each figure `headline` (a number the answer text
    states) or `detail` (a breakdown row, e.g. one per month).
  - The prompt keeps per-component and per-hub numbers out of the answer text, so a breakdown
    never appears twice.
  - **Monthly counts.** In one live run the Denver annual answer still listed seven months:
    `weather_stat` returned them next to the annual count, and a prompt rule alone didn't stop
    the model from writing them out. Now `weather_stat` takes `by_month`:
    - **`false`, an annual (one-year) question:** the model gets the totals only, so it
      can't list months. The app adds one `detail` row per month to the answer's Breakdown, e.g.
      "Denver, January 2025: daily snowfall > 0 cm (any snowfall)" = 11 days. The rows come from
      the same deterministic result, with no extra model call. The stored assistant message
      stays as the model wrote it.
    - **`true`, the user asked for a monthly breakdown** (or the answer needs month-level
      counts, e.g. "which month had the most snow"): the model gets the monthly counts and lists
      them in the answer text and as detail figures. The app adds nothing for those months,
      even if the same turn also made a `false` call, so nothing appears twice.
    - Full-history results (`year: null`) have no monthly counts either way.
  - Two eval checks enforce this: the main figure must be a headline, and the Denver answer may
    not list months. The monthly-request path is tested offline (scripted answers).
- **Atlas layout.**
  - **Look:** warm off-white paper, navy ink, a teal accent, and serif headings. Land is
    parchment on a pale sea, with a dashed 5° latitude/longitude grid clipped to the lower 48.
  - **Desktop:** the map and chat get equal columns.
  - **Mobile:** one column with the chat first.
  - **Opening screen:** the chat shows only a question box under "What would you like to know
    about your hubs?" and three example questions. It switches to the usual conversation layout
    after the first question, and "New conversation" returns to it.
  - **Favicon:** a teal pin on a navy tile (`app/static/favicon.svg`, with PNG fallbacks and
    `/favicon.ico`).
  - **Light/dark switch:** in the header (`role="switch"`, with a sun or moon icon), and the
    choice is remembered in `localStorage`. Until you choose, the page follows the system
    setting, including live changes. A small script in `<head>` applies the theme before the
    first paint, so there's no flash.
- **Hub map.** A US map sits next to the chat on desktop, and below the chat on mobile.
  - **Ranking and map linked both ways:** hovering or focusing a city in the ranking
    highlights its marker and dims the others; hovering a marker highlights its ranking row.
  - **Selecting a hub** from either shows the full name ("Houston, Texas", from
    `/map/hubs` `state_name`) with one suggested question and a question box.
  - **Data:** the state outlines come from us-atlas 3.0.1 (`states-albers-10m.json`, ISC licence,
    (c) Michael Bostock, from US Census Bureau 2017 cartographic boundaries). The file is
    integrity-checked and converted to SVG paths by `scripts/build_us_map.py`, then committed, so
    the app stays offline.
  - **Placement:** `app/geo.py` places hubs with the same d3 Albers USA projection the atlas was
    built with. Tests check it against d3's own output and confirm that every hub lands inside
    its own state.
  - **Markers:** coloured by tier, and keyboard-accessible.
  - **Hub card:** shows county, region, score, tier and rank. A typed question that doesn't
    name the city is sent as "About the Denver, Colorado hub: …", and the chat shows exactly
    what was sent.
  - **Labels:** city labels stay at about 15–17 px on screen at every width, with a paper halo.
    They are sized from the rendered map width, and their placement is recomputed on resize,
    so they never overlap or leave the map. A browser check sweeps 320–1600 px.
- **Accessibility and robustness** (from an independent review; each fix has a browser
  regression check):
  - **Contrast:** tier words use darker text tokens that reach WCAG AA (≥ 4.5:1) in both themes.
  - **Focus:** focus rings are visible. Focus order follows the layout (ranking, then the hub
    card), and pressing Enter on a hub moves to its question.
  - **Requests:** all ask controls are disabled while a request runs. "New conversation"
    cancels a request in flight and drops its late answer and session. Error responses (422,
    proxy pages, network failures) are shown as readable messages.
  - **Phones:** the conversation is a fixed-height scroll box, so the map never gets pushed
    down. Tapping an example doesn't open the keyboard.
  - **Map failure:** if the map can't load, the atlas collapses to a short notice.

## 4. Data sources
| Source | What we use | Why |
|---|---|---|
| Open-Meteo Historical Weather API (default Best Match model selection: gridded estimates for the area around each hub) | Daily snowfall, precipitation, Tmin, Tmax, max gust per hub, 2020-01-01 to 2025-12-31 | Free, no key, consistent across hubs. Answers "% of days with snowfall" directly |
| FEMA National Risk Index, county level (Dec 2025 release) | Annual event frequency (`*_AFREQ`) for 9 hazards per hub county, plus the national distribution of each | An authoritative long-record view of rare hazards (hurricanes) that 6 years of weather can't give |

**Findings that shaped the design**
- **FEMA risk scores saturate.** Miami-Dade and Harris both score about 100 on hurricane risk
  and inland flood risk, so those scores can't separate the hubs.
- **FEMA risk and loss figures measure the county, not our hub.** They scale with the dollar
  value of everything exposed in the county. For example, Harris's hurricane building exposure
  is $839B against Miami-Dade's $359B.
- **Decision:** the model uses **frequencies** only. Ratings appear as context.
- **Coastal flooding:** the risk score and the frequency disagree. Houston has the higher
  frequency (3.76/yr vs 2.25/yr), but Miami has the higher risk score (99.6 vs 83.2). FEMA's
  coastal-flood event count is empty, so this frequency is a modelled estimate.
- **Inland flooding:** FEMA renamed riverine flooding (`RFLD`) to inland flooding (`IFLD`) in
  this release, and the code uses the new name.
- **Gridded estimates can differ from station records.** Open-Meteo's Denver snowfall estimates are
  58–110 cm/yr for 2020–2025. The request sends no `models` parameter, so Open-Meteo picks its
  default Best Match model selection, and no specific model or grid resolution is claimed.
- **Snowfall rule:**
  - The default is any snowfall: **> 0 cm**. That gives Denver 2025 = 43 of 365 days (11.8%).
  - A positive threshold X means **at least X cm (>= X)**, so a day with exactly X cm counts.
    At least 1 cm gives 17 days.
  - A **negative snowfall threshold is rejected**: `weather_stat` returns an error and counts
    nothing, and `GET /hubs/{id}/weather` answers 400.
  - Other metrics keep their own comparisons (`hard_freeze` is <= and accepts −12 °C).
  - The agent quotes the tool's `condition` text (e.g. "daily snowfall >= 1 cm (at least 1 cm)")
    in the answer and the figure labels, and passes the user's threshold exactly. The
    `denver_snow_threshold` eval fails if any call used another value, such as 0.99.

## 5. Risk model (`app/scoring.py`, `config.yaml`)
- **Components per hazard:**
  - Winter: snow days, hard-freeze days, and FEMA frequencies for winter weather, ice storm
    and cold wave.
  - Flood: heavy-rain days, and FEMA frequencies for inland and coastal flooding.
  - Hurricane: FEMA hurricane frequency only.
  - Wind: high-gust days, and FEMA frequencies for tornado and strong wind.
  - Heat: extreme-heat days, and FEMA heat-wave frequency.
- **Normalisation:** each component is scaled linearly to 0–100 against a fixed cap, set by rule
  before looking at any hub's result:
  - Observed day counts: the cap is 36.5 days a year (10% of days).
  - FEMA frequencies: the cap is the **99th percentile of that field across all 3,232 US
    counties**, computed from FEMA's national table, never from our hubs.
- **Why not min-max across hubs:** min-max would let adding or removing a hub change everyone
  else's score, and would make any future "score changed" alerts meaningless.
- **Scores:**
  - Hazard score = mean of its available components.
  - Overall = weighted sum: winter .25, flood .25, hurricane .20, wind .15, heat .15.
  - Tiers: Low <35, Moderate 35–55, High 55–75, Severe ≥75.
- **Explainability:** `ScoreBreakdown` lists every component's raw value, cap and normalised
  value, plus each hazard's weight and its contribution in points. "Why is X high" answers name
  the largest contributors.
- **Confidence:** `high` with both sources, `medium` if the observed weather is missing,
  `low` if the FEMA record is missing.
- **No tuning toward expected results:** nothing was adjusted to produce a particular ranking.
  The unit tests use synthetic inputs, and the eval expectations are computed from the model at
  run time.

## 6. Data storage choice: SQLite plus a committed raw JSON cache
- **The data is small and relational.** It is about 15k daily rows, 7 FEMA records, 9 caps and
  the chat history. SQLite handles it with zero operations work, ships as a single file, needs no
  server for the interview demo, and supports SQL for any future analysis.
- **The raw responses are the source of truth.** They live in `data/cache/` and are committed, and
  the DB is rebuilt from them. This makes results reproducible and auditable (you can diff the raw
  data), and it decouples the demo from API quotas. During the build, Open-Meteo's per-IP daily
  quota on the shared sandbox IP blocked fetches several times.
- **Chat history uses the same DB**, keyed by `session_id`.
- **When to move to Postgres:** once several app instances or many concurrent users share one
  database. The only module that touches storage is `store.py`.

## 7. Repository structure
```
app/
  main.py          FastAPI routes; serves the UI
  access.py        reviewer access code check for /chat
  agent.py         Claude tool-use loop, structured output, history
  tools.py         tool schemas (strict) + dispatch to deterministic code
  scoring.py       risk model (pure compute_breakdown + DB-backed wrappers)
  metrics.py       observed weather day counts / percentages
  schemas.py       Pydantic models + the AgentAnswer JSON schema
  store.py         SQLite schema, cache → DB build, reads, chat history
  config.py        loads config.yaml, data/hubs.json and the optional .env
  geo.py           Albers USA projection that places hubs on the map
  sources/         open_meteo.py, fema_nri.py (API clients → data/cache)
  static/index.html    chat UI and hub map (no build step)
  static/us-states.json  state outlines (SVG paths), built by scripts/build_us_map.py
data/hubs.json, data/cache/*.json, config.yaml
scripts/refresh_data.py, scripts/build_us_map.py, scripts/export_transcript.py
scripts/score_changes.py   before/after scores for the score-change workflow
.github/workflows/score-changes.yml   opens an issue when a score changes on main
evals/cases.yaml, evals/run.py
tests/  scoring, agent loop, model fallback, .env, map and breakdown, tool limit,
        monthly breakdown, access code, score changes
docs/DESIGN.md, docs/session-transcript.md
```

### Score-change monitoring
Scores are a pure function of committed files. The "Score changes" workflow therefore
re-scores the commit before and after each push to `main`, each in its own checkout, and opens
an issue when an overall score changes. A tier change is highlighted. A hidden marker for the
commit pair prevents duplicate issues on re-runs. The run is skipped only when there is no
earlier app to score, and it fails if an existing version can't be scored. See the README.

## 8. Evaluation
`evals/cases.yaml` has 10 cases:
- the 4 assignment questions
- a two-turn follow-up
- a different snow threshold
- the full ranking
- a methodology question
- an unknown hub (Seattle)
- an out-of-scope hazard (wildfire)

Per-case checks:
- the required tools were called
- `in_scope` is correct
- the expected hubs are mentioned
- the ranking order matches the model's
- **required figures:** `answer.figures` must contain the number within tolerance of
  `metrics.weather_stat`, **with the right unit** (`%` or `days`).
  - "11.8 days" fails where 11.8% is expected.
  - A missing figure, a figure only in the text, or an expected value that can't be computed
    fails the case.
  - **The answer text counts too:** stating the number with another unit ("11.8 days") fails,
    and a headline figure must appear in the text with the right unit.
  - **Units** are classified by their leading word: "% (43 of 365 days)" is a percentage, while
    "days (4.7% of days)" and "of 365 days" are days. "percentage points" and "mm/day" are
    neither.

Always checked:
- the answer validates against the schema
- every cited score matches the model
- in-scope answers list assumptions

`python -m evals.run --dry-run` shows the expected values without calling the LLM.
`pytest` covers the scoring maths and the agent loop (with a fake client).

## 9. Assumptions, uncertainty, and next steps
- **Assumptions:**
  - The hazard weights are an operational judgement, and they are configurable.
  - The 10%-of-days cap and the national 99th-percentile cap are rules, not fitted values.
  - A null FEMA frequency means the hazard doesn't apply (scored as 0).
  - US Census regions are used for "Midwest", "South" and "West".
- **Uncertainty:**
  - Open-Meteo values are gridded estimates for the area around each hub, and can differ from
    station records.
  - FEMA data describes the whole county.
  - Six years of observed weather is a short climate sample.
  - Exposure is not the same as vulnerability: building condition, backup power and the
    hub's own network role are not modelled.
- **Next steps:**
  - Alerts: score changes on `main` already open a GitHub issue (the "Score changes"
    workflow). Next: notify a webhook or chat channel on a tier change, and a scheduled job that
    re-fetches the upstream data (Open-Meteo revisions, new FEMA NRI releases) and reports
    changes before anyone commits them. Fixed caps make the diffs meaningful.
  - Live NWS alerts as a separate overlay.
  - Station data (NOAA GHCN-Daily), to measure where the gridded estimates can differ from
    station records.
  - Hub-specific vulnerability and cost data, to turn exposure into expected disruption cost.
