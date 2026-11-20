# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which distribution hubs are most exposed to
weather disruption. A **deterministic risk model** (Python) scores 7 hubs from public data.
**Claude** answers questions by calling that model through tools, and returns a
**JSON-schema-validated** answer that the chat UI renders.

The browser UI is a **weather atlas**: the hub map and the chat share the screen equally on
desktop, and on mobile the chat comes first.
- **Opening screen:** a question box under "What would you like to know about your hubs?" with
  three example questions. After the first question it switches to the conversation layout.
- **Answers:** each one shows its main result first, with the numbers in an expandable
  **Breakdown**.
  - For an annual question ("what percentage of days last year…"), the monthly counts appear
    only in the Breakdown.
  - Ask for a monthly breakdown ("break that down by month") and the months appear in the
    answer too.
- **Ranking and map:** hovering a city in the ranking highlights it on the map. Selecting it
  shows the full name ("Houston, Texas"), its score and tier, and a question you can ask
  about that hub.

The agent runs on **Claude Sonnet 5.5**. If Sonnet is temporarily unavailable, the same request is
tried once on **Claude Haiku 5.5**, and every answer (UI, `/chat`, eval results) says which model
actually answered.

- Design and architecture: [`docs/DESIGN.md`](docs/DESIGN.md)
- AI session transcript (plan and full conversation): [`docs/session-transcript.md`](docs/session-transcript.md)

## Quick start

```bash
python -m pip install -r requirements.txt
```

Put your key in a `.env` file in the repo root (git-ignored; see `.env.example`). It is needed
only for `/chat` and the evals:

```bash
cp .env.example .env    # then edit: ANTHROPIC_API_KEY=sk-ant-...
```

A variable already set in the environment takes precedence over `.env`.

```bash
python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and click one of the example questions.

The public data is already cached in `data/cache/` (raw API responses). The SQLite database
is rebuilt from that cache at startup, so the app runs offline and doesn't depend on API quotas.
To re-download the data:

```bash
python -m scripts.refresh_data            # fetch anything missing, rebuild DB
```

```bash
python -m scripts.refresh_data --force    # re-download everything
```

## Windows (PowerShell 5.1 or 7)

Needs Git and Python 3.10+.

```powershell
git clone -b claude/lucid-thompson-42z611 https://github.com/BarHaham/weather-risk-intelligence-agent.git
cd weather-risk-intelligence-agent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

If `py` isn't found, use `python -m venv .venv` instead. The `pytest` step needs no API key.

Put the key in `.env` in the repo root. Both the app and the evals read it, so every window works
without setting the key again. Create it with `notepad .env` (Notepad offers to create the file)
and add one line: `ANTHROPIC_API_KEY=sk-ant-...`. Any encoding works, including the UTF-16 that
PowerShell 5.1's `>` writes. If the file ends up named `.env.txt`, the app warns you to rename it.

Alternative: set the key for one window only with a prompt, so it isn't saved in PowerShell's
history file. It then takes precedence over `.env`:
`$env:ANTHROPIC_API_KEY = [Net.NetworkCredential]::new('', (Read-Host 'Anthropic API key' -AsSecureString)).Password`

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000. `http://127.0.0.1:8000/health` shows `"api_key_source": ".env"` once the
key has been read. Evals call Claude directly, not through the server, from any window `cd`'d into
the clone:

```powershell
.\.venv\Scripts\python.exe -m evals.run --dry-run
.\.venv\Scripts\python.exe -m evals.run --case follow_up_detroit
.\.venv\Scripts\python.exe -m evals.run
```

Each eval line shows `answered_by=<model>`. When the fallback was used, it adds
`[fallback from claude-sonnet-5-5: <reason>]`. The summary counts answers per model.

### Try the fallback with a simulated Sonnet outage

`WRIA_SIMULATE_OUTAGE` makes the listed models fail with a synthetic 529 "overloaded" error before
any request is sent. Haiku is then called for real, so this costs Haiku tokens only:

```powershell
$env:WRIA_SIMULATE_OUTAGE = "claude-sonnet-5-5"
.\.venv\Scripts\python.exe -m evals.run --case denver_snow_last_year
Remove-Item Env:WRIA_SIMULATE_OUTAGE
```

Expect `answered_by=claude-haiku-5-5 [fallback from claude-sonnet-5-5: OverloadedError 529
overloaded_error: Simulated outage ... [simulated]]`. To see what happens when both models are
down, use `$env:WRIA_SIMULATE_OUTAGE = "claude-sonnet-5-5,claude-haiku-5-5"`. The case then fails
and the report names both errors. Don't put this variable in `.env`, or every run silently goes to
Haiku.

