"""Tests for competitor-customer disqualification."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from signal_agent.quality import competitor_customers as cc


def _make_company(domain="stripe.com", name="Stripe"):
    c = MagicMock()
    c.domain = domain
    c.name = name
    return c


def test_extract_candidates_from_logo_filenames():
    html = '''
    <link rel="preload" href="/customers/stripe.jpg">
    <link rel="preload" href="/customers/ramp-logo.png">
    <img src="/logos/brex.svg">
    '''
    names = cc._extract_candidates(html)
    assert "stripe" in names
    assert "ramp" in names   # "-logo" suffix stripped
    assert "brex" in names


def test_extract_candidates_from_alt_attributes():
    html = '<img alt="Stripe">'
    names = cc._extract_candidates(html)
    assert "stripe" in names


def test_domain_token_match_high_confidence():
    company = _make_company(domain="stripe.com", name="Stripe")
    candidates = {"stripe", "notion", "dropbox"}
    conf = cc._match_company_to_candidates(company, candidates)
    assert conf is not None and conf >= 0.9


def test_fuzzy_name_match():
    company = _make_company(domain="acme.io", name="Acme Corp")
    candidates = {"acme corp logo"}  # close fuzzy match
    conf = cc._match_company_to_candidates(company, candidates)
    assert conf is not None


def test_no_match_returns_none():
    company = _make_company(domain="acme.io", name="Acme")
    candidates = {"vercel", "notion", "dropbox"}
    assert cc._match_company_to_candidates(company, candidates) is None


def test_is_competitor_customer_no_matches():
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    status = cc.is_competitor_customer(session, company_id=1)
    assert status.is_customer is False
    assert status.competitors == []


def test_is_competitor_customer_above_floor():
    row = MagicMock(
        competitor="Braintrust",
        confidence=0.95,
        evidence_url="https://braintrust.dev/",
        is_override=False,
    )
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [row]
    status = cc.is_competitor_customer(session, company_id=1, min_confidence=0.75)
    assert status.is_customer is True
    assert status.competitors == ["Braintrust"]
    assert status.confidence == 0.95


def test_override_bypasses_confidence_floor():
    row = MagicMock(
        competitor="Braintrust",
        confidence=0.5,           # below floor
        evidence_url="operator_override",
        is_override=True,          # but it's an override
    )
    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.return_value = [row]
    status = cc.is_competitor_customer(session, company_id=1, min_confidence=0.75)
    assert status.is_customer is True
