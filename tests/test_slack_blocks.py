from signal_agent.integrations.slack import AlertContext, SlackAlerter


def test_block_rendering_contains_expected_parts():
    alerter = SlackAlerter.__new__(SlackAlerter)  # skip __init__ (no token needed)
    ctx = AlertContext(
        company_name="Acme Bank",
        company_domain="acme.com",
        cumulative_score=9.2,
        tier="tier_1",
        summary_for_ae="Acme is hiring its first Head of AI Governance, indicating EU AI Act readiness.",
        top_signals=[
            {"type": "job_posting.ai_governance",
             "url": "https://boards.greenhouse.io/acme/jobs/1",
             "text": "Head of AI Governance"},
        ],
        hubspot_url="https://app.hubspot.com/contacts/_/company/123",
        owner_name="Jamie",
        deal_stage="Qualification",
        alert_id=42,
    )
    blocks = alerter._build_blocks(ctx)
    blob = str(blocks)
    assert "Acme Bank" in blob
    assert "9.2" in blob
    assert "EU AI Act" in blob
    assert "Claim" in blob
    assert "Snooze 30d" in blob
    assert "Open in HubSpot" in blob
    assert "42" in blob  # alert_id propagates into button values
