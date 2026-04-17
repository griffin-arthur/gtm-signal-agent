from unittest.mock import MagicMock

from signal_agent.models import Suppression
from signal_agent.quality import suppression as supp
from signal_agent.schemas import NormalizedSignal


def _make_session(rules):
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = rules
    return session


def _signal(text="Senior MLOps Engineer\nBuild our LLM platform.", name="Acme"):
    return NormalizedSignal(
        company_domain="acme.com",
        company_name=name,
        signal_type="job_posting.ml_platform",
        source="greenhouse",
        source_url="https://example.com/1",
        signal_text=text,
        raw_payload={},
    )


def test_suppression_by_text():
    rules = [Suppression(pattern="on behalf of our client", field="signal_text", reason="proxy")]
    session = _make_session(rules)
    suppressed, reason = supp.is_suppressed(
        session, _signal(text="MLOps Engineer\nPosted on behalf of our client.")
    )
    assert suppressed and reason == "proxy"


def test_suppression_by_company_name():
    rules = [Suppression(pattern="Robert Half", field="company_name", reason="agency")]
    session = _make_session(rules)
    suppressed, _ = supp.is_suppressed(session, _signal(name="Robert Half Technology"))
    assert suppressed


def test_no_rules_no_suppression():
    session = _make_session([])
    suppressed, _ = supp.is_suppressed(session, _signal())
    assert not suppressed
