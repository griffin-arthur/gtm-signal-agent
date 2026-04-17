# Arthur Tracing Setup

The signal agent exports OpenTelemetry traces to the Arthur Engine so every
pipeline run is visible in Arthur's Trace Viewer. Spans include:

- **Anthropic LLM calls** — prompt, completion, token counts, model, latency
  (auto-instrumented via `openinference-instrumentation-anthropic`)
- **HTTPX calls** to Greenhouse / Lever / Ashby / Workday / Google News / SEC /
  HubSpot (auto-instrumented via `opentelemetry-instrumentation-httpx`)
- **Pipeline-stage spans** — one per signal: `ingest_company`, `process_signal`,
  `suppression_check`, `competitor_customer_check`, `llm_validation`,
  `score_and_decide`, `account_resolution`, `slack_post`, `hubspot_write`

One trace = one signal's journey through the pipeline. Filter by
`signal_agent.company_name` or `signal_agent.outcome` in the Arthur UI.

## Setup

### 1. Get your keys from the Arthur Engine UI

- **API key**: `Model Management → API Key` (Bearer token)
- **Task ID**: same page, shown per Agentic Model

### 2. Fill in `.env`

```
ARTHUR_TRACING_ENABLED=1
ARTHUR_ENGINE_BASE_URL=http://localhost:3030        # your Engine's URL
ARTHUR_ENGINE_API_KEY=<paste>
ARTHUR_TASK_ID=<paste>
ARTHUR_SERVICE_NAME=signal-agent                    # shows up in UI
```

To disable tracing temporarily, set `ARTHUR_TRACING_ENABLED=0`. The pipeline
works either way — no-op tracing falls back to OpenTelemetry's default.

### 3. Run the pipeline

```bash
.venv/bin/python -m scripts.run_pipeline
```

Watch the first run's log output for:

```
2026-04-17 15:41:12 [info] arthur_tracing.initialized  endpoint=http://localhost:3030/v1/traces  task_id=…  service_name=signal-agent
```

If you see `arthur_tracing.skipped_missing_env`, one of the env vars is empty.
If you see `arthur_tracing.disabled`, `ARTHUR_TRACING_ENABLED=0`.

### 4. Confirm traces in Arthur

Open your Arthur Engine UI → Trace Viewer tab. Each run shows:

- Parent span `signal_agent.process_signal` per signal
- Child spans for each stage
- Auto-instrumented Anthropic LLM spans under `llm_validation`
- HTTP spans under `ingest_company` and `account_resolution` / `hubspot_write`

## Troubleshooting

### "No traces showing up"

1. Check the base URL — `curl -i $ARTHUR_ENGINE_BASE_URL/v1/traces` should return
   something (401 is fine — it means the endpoint exists).
2. Check the API key — the Engine rejects with 401 if Bearer token is wrong.
   Our exporter logs via `opentelemetry` stderr; raise verbosity with:
   ```
   LOG_LEVEL=DEBUG .venv/bin/python -m scripts.run_pipeline
   ```
3. Check the task ID — if wrong, the Engine may accept the traces but file them
   under the wrong model. Verify the value matches exactly what the UI shows.
4. BatchSpanProcessor buffers — if the script exits before flush, spans are
   dropped. `run_pipeline.py` calls `tracing.shutdown()` at the end; make sure
   your own scripts do too if you invoke the pipeline programmatically.

### "Too many HTTP spans cluttering the view"

The httpx instrumentor traces every outbound HTTP call. To silence health
checks or HubSpot polling spans, add a sampler configuration to `tracing.py`
or filter by `http.target` in the Arthur UI.

### "LLM span doesn't show prompt/completion"

OpenInference's Anthropic instrumentor records both by default. If they're
missing, check the Anthropic SDK version — `>= 0.39` is required.

## Architecture notes

- `signal_agent/observability/tracing.py` is the single initialization point.
  It's imported at the top of `signal_agent/api/main.py` and
  `scripts/run_pipeline.py` **before** anything else that could instantiate an
  Anthropic or httpx client. Order matters — instrumentors wrap at import time.
- The span resource includes `arthur.task=<TASK_ID>` and `service.name=…` so
  Arthur's UI can route traces correctly.
- `BatchSpanProcessor` flushes asynchronously. For a long-running service
  (FastAPI), that's fine. For short CLI scripts, always call
  `tracing.shutdown()` before exit.
- Every `stage_span("name", ...)` prefixes attribute keys with `signal_agent.`
  so they don't collide with OpenTelemetry or OpenInference standard attrs.

## Future work

- **Evaluator integration**: the Anthropic instrumentor is OpenInference-shaped,
  so Arthur's evaluators can run automatically against the captured traces
  once a Task is set up.
- **Custom evaluator for our decision engine**: add spans with the alert
  decision reason and score delta so we can track "did this cooldown decision
  suppress a signal that later converted?" — needs Phase 4 attribution data.
