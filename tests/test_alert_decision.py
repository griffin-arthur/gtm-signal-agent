"""Tests for the cooldown + material-change + dedup alert decision logic."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from signal_agent.scoring.scorer import (
    ALWAYS_ALERT_SIGNAL_TYPES,
    AlertDecision,
    should_alert,
)


# ---- test helpers -----------------------------------------------------------

def _make_signal(signal_type="job_posting.ml_platform", raw_score=7.0,
                 tier="tier_2", source_url="https://example.com/a"):
    sig = MagicMock()
    sig.signal_type = signal_type
    sig.raw_score = raw_score
    sig.tier = MagicMock(value=tier)
    sig.source_url = source_url
    return sig


def _make_rollup(cumulative=20.0, top_tier="tier_2"):
    r = MagicMock()
    r.cumulative_score = cumulative
    r.top_tier = top_tier
    r.contributing_signal_ids = [1, 2, 3]
    return r


def _make_company(last_alerted_at=None, last_alerted_score=None, company_id=1):
    c = MagicMock()
    c.id = company_id
    c.last_alerted_at = last_alerted_at
    c.last_alerted_score = last_alerted_score
    return c


def _apply_settings(s_mock):
    """Pin every setting the scorer reads so MagicMock doesn't propagate."""
    s_mock.alert_score_threshold = 8
    s_mock.alert_cumulative_threshold = 12
    s_mock.alert_cooldown_hours = 24
    s_mock.alert_material_change_ratio = 0.5
    s_mock.alert_same_type_cooldown_days = 7


# ---- threshold / cooldown / material-change ---------------------------------

def test_below_threshold_never_alerts():
    sig = _make_signal(raw_score=2.0)
    rollup = _make_rollup(cumulative=5.0)
    company = _make_company()
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_alert(rollup, sig, company)
    assert d.should_fire is False
    assert d.reason == "below_threshold"


def test_first_crossing_fires():
    sig = _make_signal(raw_score=7.0)
    rollup = _make_rollup(cumulative=20.0)
    company = _make_company(last_alerted_at=None)
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_alert(rollup, sig, company)
    assert d.should_fire is True
    assert d.reason == "first_crossing"


def test_cooldown_suppresses_non_material_change():
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="job_posting.ml_platform",
        raw_score=5.0,
        source_url="https://example.com/never-alerted-before",
    )
    rollup = _make_rollup(cumulative=21.0)  # +5% over last alert of 20.0
    company = _make_company(
        last_alerted_at=now - timedelta(hours=2),
        last_alerted_score=20.0,
    )
    # No prior same-type alerts (different signal type was the last alert).
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"news.ai_product_launch": now - timedelta(hours=2)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is False
    assert d.reason == "cooldown"


def test_cooldown_fires_on_material_change():
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="job_posting.ml_platform", raw_score=5.0,
        source_url="https://example.com/new-job",
    )
    rollup = _make_rollup(cumulative=35.0)  # +75% over 20.0
    company = _make_company(
        last_alerted_at=now - timedelta(hours=2),
        last_alerted_score=20.0,
    )
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"news.ai_product_launch": now - timedelta(hours=2)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is True
    assert d.reason == "material_change"
    assert d.delta_vs_last == 15.0


def test_cooldown_expired_allows_alert():
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="job_posting.ml_platform", raw_score=5.0,
        source_url="https://example.com/new-job",
    )
    rollup = _make_rollup(cumulative=21.0)
    company = _make_company(
        last_alerted_at=now - timedelta(hours=30),  # past 24h general cooldown
        last_alerted_score=20.0,
    )
    # And the prior same-type alert is >7 days old, so same-type window expired too.
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"job_posting.ml_platform": now - timedelta(days=14)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is True
    assert d.reason == "cooldown_expired"


# ---- always-alert behavior --------------------------------------------------

def test_always_alert_bypasses_everything():
    """Tier-1 signal types bypass thresholds AND general cooldown, as long as
    the same signal_type isn't in its rolling same-type window."""
    for st in ALWAYS_ALERT_SIGNAL_TYPES:
        sig = _make_signal(signal_type=st, raw_score=0.1)  # way below threshold
        rollup = _make_rollup(cumulative=0.0)
        company = _make_company(
            last_alerted_at=datetime.now(timezone.utc),  # fully in cooldown
            last_alerted_score=100.0,
        )
        # No session → legacy path, no dedup
        with patch("signal_agent.scoring.scorer.settings") as s:
            _apply_settings(s)
            d = should_alert(rollup, sig, company)
        assert d.should_fire is True, f"{st} should bypass general cooldown"
        assert d.reason == "always_alert"


def test_legacy_no_session_skips_dedup():
    """Callers that don't pass a session get the old, simpler behavior."""
    sig = _make_signal(signal_type="news.exec_hire_ai", raw_score=7.0)
    rollup = _make_rollup(cumulative=7.0)
    company = _make_company()
    with patch("signal_agent.scoring.scorer.settings") as s:
        _apply_settings(s)
        d = should_alert(rollup, sig, company)
    assert d.should_fire is True
    assert d.reason == "always_alert"


