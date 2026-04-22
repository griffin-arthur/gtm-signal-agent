"""Tests for the cooldown + material-change alert decision logic."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from signal_agent.scoring.scorer import (
    ALWAYS_ALERT_SIGNAL_TYPES,
    AlertDecision,
    should_alert,
)


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


def _make_session_with_prior(prior_urls: set[str], prior_types: set[str]):
    """Build a session mock whose _prior_alert_summary lookup returns
    the given sets. The underlying SELECT is mocked out."""
    session = MagicMock()
    # The query returns rows of (source_url, signal_type) tuples.
    rows = [(u, "any") for u in prior_urls] + [("x", t) for t in prior_types]
    session.execute.return_value.all.return_value = rows
    return session


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


# --- cross-run dedup tests ---------------------------------------------------
# These exercise the session-aware path: URL dedup + same-signal-type cooldown.
# The session is a plain MagicMock; we patch _prior_alert_summary directly so
# the tests don't need to reason about the real SQL query shape.

def test_url_dedup_suppresses_same_article_always_alert():
    """Re-ingesting the same news article for a company that's already been
    alerted on it must not fire again, even though news.exec_hire_ai is in
    ALWAYS_ALERT_SIGNAL_TYPES. Regression for the FICO/Vercel dup."""
    url = "https://example.com/fico-caio"
    sig = _make_signal(
        signal_type="news.exec_hire_ai", raw_score=7.0, source_url=url,
    )
    rollup = _make_rollup(cumulative=7.0)
    company = _make_company()
    session = MagicMock()
    with patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=({url}, {"news.exec_hire_ai"})):
        d = should_alert(rollup, sig, company, session=session)
    assert d.should_fire is False
    assert d.reason == "already_alerted_on_this_url"


def test_url_dedup_allows_new_article_same_type():
    """A different URL of the same signal type for the same company still
    fires (respecting other rules). Only the exact URL is blocked."""
    sig = _make_signal(
        signal_type="news.exec_hire_ai", raw_score=7.0,
        source_url="https://example.com/second-article",
    )
    rollup = _make_rollup(cumulative=7.0)
    company = _make_company()
    session = MagicMock()
    with patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=({"https://example.com/prior"}, {"news.exec_hire_ai"})):
        d = should_alert(rollup, sig, company, session=session)
    assert d.should_fire is True
    assert d.reason == "always_alert"


def test_same_signal_type_during_cooldown_suppressed():
    """Non-always-alert signal type, same as a prior alert, within cooldown
    → suppressed even if cumulative score technically would qualify."""
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="job_posting.ml_platform", raw_score=7.0,
        source_url="https://example.com/new-job",
    )
    rollup = _make_rollup(cumulative=25.0)
    company = _make_company(
        last_alerted_at=now - timedelta(hours=2),  # in cooldown
        last_alerted_score=20.0,
    )
    session = MagicMock()
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"job_posting.ml_platform"})):
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        s.alert_cooldown_hours = 24
        s.alert_material_change_ratio = 0.5
        d = should_alert(rollup, sig, company, session=session, now=now)
    assert d.should_fire is False
    assert d.reason == "cooldown_same_signal_type"


def test_new_signal_type_during_cooldown_passes_to_material_check():
    """A genuinely new signal type bypasses the same-type cooldown and
    gets evaluated by the material-change rule. Here the score jump is
    large so it fires."""
    now = datetime.now(timezone.utc)
    sig = _make_signal(
        signal_type="news.ai_product_launch", raw_score=5.0,
        source_url="https://example.com/new-product",
    )
    rollup = _make_rollup(cumulative=35.0)  # +75% over last alert of 20.0
    company = _make_company(
        last_alerted_at=now - timedelta(hours=2),
        last_alerted_score=20.0,
    )
    session = MagicMock()
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"job_posting.ml_platform"})):
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        s.alert_cooldown_hours = 24
        s.alert_material_change_ratio = 0.5
        d = should_alert(rollup, sig, company, session=session, now=now)
    assert d.should_fire is True
    assert d.reason == "material_change"


def test_legacy_no_session_skips_dedup():
    """Backward compat — callers that don't pass a session get the old
    behavior (no URL dedup, no same-type check). Keeps tests + old code
    paths working during rollout."""
    sig = _make_signal(signal_type="news.exec_hire_ai", raw_score=7.0)
    rollup = _make_rollup(cumulative=7.0)
    company = _make_company()
    d = should_alert(rollup, sig, company)  # no session arg
    assert d.should_fire is True
    assert d.reason == "always_alert"


def test_always_alert_same_signal_type_in_cooldown_suppressed():
    """Two news.exec_hire_ai articles for the same company within cooldown
    should fire only once. A different URL passes URL dedup but the
    same-type-in-cooldown rule catches the second. Regression for FICO
    getting two CAIO alerts from different article URLs."""
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
    session = MagicMock()
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=({"https://example.com/fico-first-article"},
                             {"news.exec_hire_ai"})):
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        s.alert_cooldown_hours = 24
        s.alert_material_change_ratio = 0.5
        d = should_alert(rollup, sig, company, session=session, now=now)
    assert d.should_fire is False
    assert d.reason == "always_alert_suppressed_same_type_in_cooldown"


def test_always_alert_different_signal_type_still_fires():
    """A genuinely new always-alert signal type for a company in cooldown
    still fires. News of an AI incident at FICO breaks through even if
    FICO was just alerted on an exec hire."""
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
    session = MagicMock()
    with patch("signal_agent.scoring.scorer.settings") as s, \
         patch("signal_agent.scoring.scorer._prior_alert_summary",
               return_value=(set(), {"news.exec_hire_ai"})):
        s.alert_score_threshold = 8
        s.alert_cumulative_threshold = 12
        s.alert_cooldown_hours = 24
        s.alert_material_change_ratio = 0.5
        d = should_alert(rollup, sig, company, session=session, now=now)
    assert d.should_fire is True
    assert d.reason == "always_alert"
