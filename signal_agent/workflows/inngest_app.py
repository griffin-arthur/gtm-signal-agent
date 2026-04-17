"""Inngest client + function registry.

All Inngest functions are defined in sibling modules and imported here. The
FastAPI app in `api/main.py` mounts this registry under `/inngest`.
"""
from __future__ import annotations

import inngest

from signal_agent.config import settings

inngest_client = inngest.Inngest(
    app_id=settings.inngest_app_id,
    is_production=not settings.inngest_dev,
)


def all_functions() -> list:
    # Import lazily so import-time side effects don't trip during tests.
    from signal_agent.workflows.alert_pipeline import process_signal
    from signal_agent.workflows.digest_flush import flush_digest
    from signal_agent.workflows.jobs_daily import ingest_jobs_daily, ingest_jobs_for_company

    return [ingest_jobs_daily, ingest_jobs_for_company, process_signal, flush_digest]
