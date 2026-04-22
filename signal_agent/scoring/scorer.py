"""Apply the rubric to DB-resident signals, compute company rollups.

The scorer is called after validation. It updates Signal rows with their score/tier,
computes the cumulative score for the company over the configured window, and
decides whether the incoming signal warrants an alert.

### Alert decision (docs/icp.md + build brief §7)

A naive "alert any time cumulative_score ≥ threshold" floods the channel: once
a company crosses the threshold, every subsequent validated signal re-crosses
it and fires again. We apply these layers of gating:

1. **Same-URL dedup** — if the triggering signal's `source_url` was already
   the trigger for a prior alert on this company, NEVER re-alert. Protects
   against the Slack channel seeing the same news article / job post twice
   on consecutive runs. Applies to ALL signal types including always-alert.

2. **Always-alert signal types** (Tier 1 urgency, bypass cooldown+threshold):
   - `news.ai_incident`
   - `job_posting.ai_governance`
   - `job_posting.ai_leadership`
   - `news.exec_hire_ai`
   - `linkedin.exec_hire_ai`
   Requires a DIFFERENT source_url than any previously-alerted signal.

3. **First-time threshold crossing** — company has never been alerted, score
   now crosses single-signal OR cumulative threshold. One alert.

4. **Same-signal-type cooldown** — within ALERT_COOLDOWN_HOURS of the
   previous alert, re-alerting on the same signal_type is suppressed.
   A NEW signal type (e.g. exec_hire after a prior job_posting alert)
   passes through subject to the material-change check.

5. **Material-change during cooldown** — re-alert only if cumulative score
   has grown by ≥ ALERT_MATERIAL_CHANGE_RATIO over the last-alerted score.

After cooldown expires, alerts resume normally — still subject to URL dedup.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_agent.config import settings
from signal_agent.models import Alert, Company, Signal, SignalStatus, SignalTier
from signal_agent.schemas import CompanyScoreRollup
from signal_agent.scoring.rubric import score_signal, tier_for_score

# Signal types that always alert immediately, regardless of cooldown. Per the
# build brief: "Tier 1 signals always alert immediately."
ALWAYS_ALERT_SIGNAL_TYPES = {
    "news.ai_incident",
    "job_posting.ai_governance",
    "job_posting.ai_leadership",
    "news.exec_hire_ai",
    "linkedin.exec_hire_ai",
}


@dataclass
class AlertDecision:
    """Why we did or didn't fire an alert. Structured for observability + tests."""
    should_fire: bool
    reason: str   # "always_alert" | "first_crossing" | "material_change" | "cooldown" | ...
    delta_vs_last: float | None = None  # cumulative - last_alerted_score, when applicable


def update_signal_score(session: Session, signal: Signal) -> None:
    """Called after a Signal has been validated. Writes raw_score + tier in place."""
    score = score_signal(
        signal_type=signal.signal_type,
        detected_at=signal.detected_at,
        llm_confidence=signal.llm_confidence or 0.0,
        target_tier=signal.company.target_tier if signal.company else 2,
    )
    signal.raw_score = score
    signal.tier = SignalTier(tier_for_score(score))


def cumulative_company_score(session: Session, company_id: int) -> CompanyScoreRollup:
    """Sum all validated signals for the company within the lookback window."""
    window_start = datetime.now(timezone.utc) - timedelta(days=settings.alert_cumulative_window_days)
    rows = session.execute(
        select(Signal).where(
            Signal.company_id == company_id,
            Signal.status == SignalStatus.VALIDATED,
            Signal.detected_at >= window_start,
        )
    ).scalars().all()

    if not rows:
        return CompanyScoreRollup(
            company_id=company_id,
            cumulative_score=0.0,
            top_tier="tier_3",
            window_days=settings.alert_cumulative_window_days,
            contributing_signal_ids=[],
        )

    total = round(sum(r.raw_score for r in rows), 2)
    top_tier = min(r.tier.value for r in rows if r.tier is not None)  # tier_1 < tier_2 < tier_3 lex
    return CompanyScoreRollup(
        company_id=company_id,
        cumulative_score=total,
        top_tier=top_tier,
        window_days=settings.alert_cumulative_window_days,
        contributing_signal_ids=[r.id for r in rows],
    )


def _prior_alert_summary(session: Session, company_id: int) -> tuple[set[str], set[str]]:
    """Collect the set of (source_urls, signal_types) from every prior alert
    for this company. Used for cross-run dedup.

    Cheap: each company typically has a handful of alerts total; the join
    is on indexed foreign keys. No need to narrow by a time window since
    source_url dedup is intentionally "forever" — if we've Slack-posted
    about this article before, we never post it again.
    """
    rows = session.execute(
        select(Signal.source_url, Signal.signal_type)
        .join(Alert, Alert.triggering_signal_id == Signal.id)
        .where(Alert.company_id == company_id)
    ).all()
    urls = {r[0] for r in rows if r[0]}
    types = {r[1] for r in rows if r[1]}
    return urls, types


