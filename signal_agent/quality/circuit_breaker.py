"""Alert-rate circuit breaker.

If more than CIRCUIT_BREAKER_ALERTS_PER_HOUR alerts fire in a 60-minute window,
pause alerting and notify the owner. Resumes automatically when the rolling
count drops below the threshold.

This is intentionally simple: each alert-fire call checks the DB for the last
hour's count. At larger scale we'd move this to Redis, but Phase 1 alert volume
is tiny.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from signal_agent.config import settings
from signal_agent.models import Alert, CircuitBreakerEvent


def is_tripped(session: Session) -> bool:
    """Return True if alerts should currently be paused."""
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    count = session.execute(
        select(func.count(Alert.id)).where(Alert.fired_at >= one_hour_ago)
    ).scalar_one()
    return count >= settings.circuit_breaker_alerts_per_hour


def record_trip(session: Session, count: int) -> CircuitBreakerEvent:
    ev = CircuitBreakerEvent(alert_count=count)
    session.add(ev)
    session.flush()
    return ev
