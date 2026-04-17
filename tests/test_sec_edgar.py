"""Light tests for SEC EDGAR helpers.

We don't exercise the HTTP path (EDGAR's contents change daily and we'd be
rate-limited in CI); we just cover the pure logic.
"""
from signal_agent.ingestors.sec_edgar import _extract_relevant_excerpt


def test_excerpt_returns_window_around_keyword():
    text = (
        "Intro paragraph about the business. "
        "Our generative AI capabilities are governed by internal review. "
        "Further text continues here and here."
    )
    excerpt = _extract_relevant_excerpt(text, "generative ai", window=80)
    assert "generative AI capabilities" in excerpt
    # Should not contain the far ends of the text
    assert "Intro paragraph" not in excerpt


def test_excerpt_case_insensitive():
    text = "We are assessing EU AI ACT compliance."
    excerpt = _extract_relevant_excerpt(text, "eu ai act", window=60)
    assert "EU AI ACT" in excerpt


def test_excerpt_missing_keyword_returns_empty():
    assert _extract_relevant_excerpt("no match here", "nonexistent") == ""
