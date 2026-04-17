"""Slack integration — alert rendering + posting.

Block Kit shape from brief section 7:
 - header: company + score + primary signal type
 - "why this matters" sentence (LLM-generated)
 - 2–3 top signals as context with source links
 - HubSpot record link + account owner (if any)
 - Action buttons: Claim / Snooze 30 days

Interactivity (button clicks) is handled in api/slack_interactivity.py — this
module only renders and sends.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from signal_agent.config import settings

log = structlog.get_logger()


@dataclass
class AlertContext:
    company_name: str
    company_domain: str
    cumulative_score: float
    tier: str
    summary_for_ae: str
    top_signals: list[dict[str, Any]]  # [{"type": ..., "url": ..., "text": ...}, ...]
    hubspot_url: str | None
    owner_name: str | None
    deal_stage: str | None
    alert_id: int


class SlackAlerter:
    def __init__(self, client: WebClient | None = None) -> None:
        self._client = client or WebClient(token=settings.slack_bot_token)

    def post_alert(self, ctx: AlertContext, channel: str | None = None) -> str | None:
        """Post a formatted alert. Returns the message ts (for threading acks)."""
        channel = channel or settings.slack_alert_channel
        blocks = self._build_blocks(ctx)
        try:
            resp = self._client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text=f"Signal alert: {ctx.company_name} (score {ctx.cumulative_score})",
            )
            return resp["ts"]
        except SlackApiError as e:
            log.error("slack.post_failed", err=str(e))
            return None

    def post_raw_blocks(self, blocks: list[dict[str, Any]], fallback_text: str,
                        channel: str | None = None) -> str | None:
        """Post a pre-built Block Kit message. Used by digest flusher."""
        channel = channel or settings.slack_alert_channel
        try:
            resp = self._client.chat_postMessage(
                channel=channel, blocks=blocks, text=fallback_text,
            )
            return resp["ts"]
        except SlackApiError as e:
            log.error("slack.digest_post_failed", err=str(e))
            return None

    def post_thread_ack(self, channel: str, thread_ts: str, text: str) -> None:
        try:
            self._client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        except SlackApiError as e:
            log.warning("slack.thread_ack_failed", err=str(e))

    def notify_circuit_breaker(self, count: int) -> None:
        if not settings.slack_owner_user_id:
            return
        try:
            self._client.chat_postMessage(
                channel=settings.slack_owner_user_id,
                text=(
                    f":rotating_light: Signal agent circuit breaker tripped — "
                    f"{count} alerts in the last hour. Alerting paused. "
                    f"Likely a data source changed format. Check logs."
                ),
            )
        except SlackApiError as e:
            log.warning("slack.cb_notify_failed", err=str(e))

    # ---- block kit ------------------------------------------------------------

    def _build_blocks(self, ctx: AlertContext) -> list[dict[str, Any]]:
        tier_emoji = {"tier_1": ":red_circle:", "tier_2": ":large_orange_circle:",
                      "tier_3": ":large_yellow_circle:"}.get(ctx.tier, ":white_circle:")

        header_text = (
            f"{tier_emoji} *{ctx.company_name}* — score *{ctx.cumulative_score:.1f}* "
            f"({ctx.tier.replace('_', ' ').title()})"
        )

        signal_lines = []
        for s in ctx.top_signals[:3]:
            kind = s["type"].split(".")[-1].replace("_", " ").title()
            signal_lines.append(f"• *{kind}* — <{s['url']}|{s['text'][:80]}>")

        owner_line = (
            f"Owner: *{ctx.owner_name}*" if ctx.owner_name else "Owner: _unassigned_"
        )
        if ctx.deal_stage:
            owner_line += f"  ·  Stage: *{ctx.deal_stage}*"

        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"_{ctx.summary_for_ae}_"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(signal_lines)}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"{ctx.company_domain}  ·  {owner_line}"},
            ]},
        ]

        action_elements: list[dict[str, Any]] = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Claim"},
                "style": "primary",
                "action_id": "claim_alert",
                "value": str(ctx.alert_id),
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Snooze 30d"},
                "action_id": "snooze_alert",
                "value": str(ctx.alert_id),
            },
        ]
        if ctx.hubspot_url:
            action_elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": "Open in HubSpot"},
                "url": ctx.hubspot_url,
                "action_id": "open_hubspot",
            })
        blocks.append({"type": "actions", "elements": action_elements})

        return blocks