## Model fallback

- **Every turn starts on `llm.model`** (`claude-sonnet-5-5`), with `llm.max_retries` SDK retries.
- **What triggers the fallback:** the primary is temporarily unavailable. That means overloaded
  (529), another 5xx except 501, a rate limit (429), a 408, a stalled stream (no data for
  `llm.stream_idle_timeout_s`; a slow but healthy answer doesn't count) or a connection error.
- **The fallback attempt:** the same request is sent **once** to `llm.fallback_model`
  (`claude-haiku-5-5`), as a single attempt with no retries. The rest of that turn stays on Haiku.
  The next turn starts on Sonnet again.
- **If Haiku fails too:** both actual errors are reported (class, HTTP status, the API's error type
  and message), so a permissions problem shows up as e.g. `PermissionDeniedError 403
  permission_error: ...` rather than as an outage. `/chat` returns **503** when Haiku was also
  unavailable, and **502** otherwise.
- **Errors that never trigger the fallback:** 400, 401, 403, 404 and 501 are problems with the
  request or the key, so they are raised directly.
- **Refusals** are not retried on another model. The answer says the request can't be handled.

## Tool-round limit

A turn may use up to `llm.max_tool_rounds` (8) rounds of tool calls.
- **Repeated calls:** a call that repeats an earlier one in the same turn (same tool, same input)
  isn't run again. Its result points to the earlier one.
- **After the 8th round:** Claude gets one more request with tools turned off and is told to
  answer from the data it has already gathered. If that reply's JSON is invalid, one repair
  request follows, also with tools off.
- **What you see:** such an answer shows "Reached the 8-round tool limit …". If even that reply
  isn't a valid answer, the chat says so: it stopped at the limit, or the reply was cut off or
  refused. None of these is labelled "out of scope".
- **In the evals:** a case line adds the status when a turn didn't end with a normal answer
  (e.g. `status=answered_at_tool_limit (8 of 8 tool rounds, 6 repeated calls)`), and the number
  of repeated calls when there were any. The summary lists the cases that used all 8 rounds.
  `evals/results/latest.json` has each turn's status, rounds and tool calls under `turns`,
  including a turn that failed with an API error (`status: api_error`).

**Light/dark mode:** use the **Dark mode** switch in the header. The choice is remembered in
this browser; until you choose, the page follows your system setting.

**Chats from an earlier version:** an app update that changes the system prompt or tools stops
older conversations from continuing. The API rejects their saved reasoning ("Invalid signature").
The chat then shows a short message with a **Start a new conversation** button, and your question
is kept in the box.

## Tests and evaluation

```bash
python -m pytest -q                       # scoring, agent loop, simulated outages, .env; no API key needed
```

```bash
python -m evals.run --dry-run             # show the expected values computed from the model
```

```bash
python -m evals.run                       # 10 cases against the live agent (uses API tokens)
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/chat` | `{message, session_id?}` → `{session_id, answer (AgentAnswer), status, tool_rounds, max_tool_rounds, tool_calls, models, usage}`. `models` = `{requested, answered_by, calls, fallback}`. `status` = `answered`, `answered_at_tool_limit`, `tool_limit`, `cut_off`, `refused` or `invalid_answer`. When `REVIEWER_ACCESS_CODE` is set, send it in the `X-Access-Code` header: missing → 401 `access_code_required`, wrong → 401 `access_code_invalid`. On Render without a code: 503 `chat_disabled` (see [Deploying to Render](#deploying-to-render)) |
| GET | `/rankings?hazard=&region=` | Deterministic ranking (overall or one hazard) |
| GET | `/hubs/{id}/score` | Full score breakdown for one hub |
| GET | `/hubs/{id}/weather?metric=&year=&threshold=` | Day counts from Open-Meteo's gridded estimates. Snowfall: no threshold or 0 = any snowfall (> 0 cm); a positive threshold X = at least X cm (>= X); a negative snowfall threshold is rejected with 400 |
| GET | `/methodology` | Weights, caps, thresholds, sources |
| GET | `/map/hubs` | Hubs with map position (x/y on the 975x610 map), overall score, tier and rank |
| GET | `/static/us-states.json` | State outlines for the map |
| GET | `/hubs`, `/health` | Hub list; data, models, simulated outage, `api_key_source` (`.env` / `environment` / `missing`, never the key) and `chat_access` (`open` / `code_required` / `disabled`, never the code) |

## Docker

```bash
docker build -t weather-risk .
```

```bash
docker run -p 8000:8000 --env-file .env weather-risk
```

`.env` is excluded from the image (`.dockerignore`). Docker's `--env-file` needs UTF-8 and
`KEY=value` lines without quotes.

## Deploying to Render

`/chat` is the only endpoint that calls Claude. On a public URL it is protected by a
**reviewer access code**, which the server checks before a request reaches Claude. That
covers the browser UI and direct API calls alike.

| Where | `REVIEWER_ACCESS_CODE` | `/chat` |
|---|---|---|
| Anywhere | set | needs the code in the `X-Access-Code` header (401 without it or with a wrong one) |
| Render | not set | **off**: 503 `chat_disabled`, whatever is sent |
| Locally | not set | open, as before |

**Setting it up on Render:**
1. **Create a web service** from this repo. The Docker runtime uses the `Dockerfile`, which
   listens on `$PORT`.
2. **Add environment variables:** `ANTHROPIC_API_KEY`, and `REVIEWER_ACCESS_CODE` set to a long
   random value. One way to make one:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(18))"
   ```

   Use printable ASCII only (letters, digits and symbols): other characters can't be sent in an
   HTTP header, and the server log warns if the code has any. Failed attempts aren't
   rate-limited, so don't use a short or guessable code.
3. **Check the mode** at `https://<your-app>.onrender.com/health`: `"chat_access"` should be
   `"code_required"`. Before the code is set, it says `"disabled"`, which also confirms the app
   detected Render (from the `RENDER` / `RENDER_SERVICE_ID` variables Render sets). The server log
   has a line saying which mode is active, never the code.

**Using it:**
- **Reviewers:** on the first question, the chat asks for the code once, then sends the question.
  The code is kept for that browser tab only.
- **Direct API calls:**

  ```bash
  curl -X POST https://<your-app>.onrender.com/chat -H "Content-Type: application/json" -H "X-Access-Code: <code>" -d '{"message": "Rank all of our hubs by overall weather disruption risk."}'
  ```

**Locally:** no code is needed. To try the check, add `REVIEWER_ACCESS_CODE=...` to `.env`
and restart the server. The evals call the agent directly, not through `/chat`, so they never
need the code.

## Score-change issues (GitHub Action)

`.github/workflows/score-changes.yml` watches the branch Render deploys from (`main`).
- **What it compares:** on every push it re-scores the commit before the push and the pushed
  commit, each from its own committed data, `config.yaml` and scoring code. No API key or
  network is needed.
- **When it opens an issue:** a hub's **overall score** changed.
  - **The issue shows:**
    - the old and new score, the change, the tier and the rank
    - how far each hub now sits from the nearest tier cutoff
    - the hazard sub-scores that moved
  - **A tier change** (e.g. Dallas 34.9 Low → 35.0 Moderate) is highlighted at the top and
    labelled `tier-change`. Every score-change issue is labelled `score-change`.
  - **No duplicates:** re-running the same push never opens a second issue. Each issue carries a
    hidden marker for its commit pair, and existing issues, open or closed, are checked first.
- **When it skips:** only when there is no earlier version to score. That's when the push
  created the branch, or when the earlier commit has no app, like the first merge into the empty
  `main`. If an earlier version exists but its scores can't be calculated, the run **fails** and
  shows the error.
- **Another branch:** set the repository variable `DEPLOY_BRANCH` (Settings → Secrets and
  variables → Actions → Variables).
- **Try it without opening an issue:** Actions → Score changes → Run workflow. Set `before` to
  an older commit or branch and leave `dry_run` on; the result appears in the run's summary.
- **If opening the issue fails with 403:** allow write access under Settings → Actions → General
  → Workflow permissions.

The same comparison runs locally:

```bash
python scripts/score_changes.py baseline --before <commit> --out before.json
```

```bash
PYTHONPATH=. python scripts/score_changes.py snapshot --out after.json
```

```bash
python scripts/score_changes.py compare before.json after.json --before <commit> --after HEAD --branch main --repo BarHaham/weather-risk-intelligence-agent --title-file title.txt --body-file body.md
```

## Map data

State outlines come from [us-atlas](https://github.com/topojson/us-atlas) 3.0.1
(`states-albers-10m.json`, ISC licence, (c) Michael Bostock), derived from the US Census
Bureau's 2017 cartographic boundaries. `app/static/us-states.json` is committed. To rebuild it,
which downloads the package from the npm registry and checks its published sha512:

```bash
python -m scripts.build_us_map
```

## Configuration

Everything that affects a score lives in `config.yaml`, with the reason for each value: thresholds,
normalisation caps, hazard components, weights, tiers, and the LLM models, effort, timeout and
retries.
