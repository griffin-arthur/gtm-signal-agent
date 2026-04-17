from datetime import datetime, timedelta, timezone

from signal_agent.scoring.rubric import (
    freshness_multiplier,
    score_signal,
    tier_for_score,
)


def test_fresh_signal_full_weight():
    now = datetime.now(timezone.utc)
    s = score_signal("job_posting.ai_governance", detected_at=now, llm_confidence=1.0, now=now)
    # base_weight 9.0 * freshness 1.0 * confidence 1.0
    assert s == 9.0


def test_freshness_decay_halves_at_half_life():
    now = datetime.now(timezone.utc)
    half_life_ago = now - timedelta(days=30)  # matches ai_governance half_life
    s = score_signal("job_posting.ai_governance", detected_at=half_life_ago,
                    llm_confidence=1.0, now=now)
    assert 4.4 < s < 4.6  # ~4.5


def test_confidence_multiplies_score():
    now = datetime.now(timezone.utc)
    s = score_signal("job_posting.ai_governance", detected_at=now, llm_confidence=0.7, now=now)
    assert s == round(9.0 * 0.7, 2)


def test_decay_floor():
    now = datetime.now(timezone.utc)
    ancient = now - timedelta(days=365)
    mult = freshness_multiplier(ancient, now=now, half_life_days=30)
    assert mult == 0.2


def test_tier_bands():
    assert tier_for_score(9.1) == "tier_1"
    assert tier_for_score(8.0) == "tier_1"
    assert tier_for_score(6.0) == "tier_2"
    assert tier_for_score(3.5) == "tier_3"


def test_unknown_signal_type_returns_zero():
    now = datetime.now(timezone.utc)
    assert score_signal("unknown.type", detected_at=now, llm_confidence=1.0, now=now) == 0.0


def test_target_tier_multiplier():
    now = datetime.now(timezone.utc)
    base = score_signal("job_posting.ai_governance", detected_at=now,
                        llm_confidence=1.0, now=now, target_tier=2)
    tier_1 = score_signal("job_posting.ai_governance", detected_at=now,
                          llm_confidence=1.0, now=now, target_tier=1)
    tier_3 = score_signal("job_posting.ai_governance", detected_at=now,
                          llm_confidence=1.0, now=now, target_tier=3)
    # Tier 1 = 1.25x, Tier 2 = 1.0x, Tier 3 = 0.75x
    assert tier_1 > base > tier_3
    assert abs(tier_1 - base * 1.25) < 0.01
    assert abs(tier_3 - base * 0.75) < 0.01


def test_target_tier_default_is_neutral():
    now = datetime.now(timezone.utc)
    default = score_signal("job_posting.ai_governance", detected_at=now,
                           llm_confidence=1.0, now=now)
    explicit = score_signal("job_posting.ai_governance", detected_at=now,
                            llm_confidence=1.0, now=now, target_tier=2)
    assert default == explicit
