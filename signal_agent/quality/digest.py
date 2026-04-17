"""Digest batching for high-volume periods.

Rules (from build brief section 7):
 - Tier 1 alerts always post immediately.
 - If ≥DIGEST_RATE_THRESHOLD alerts fired in the last hour, route non-Tier-1
   alerts to a pending_digest queue instead of posting to Slack.
 - A scheduled job flushes the queue every N minutes as a grouped Slack post.

This is the primary lever we have against AE channel fatigue during bursts
(e.g., a conference season where every ICP company posts an MLOps role).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from signal_agent.config import settings
from signal_agent.integrations.slack import SlackAlerter
from signal_agent.models import Alert, Company, DigestItem, Signal, SignalTier

log = structlog.get_logger()


def should_batch(session: Session, tier: SignalTier) -> bool:
    """True if this alert should be deferred to the next digest flush."""
    # Tier 1 always posts live.
    if tier == SignalTier.TIER_1:
        return False
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = session.execute(
        select(func.count(Alert.id)).where(Alert.fired_at >= one_hour_ago)
    ).scalar_one()
    return recent >= settings.digest_rate_threshold


def enqueue(session: Session, alert: Alert) -> None:
    """Record an alert as pending digest delivery."""
    session.add(DigestItem(alert_id=alert.id, company_id=alert.company_id))
    session.flush()


def flush_pending(session: Session, alerter: SlackAlerter | None = None) -> dict[str, Any]:
    """Post a single grouped Slack message for all unflushed digest items.

    Returns {"flushed": N, "companies": M, "ts": ...} for observability.
    Safe to call on an empty queue — returns {"flushed": 0}.
    """
    alerter = alerter or SlackAlerter()

    items = session.execute(
        select(DigestItem, Alert, Company, Signal)
        .join(Alert, Alert.id == DigestItem.alert_id)
        .join(Company, Company.id == DigestItem.company_id)
        .join(Signal, Signal.id == Alert.triggering_signal_id)
        .where(DigestItem.flushed_at.is_(None))
        .order_by(Alert.tier.asc(), Alert.cumulative_score.desc())
    ).all()

    if not items:
        return {"flushed": 0}

    # Group by company so one noisy account isn't spread across N lines.
    by_company: dict[int, list[tuple[DigestItem, Alert, Company, Signal]]] = {}
    for row in items:
        by_company.setdefault(row[2].id, []).append(row)

    blocks = _build_digest_blocks(by_company)
    ts = alerter.post_raw_blocks(blocks, fallback_text=f"Signal digest — {len(items)} alerts")

    now = datetime.now(timezone.utc)
    for di, _, _, _ in items:
        di.flushed_at = now

    log.info("digest.flushed", count=len(items), companies=len(by_company), slack_ts=ts)
    return {"flushed": len(items), "companies": len(by_company), "ts": ts}


def _build_digest_blocks(
    by_company: dict[int, list[tuple[DigestItem, Alert, Company, Signal]]],
) -> list[dict[str, Any]]:
    """Render a Block Kit message summarizing batched alerts."""
    total = sum(len(v) for v in by_company.values())
    header = {
        "type": "header",
        "text": {"type": "plain_text", "text": f"Signal digest — {total} alerts across {len(by_company)} companies"},
    }
    blocks: list[dict[str, Any]] = [header, {"type": "divider"}]

    tier_emoji = {
        SignalTier.TIER_1: ":red_circle:",
        SignalTier.TIER_2: ":large_orange_circle:",
        SignalTier.TIER_3: ":large_yellow_circle:",
    }

    for company_id, rows in by_company.items():
        company = rows[0][2]
        top_tier = min((a.tier for _, a, _, _ in rows), key=lambda t: t.value)
        max_score = max(a.cumulative_score for _, a, _, _ in rows)
        sample_signals = [s for _, _, _, s in rows[:3]]

        summary_lines = []
        for s in sample_signals:
            kind = s.signal_type.split(".")[-1].replace("_", " ").title()
            title = s.signal_text.split("\n", 1)[0][:90]
            summary_lines.append(f"• _{kind}_ — <{s.source_url}|{title}>")

        hubspot_url = (
            f"https://app.hubspot.com/contacts/_/company/{company.hubspot_id}"
            if company.hubspot_id else None
        )
        owner_line = (
            f"<{hubspot_url}|HubSpot record>" if hubspot_url else "_no HubSpot link_"
        )
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{tier_emoji[top_tier]} *{company.name}* "
                    f"— {len(rows)} alert(s), top score *{max_score:.1f}*  ·  {owner_line}\n"
                    + "\n".join(summary_lines)
                ),
            },
        })

    return blocks