def should_alert(
    rollup: CompanyScoreRollup,
    triggering: Signal,
    company: Company,
    session: Session | None = None,
    now: datetime | None = None,
) -> AlertDecision:
    """Decide whether to fire an alert for this signal.

    See module docstring for the full decision tree. Inputs:
      - rollup:      the company's cumulative score across the window
      - triggering:  the newly-ingested signal that landed us here
      - company:     has last_alerted_at / last_alerted_score for cooldown
      - session:     SQLAlchemy session; used to query prior-alert history
                     for cross-run URL + signal-type dedup. Optional for
                     backward compatibility — callers without session skip
                     the dedup checks (legacy behavior).
    """
    now = now or datetime.now(timezone.utc)

    # Gather the prior-alert history for this company (URLs + signal types
    # previously posted to Slack). Empty if session is None (legacy callers).
    prior_urls: set[str] = set()
    prior_types: set[str] = set()
    if session is not None:
        prior_urls, prior_types = _prior_alert_summary(session, company.id)

    # Rule 1: same-URL dedup. ALWAYS applies, including to always-alert
    # signal types. If we've Slack-posted about this specific article or
    # job posting before for this company, we never post it again.
    if triggering.source_url and triggering.source_url in prior_urls:
        return AlertDecision(should_fire=False, reason="already_alerted_on_this_url")

    # Rule 2: always-alert signal types. Bypass threshold + material-change,
    # BUT only if this signal type hasn't already alerted during the active
    # cooldown. Two news.exec_hire_ai articles about the same FICO CAIO
    # appointment shouldn't both fire — one is enough; a different signal
    # type (news.ai_incident) would still break through.
    if triggering.signal_type in ALWAYS_ALERT_SIGNAL_TYPES:
        if company.last_alerted_at is not None:
            cooldown_end = company.last_alerted_at + timedelta(
                hours=settings.alert_cooldown_hours,
            )
            in_cooldown = now < cooldown_end
            if in_cooldown and triggering.signal_type in prior_types:
                return AlertDecision(
                    should_fire=False,
                    reason="always_alert_suppressed_same_type_in_cooldown",
                )
        return AlertDecision(should_fire=True, reason="always_alert")

    # Below threshold? Never alert.
    above_single = triggering.raw_score >= settings.alert_score_threshold
    above_cumulative = rollup.cumulative_score >= settings.alert_cumulative_threshold
    if not (above_single or above_cumulative):
        return AlertDecision(should_fire=False, reason="below_threshold")

    # Rule 3: first-time crossing — company has never been alerted.
    if company.last_alerted_at is None:
        return AlertDecision(should_fire=True, reason="first_crossing")

    # Rule 4: we're inside the cooldown window.
    cooldown_end = company.last_alerted_at + timedelta(hours=settings.alert_cooldown_hours)
    in_cooldown = now < cooldown_end

    # Rule 4a: same signal type as a prior alert while in cooldown → suppress.
    # Handles the "same exec hire / same news article keeps re-scoring" case.
    # A genuinely new signal type (e.g. new job posting after exec-hire alert)
    # bypasses this and gets evaluated by the material-change rule.
    if in_cooldown and triggering.signal_type in prior_types:
        return AlertDecision(
            should_fire=False, reason="cooldown_same_signal_type",
        )

    last_score = company.last_alerted_score or 0.0
    delta = rollup.cumulative_score - last_score
    # Material change: cumulative score has grown by >= ratio over the score
    # we last alerted on. Using last_score as the denominator ensures that
    # tiny-ratio alerts at very high scores still trigger (e.g., going from
    # 40 → 61 is material even though 40 > 0).
    if last_score <= 0:
        materially_changed = rollup.cumulative_score > 0
    else:
        materially_changed = delta / last_score >= settings.alert_material_change_ratio

    if in_cooldown and not materially_changed:
        return AlertDecision(
            should_fire=False, reason="cooldown", delta_vs_last=delta,
        )

    if in_cooldown and materially_changed:
        return AlertDecision(
            should_fire=True, reason="material_change", delta_vs_last=delta,
        )

    # Cooldown expired — a fresh threshold crossing reopens alerts.
    return AlertDecision(
        should_fire=True, reason="cooldown_expired", delta_vs_last=delta,
    )


def mark_alerted(company: Company, cumulative_score: float,
                 now: datetime | None = None) -> None:
    """Update cooldown state after an alert fires. Call from within a session."""
    company.last_alerted_at = now or datetime.now(timezone.utc)
    company.last_alerted_score = cumulative_score
