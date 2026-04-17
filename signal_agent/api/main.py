"""FastAPI app — mounts Inngest + Slack interactivity + health check."""
from __future__ import annotations

import inngest.fast_api
import structlog
from fastapi import FastAPI

# Initialize Arthur tracing BEFORE importing anything that uses Anthropic or
# httpx — the instrumentors need to wrap those libraries at import time.
from signal_agent.observability import tracing as _tracing
_tracing.initialize()

from signal_agent.api import slack_interactivity  # noqa: E402
from signal_agent.workflows.inngest_app import all_functions, inngest_client  # noqa: E402

log = structlog.get_logger()

app = FastAPI(title="Arthur Signal Agent")


@app.on_event("shutdown")
async def _shutdown_tracing() -> None:
    _tracing.shutdown()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


# Inngest mounts /api/inngest by default; serve directly at /inngest for clarity.
inngest.fast_api.serve(
    app,
    inngest_client,
    all_functions(),
    serve_path="/inngest",
)

app.include_router(slack_interactivity.router)
