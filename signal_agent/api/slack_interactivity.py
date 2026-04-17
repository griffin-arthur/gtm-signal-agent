"""Slack interactivity endpoint — handles Claim / Snooze button clicks.

Flow:
 - Slack POSTs an interactivity payload (form-encoded `payload=<json>`).
 - We verify the signature using SLACK_SIGNING_SECRET.
 - Parse which action (claim_alert / snooze_alert) and which alert_id.
 - Apply side effect: set Alert.claimed_by / set Company.snoozed_until.
 - Post a thread acknowledgment so the channel sees who took ownership.
 - Return 200 within 3s (Slack's deadline).

We verify the signature directly rather than pulling in `slack_bolt` — one fewer
dependency and it keeps the handler tiny.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from slack_sdk import WebClient

from signal_agent.config import settings
from signal_agent.db import session_scope
from signal_agent.models import Alert, Company

log = structlog.get_logger()
router = APIRouter()

SNOOZE_DAYS = 30
SIGNATURE_VERSION = "v0"
REPLAY_WINDOW_SECONDS = 60 * 5


def _verify_slack_signature(body: bytes, headers: dict[str, str]) -> bool:
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > REPLAY_WINDOW_SECONDS:
            return False
    except ValueError:
        return False
    base = f"{SIGNATURE_VERSION}:{ts}:".encode() + body
    computed = SIGNATURE_VERSION + "=" + hmac.new(
        settings.slack_signing_secret.encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, sig)


@router.post("/slack/interactivity")
async def slack_interactivity(request: Request) -> PlainTextResponse:
    body = await request.body()
    if settings.slack_signing_secret:
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not _verify_slack_signature(body, headers):
            raise HTTPException(status_code=401, detail="invalid signature")

    form = parse_qs(body.decode())
    payload_raw = form.get("payload", [None])[0]
    if not payload_raw:
        raise HTTPException(status_code=400, detail="missing payload")
    payload = json.loads(payload_raw)

    actions = payload.get("actions") or []
    if not actions:
        return PlainTextResponse("")

    action = actions[0]
    action_id = action["action_id"]
    alert_id = int(action.get("value", 0))
    user = payload.get("user") or {}
    user_id = user.get("id", "")
    user_name = user.get("username") or user.get("name") or user_id
    channel_id = (payload.get("channel") or {}).get("id", "")
    message_ts = (payload.get("message") or {}).get("ts", "")

    client = WebClient(token=settings.slack_bot_token)

    if action_id == "claim_alert":
        with session_scope() as s:
            alert = s.get(Alert, alert_id)
            if alert and not alert.claimed_by:
                alert.claimed_by = user_id
                alert.claimed_at = datetime.now(timezone.utc)
        text = f":white_check_mark: Claimed by <@{user_id}>."
    elif action_id == "snooze_alert":
        with session_scope() as s:
            alert = s.get(Alert, alert_id)
            if alert:
                company = s.get(Company, alert.company_id)
                if company:
                    company.snoozed_until = datetime.now(timezone.utc) + timedelta(days=SNOOZE_DAYS)
        text = f":sleeping: Snoozed for {SNOOZE_DAYS} days by <@{user_id}>."
    else:
        text = ""

    if text and channel_id and message_ts:
        try:
            client.chat_postMessage(channel=channel_id, thread_ts=message_ts, text=text)
        except Exception as e:
            log.warning("slack.interactivity.ack_failed", err=str(e))

    log.info("slack.interactivity", action=action_id, alert_id=alert_id, user=user_name)
    return PlainTextResponse("")
