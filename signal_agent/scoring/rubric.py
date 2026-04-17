"""Scoring rubric.

Translates the signal taxonomy from docs/icp.md §4 into concrete weights,
freshness decay, and tier thresholds. This file is the #1 tuning knob —
expect to adjust weights after each weekly review loop.

Rules:
- A signal's `raw_score` starts at its base weight.
- Freshness decay multiplies by 0.5^(age_days / half_life_days), floor 0.2.
- LLM confidence multiplies score linearly.
- Account target_tier multiplies score (Tier 1 accounts weighted highest).
- Tier bands are fixed (Tier 1: 8-10, Tier 2: 5-7, Tier 3: 2-4).
- Cumulative company score = sum of active signal scores over the last
  ALERT_CUMULATIVE_WINDOW_DAYS days.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class RubricEntry:
    base_weight: float
    half_life_days: float


# Signal type → rubric. Types mirror what ingestors emit.
# Weights calibrated to Arthur's buyer urgency hierarchy:
#   - AI incidents are highest (reactive urgency, shortest cycle)
#   - Governance-specific hires and roles are near-top
#   - Exec hires signal medium-term urgency
#   - ML platform scaling is supporting evidence
#   - SEC filings are lower per-signal but highly credible (exec-signed)
#   - Competitive mentions + conference speakers = awareness signals
RUBRIC: dict[str, RubricEntry] = {
    # Tier 1 — buying window, urgent
    "news.ai_incident":            RubricEntry(base_weight=9.5, half_life_days=14),
    "job_posting.ai_governance":   RubricEntry(base_weight=9.0, half_life_days=30),
    "job_posting.ai_leadership":   RubricEntry(base_weight=8.5, half_life_days=45),
    "news.exec_hire_ai":           RubricEntry(base_weight=8.5, half_life_days=45),
    "linkedin.exec_hire_ai":       RubricEntry(base_weight=8.0, half_life_days=45),
    # Tier 2 — scaling signals
    "news.ai_product_launch":      RubricEntry(base_weight=6.5, half_life_days=30),
    "job_posting.ml_platform":     RubricEntry(base_weight=6.0, half_life_days=30),
    "filing.sec_ai_mention":       RubricEntry(base_weight=6.0, half_life_days=90),
    "conference.speaker":          RubricEntry(base_weight=5.5, half_life_days=60),
    # Tier 3 — awareness
    "competitive.mentioned_with":  RubricEntry(base_weight=3.5, half_life_days=30),
}


# Account-level tier multiplier. Tier 1 = Arthur's highest-priority accounts
# (Segment A, core ICP). Same raw signal is worth more at a Tier 1 account.
# See docs/icp.md §7 for the tiering rubric.
TARGET_TIER_MULTIPLIER: dict[int, float] = {
    1: 1.25,
    2: 1.00,
    3: 0.75,
}

TIER_BANDS = [
    ("tier_1", 8.0, 10.0),
    ("tier_2", 5.0, 7.99),
    ("tier_3", 2.0, 4.99),
]

DECAY_FLOOR = 0.2


def freshness_multiplier(detected_at: datetime, now: datetime | None = None,
                         half_life_days: float = 30) -> float:
    now = now or datetime.now(timezone.utc)
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - detected_at).total_seconds() / 86400)
    mult = 0.5 ** (age_days / half_life_days)
    return max(mult, DECAY_FLOOR)


def score_signal(signal_type: str, detected_at: datetime,
                 llm_confidence: float, now: datetime | None = None,
                 target_tier: int = 2) -> float:
    """Return the scalar score for a single validated signal.

    `target_tier` is the Arthur account tier (1–3); defaults to 2 so callers
    that don't know the tier still get baseline scoring.
    """
    entry = RUBRIC.get(signal_type)
    if entry is None:
        return 0.0
    fresh = freshness_multiplier(detected_at, now=now, half_life_days=entry.half_life_days)
    tier_mult = TARGET_TIER_MULTIPLIER.get(target_tier, 1.0)
    # Confidence directly multiplies score — a 0.7 confidence signal gets 70% credit.
    return round(entry.base_weight * fresh * llm_confidence * tier_mult, 2)


def tier_for_score(score: float) -> str:
    for name, lo, hi in TIER_BANDS:
        if lo <= score <= hi:
            return name
    if score > 10:
        return "tier_1"
    return "tier_3"
