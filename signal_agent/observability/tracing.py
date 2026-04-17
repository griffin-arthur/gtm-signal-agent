"""Arthur tracing setup.

Exports OpenTelemetry traces to the Arthur Engine's OTLP HTTP endpoint. Covers:

 - **Anthropic LLM calls** via `openinference-instrumentation-anthropic`.
   Captures prompt, completion, token counts, model, latency — these are the
   spans Arthur's Trace Viewer uses for eval / governance insight.
 - **HTTPX ingestor calls** via the standard OTel httpx instrumentor.
   Gives per-source latency + failure visibility (Greenhouse/Lever/news/SEC).
 - **Custom pipeline spans** via `get_tracer()` used in `alert_pipeline.py`
   and `scripts/run_pipeline.py` to wrap each stage (suppression → validate →
   score → fire). These show up as parent spans with LLM/HTTP calls nested.

### Setup

`initialize()` is called once at process start (from FastAPI app startup and
from the CLI runner). It's a no-op when `ARTHUR_TRACING_ENABLED=0` or when
any required env var is missing, so the pipeline still works locally without
an Arthur Engine attached.

### Authentication

Arthur Engine accepts OTLP/HTTP with a Bearer token in `Authorization`. We
also include the `arthur.task` attribute on the Resource so every exported
span is associated with the correct Agentic Model in the Arthur UI.

### Span attributes we add

Each custom span gets:
 - `signal_agent.stage` — name of the pipeline stage
 - `signal_agent.signal_id` — DB id of the signal being processed
 - `signal_agent.company_id` / `company_name` / `target_tier`
 - `signal_agent.outcome` — set on span close (alerted, rejected, etc.)
This makes it easy to filter / group traces by account or stage in Arthur.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

import structlog

from signal_agent.config import settings

log = structlog.get_logger()

# Module-level state. Set on first `initialize()` call; readers should use
# `get_tracer()` rather than touching these directly.
_initialized = False
_tracer: Any = None


def initialize() -> None:
    """Set up the tracer provider + exporters. Idempotent, safe to call twice."""
    global _initialized, _tracer

    if _initialized:
        return

    # Even when tracing is disabled, expose a tracer so call sites can use
    # `with tracer.start_as_current_span(...)` without special-casing.
    # OpenTelemetry's default no-op tracer is a drop-in replacement.
    if not settings.arthur_tracing_enabled:
        log.info("arthur_tracing.disabled")
        _install_noop()
        return

    missing = [
        name for name, val in {
            "ARTHUR_ENGINE_API_KEY": settings.arthur_engine_api_key,
            "ARTHUR_TASK_ID": settings.arthur_task_id,
            "ARTHUR_ENGINE_BASE_URL": settings.arthur_engine_base_url,
        }.items() if not val
    ]
    if missing:
        log.warning("arthur_tracing.skipped_missing_env", missing=missing)
        _install_noop()
        return

    # Lazy imports so that environments without the OTel SDK still import the
    # module (e.g., tests that monkey-patch but don't export).
    from openinference.instrumentation.anthropic import AnthropicInstrumentor
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Silence the OTel SDK's own noisy INFO logs (exporter errors will still
    # surface as WARN/ERROR).
    logging.getLogger("opentelemetry").setLevel(logging.WARNING)

    exporter = OTLPSpanExporter(
        endpoint=f"{settings.arthur_engine_base_url.rstrip('/')}/v1/traces",
        headers={"Authorization": f"Bearer {settings.arthur_engine_api_key}"},
    )

    resource = Resource.create({
        # Arthur requires the task attribute to route traces to the right model.
        "arthur.task": settings.arthur_task_id,
        "service.name": settings.arthur_service_name,
        "service.version": "0.1.0",
    })

    provider = TracerProvider(resource=resource)
    # BatchSpanProcessor is more efficient than Simple — it queues and flushes
    # in a background thread. SimpleSpanProcessor is better for debugging but
    # serializes each span individually.
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Auto-instrument the Anthropic SDK + httpx clients. Must happen AFTER
    # the TracerProvider is set globally, otherwise the instrumentors will
    # attach to the no-op provider.
    AnthropicInstrumentor().instrument(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)

    _tracer = trace.get_tracer("signal_agent")
    _initialized = True

    log.info(
        "arthur_tracing.initialized",
        endpoint=f"{settings.arthur_engine_base_url}/v1/traces",
        task_id=settings.arthur_task_id,
        service_name=settings.arthur_service_name,
    )


def _install_noop() -> None:
    """Install a no-op tracer so call sites work even when tracing is off."""
    global _initialized, _tracer
    from opentelemetry import trace
    _tracer = trace.get_tracer("signal_agent.noop")
    _initialized = True


def get_tracer() -> Any:
    """Return the process-wide tracer. Call `initialize()` first."""
    if _tracer is None:
        initialize()
    return _tracer


@contextmanager
def stage_span(stage: str, **attrs: Any) -> Iterator[Any]:
    """Context manager for a pipeline-stage span.

    Example:
        with stage_span("llm_validation", signal_id=42, company_name="Stripe"):
            result = validate_signal(...)
            span = trace.get_current_span()
            span.set_attribute("signal_agent.outcome", result.is_valid)

    Keys are prefixed with `signal_agent.` for filtering in the Arthur UI.
    Values that aren't primitive OTel-acceptable types are coerced to str.
    """
    tracer = get_tracer()
    safe_attrs = {
        f"signal_agent.{k}": _coerce_attr(v) for k, v in attrs.items()
    }
    with tracer.start_as_current_span(f"signal_agent.{stage}", attributes=safe_attrs) as span:
        yield span


def _coerce_attr(v: Any) -> Any:
    """OTel span attributes only accept str/bool/int/float/sequences. Coerce."""
    if isinstance(v, (str, bool, int, float)) or v is None:
        return v
    return str(v)


def shutdown() -> None:
    """Flush pending spans. Call on graceful shutdown."""
    if not _initialized or not settings.arthur_tracing_enabled:
        return
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception as e:
        log.warning("arthur_tracing.shutdown_failed", err=str(e))
