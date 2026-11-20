# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which distribution hubs are most exposed to
weather disruption. A **deterministic risk model** (Python) scores 7 hubs from public data.
**Claude** answers questions by calling that model through tools, and returns a
**JSON-schema-validated** answer that the chat UI renders.

- Design and architecture: [`docs/DESIGN.md`](docs/DESIGN.md)
- AI session transcript (plan and full conversation): [`docs/session-transcript.md`](docs/session-transcript.md)

## Quick start

```bash
python -m pip install -r requirements.txt
```

```bash
export ANTHROPIC_API_KEY=...        # needed for /chat and the evals only
```

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

## Tests and evaluation

```bash
python -m pytest -q                       # scoring + agent-loop tests, no API key needed
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
| POST | `/chat` | `{message, session_id?}` → `{session_id, answer (AgentAnswer), tool_calls, model, usage}` |
| GET | `/rankings?hazard=&region=` | Deterministic ranking (overall or one hazard) |
| GET | `/hubs/{id}/score` | Full score breakdown for one hub |
| GET | `/hubs/{id}/weather?metric=&year=&threshold=` | Day counts from observed weather |
| GET | `/methodology` | Weights, caps, thresholds, sources |
| GET | `/hubs`, `/health` | Hub list; data and config status |

## Docker

```bash
docker build -t weather-risk .
```

```bash
docker run -p 8000:8000 -e ANTHROPIC_API_KEY weather-risk
```

## Configuration

Everything that affects a score lives in `config.yaml`, with the reason for each value: thresholds,
normalisation caps, hazard components, weights, tiers and the LLM model and effort.
