from unittest.mock import MagicMock, patch

from signal_agent.models import SignalTier
from signal_agent.quality import digest


def _fake_session_with_count(alert_count: int):
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = alert_count
    return session


def test_tier1_never_batches():
    session = _fake_session_with_count(100)  # huge rate
    assert digest.should_batch(session, SignalTier.TIER_1) is False


def test_below_threshold_does_not_batch():
    with patch.object(digest, "settings") as mock_settings:
        mock_settings.digest_rate_threshold = 5
        session = _fake_session_with_count(3)
        assert digest.should_batch(session, SignalTier.TIER_2) is False


def test_above_threshold_batches_tier2():
    with patch.object(digest, "settings") as mock_settings:
        mock_settings.digest_rate_threshold = 5
        session = _fake_session_with_count(5)
        assert digest.should_batch(session, SignalTier.TIER_2) is True
        assert digest.should_batch(session, SignalTier.TIER_3) is True
