"""Scheduled digest flusher.

Runs every DIGEST_FLUSH_INTERVAL_MINUTES and posts a single grouped Slack
message for any accumulated non-Tier-1 alerts. No-op when the queue is empty.
"""
from __future__ import annotations

import inngest
import structlog

from signal_agent.config import settings
from signal_agent.db import session_scope
from signal_agent.integrations.slack import SlackAlerter
from signal_agent.quality import digest
from signal_agent.workflows.inngest_app import inngest_client

log = structlog.get_logger()


@inngest_client.create_function(
    fn_id="flush_digest",
    trigger=[
        # `*/N * * * *` runs every N minutes. Inngest validates the expression.
        inngest.TriggerCron(cron=f"*/{settings.digest_flush_interval_minutes} * * * *"),
        inngest.TriggerEvent(event="digest.flush.requested"),  # manual kick
    ],
)
async def flush_digest(ctx: inngest.Context) -> dict:
    def _run() -> dict:
        with session_scope() as s:
            return digest.flush_pending(s, SlackAlerter())

    return await ctx.step.run("flush", _run)
