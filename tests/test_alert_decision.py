"""Tests for the cooldown + material-change alert decision logic."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from signal_agent.scoring.scorer import (
    ALWAYS_ALERT_SIGNAL_TYPES,
    AlertDecision,
    should_alert,
)


def _make_signal(signal_type="job_posting.ml_platform", raw_score=7.0, tier="tier_2"):
    sig = MagicMock()
    sig.signal_type = signal_type
    sig.raw_score = raw_score
    sig.tier = MagicMock(value=tier)
    return sig


def _make_rollup(cumulative=20.0, top_tier="tier_2"):
    r = MagicMock()
    r.cumulative_score = cumulative
    r.top_tier = top_tier
    r.contributing_signal_ids = [1, 2, 3]
    return r


def _make_company(last_alerted_at=None, last_alerted_score=None):
    c = MagicMock()
    c.last_alerted_at = last_alerted_at
    c.last_alerted_score = last_alerted_score
    return c


def test_below_threshold_never_alerts():
    sig = _make_signal(raw_score=2.0)
    rollup = _make_rollup(cumulative=5.0)
    company = _make_company()
    with patch("signal_agent.scoring.scorer.settings") as s:
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        d = should_alert(rollup, sig, company)
    assert d.should_fire is False
    assert d.reason == "below_threshold"


def test_first_crossing_fires():
    sig = _make_signal(raw_score=7.0)
    rollup = _make_rollup(cumulative=20.0)
    company = _make_company(last_alerted_at=None)
    with patch("signal_agent.scoring.scorer.settings") as s:
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        d = should_alert(rollup, sig, company)
    assert d.should_fire is True
    assert d.reason == "first_crossing"


def test_cooldown_suppresses_non_material_change():
    now = datetime.now(timezone.utc)
    sig = _make_signal(raw_score=5.0)
    rollup = _make_rollup(cumulative=21.0)  # small +5% over last alert of 20.0
    company = _make_company(
        last_alerted_at=now - timedelta(hours=2),
        last_alerted_score=20.0,
    )
    with patch("signal_agent.scoring.scorer.settings") as s:
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        s.alert_cooldown_hours = 24
        s.alert_material_change_ratio = 0.5
        d = should_alert(rollup, sig, company, now=now)
    assert d.should_fire is False
    assert d.reason == "cooldown"


def test_cooldown_fires_on_material_change():
    now = datetime.now(timezone.utc)
    sig = _make_signal(raw_score=5.0)
    rollup = _make_rollup(cumulative=35.0)  # +75% over last alert of 20.0
    company = _make_company(
        last_alerted_at=now - timedelta(hours=2),
        last_alerted_score=20.0,
    )
    with patch("signal_agent.scoring.scorer.settings") as s:
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        s.alert_cooldown_hours = 24
        s.alert_material_change_ratio = 0.5
        d = should_alert(rollup, sig, company, now=now)
    assert d.should_fire is True
    assert d.reason == "material_change"
    assert d.delta_vs_last == 15.0


def test_cooldown_expired_allows_alert():
    now = datetime.now(timezone.utc)
    sig = _make_signal(raw_score=5.0)
    rollup = _make_rollup(cumulative=21.0)  # same low delta
    company = _make_company(
        last_alerted_at=now - timedelta(hours=30),  # past cooldown
        last_alerted_score=20.0,
    )
    with patch("signal_agent.scoring.scorer.settings") as s:
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        s.alert_cooldown_hours = 24
        s.alert_material_change_ratio = 0.5
        d = should_alert(rollup, sig, company, now=now)
    assert d.should_fire is True
    assert d.reason == "cooldown_expired"


def test_always_alert_bypasses_everything():
    for st in ALWAYS_ALERT_SIGNAL_TYPES:
        sig = _make_signal(signal_type=st, raw_score=0.1)  # below threshold
        rollup = _make_rollup(cumulative=0.0)
        company = _make_company(
            last_alerted_at=datetime.now(timezone.utc),  # fully in cooldown
            last_alerted_score=100.0,
        )
        with patch("signal_agent.scoring.scorer.settings") as s:
            s.alert_score_threshold = 8
            s.alert_cumulative_threshold = 12
            s.alert_cooldown_hours = 24
            s.alert_material_change_ratio = 0.5
            d = should_alert(rollup, sig, company)
        assert d.should_fire is True, f"{st} should bypass cooldown"
        assert d.reason == "always_alert"