# ---- URL dedup --------------------------------------------------------------

def test_url_dedup_suppresses_same_article_always_alert():
    """Re-ingesting the same news article for a company that's been alerted
    on it must not fire again, even though it's an always-alert type.
    Regression for the FICO/Vercel duplicate."""
    url = "https://example.com/fico-caio"
    sig = _make_signal(
        signal_type="news.exec_hire_ai", raw_score=7.0, source_url=url,
    )
    rollup = _make_rollup(cumulative=7.0)
    company = _make_company()
    now = datetime.now(timezone.utc)
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=({url}, {"news.exec_hire_ai": now - timedelta(days=10)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is False
    assert d.reason == "already_alerted_on_this_url"


def test_url_dedup_allows_new_article_same_type_past_window():
    """A different URL of the same signal type for the same company can
    still fire when the same-type rolling window has passed."""
    sig = _make_signal(
        signal_type="news.exec_hire_ai", raw_score=7.0,
        source_url="https://example.com/fresh-article",
    )
    rollup = _make_rollup(cumulative=7.0)
    company = _make_company()
    now = datetime.now(timezone.utc)
    # prior same-type was 10 days ago > 7-day same-type window
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=({"https://example.com/old"},
                             {"news.exec_hire_ai": now - timedelta(days=10)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is True
    assert d.reason == "always_alert"


# ---- same-signal-type cooldown ---------------------------------------------

def test_same_signal_type_within_window_suppresses_non_always_alert():
    """Within the same-type rolling window, a non-always-alert signal of the
    same type as a prior alert is suppressed."""
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="job_posting.ml_platform", raw_score=7.0,
        source_url="https://example.com/new-job",
    )
    rollup = _make_rollup(cumulative=25.0)
    company = _make_company(
        last_alerted_at=now - timedelta(hours=2),
        last_alerted_score=20.0,
    )
    # Prior same-type alert was 2 days ago < 7-day window
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"job_posting.ml_platform": now - timedelta(days=2)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is False
    assert d.reason == "cooldown_same_signal_type"


def test_different_signal_type_passes_to_material_check():
    """A genuinely new signal type bypasses the same-type gate and falls
    through to the material-change rule."""
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="news.ai_product_launch", raw_score=5.0,
        source_url="https://example.com/launch",
    )
    rollup = _make_rollup(cumulative=35.0)  # +75% over 20.0
    company = _make_company(
        last_alerted_at=now - timedelta(hours=2),
        last_alerted_score=20.0,
    )
    # Prior was a different type
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"job_posting.ml_platform": now - timedelta(days=2)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is True
    assert d.reason == "material_change"


def test_always_alert_same_type_within_window_suppressed():
    """Two news.exec_hire_ai articles for the same company within the
    same-type rolling window should fire only once."""
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="news.exec_hire_ai", raw_score=7.0,
        source_url="https://example.com/fico-second-article",
    )
    rollup = _make_rollup(cumulative=14.0)
    company = _make_company(
        last_alerted_at=now - timedelta(hours=1),
        last_alerted_score=7.0,
    )
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=({"https://example.com/fico-first-article"},
                             {"news.exec_hire_ai": now - timedelta(days=1)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is False
    assert d.reason == "always_alert_suppressed_same_type_in_cooldown"


def test_always_alert_different_type_still_fires_within_cooldown():
    """A genuinely new always-alert signal type breaks through even if a
    DIFFERENT always-alert signal fired recently. An AI incident at FICO
    after a prior exec-hire alert still posts."""
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="news.ai_incident", raw_score=9.0,
        source_url="https://example.com/fico-incident",
    )
    rollup = _make_rollup(cumulative=16.0)
    company = _make_company(
        last_alerted_at=now - timedelta(hours=1),
        last_alerted_score=7.0,
    )
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"news.exec_hire_ai": now - timedelta(days=1)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is True
    assert d.reason == "always_alert"


def test_always_alert_same_type_past_window_fires_again():
    """Once the same-type rolling window expires (default 7 days), the same
    signal_type can fire again — with a fresh URL."""
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="news.exec_hire_ai", raw_score=7.0,
        source_url="https://example.com/new-caio-announcement",
    )
    rollup = _make_rollup(cumulative=7.0)
    company = _make_company(
        last_alerted_at=now - timedelta(days=30),  # last alert long ago
        last_alerted_score=7.0,
    )
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=({"https://example.com/old-article"},
                             {"news.exec_hire_ai": now - timedelta(days=30)})):
        _apply_settings(s)
        d = should_alert(rollup, sig, company, session=MagicMock(), now=now)
    assert d.should_fire is True
    assert d.reason == "always_alert"
