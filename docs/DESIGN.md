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
- **Deferred:** voice input, and alerts when a score changes (see §9).

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
  - `in_scope`, `answer` (markdown)
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
- **Refusals and cut-offs:** `stop_reason` is checked for `refusal`, `max_tokens` and `pause_turn`.
  A refusal is not retried on another model: the earlier server-side refusal fallback was replaced
  by the availability fallback below. The refused (or cut-off) assistant message is not stored, so
  an empty turn or a dangling `tool_use` can't break later requests in the session.

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
- **Main result first.** Each answer shows its text first. Every number then sits in one
  collapsed **Breakdown** section: headline figures first, then the detail rows.
  - The answer schema makes the model mark each figure `headline` (a number the answer text
    states) or `detail` (a breakdown row, e.g. one per month).
  - The prompt keeps detail numbers out of the answer text, so a breakdown never appears twice.
  - Two eval checks enforce this: the main figure must be a headline, and the Denver answer may
    not list months.
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
  - **Labels:** city labels are larger (about 21 px on desktop, 15 px on phones), with a paper
    halo, and they move automatically so they never overlap.

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
evals/cases.yaml, evals/run.py
tests/  scoring, agent loop, model fallback, .env, map and breakdown
docs/DESIGN.md, docs/session-transcript.md
```

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
- cited figures are within tolerance of `metrics.weather_stat`

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
  - Alerts: a daily job recomputes the scores, diffs them against the last snapshot in SQLite,
    and posts to a webhook when a tier changes. Fixed caps make the diffs meaningful.
  - Live NWS alerts as a separate overlay.
  - Station data (NOAA GHCN-Daily), to measure where the gridded estimates can differ from
    station records.
  - Voice through the browser Web Speech API.
  - Hub-specific vulnerability and cost data, to turn exposure into expected disruption cost.
