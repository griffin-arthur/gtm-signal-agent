from signal_agent.ingestors.keywords import classify_job


def test_governance_role_matches_tier1():
    result = classify_job(
        title="Head of AI Governance",
        description="Lead our responsible AI program across the ML platform.",
    )
    assert result is not None
    signal_type, matched = result
    assert signal_type == "job_posting.ai_governance"
    assert "ai governance" in matched


def test_head_of_ai_matches_leadership():
    result = classify_job(
        title="Head of AI",
        description="Own the AI roadmap across the company.",
    )
    assert result is not None
    assert result[0] == "job_posting.ai_leadership"


def test_ml_platform_matches_tier2():
    result = classify_job(
        title="Staff MLOps Engineer",
        description="Scale our LLM platform to serve millions of requests.",
    )
    assert result is not None
    assert result[0] == "job_posting.ml_platform"


def test_unrelated_role_returns_none():
    assert classify_job(
        title="Senior Accountant",
        description="Own monthly close and financial reporting.",
    ) is None


def test_recruiting_agency_is_filtered():
    result = classify_job(
        title="Head of AI Governance",
        description="Recruiting agency seeking talent on behalf of our client.",
    )
    assert result is None
