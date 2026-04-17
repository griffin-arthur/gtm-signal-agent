"""Hard suppression check.

Runs before LLM validation — matches cheap and doesn't spend API budget on
known false positives (recruiting agencies, etc.). Operator-managed via the
`suppressions` table; seed list in `seeds/suppression.yaml`.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_agent.models import Suppression
from signal_agent.schemas import NormalizedSignal


def is_suppressed(session: Session, signal: NormalizedSignal) -> tuple[bool, str | None]:
    rules = session.execute(select(Suppression)).scalars().all()
    haystacks = {"signal_text": signal.signal_text.lower(), "company_name": signal.company_name.lower()}
    for rule in rules:
        hay = haystacks.get(rule.field, "")
        if rule.pattern.lower() in hay:
            return True, rule.reason
    return False, None
