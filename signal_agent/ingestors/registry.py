"""Registry of enabled ingestors.

Adding a source in a future phase: implement `Ingestor`, register here.
Workflow code iterates this registry — no conditional branching by source name.
"""
from __future__ import annotations

from signal_agent.ingestors.ashby import AshbyIngestor
from signal_agent.ingestors.base import Ingestor
from signal_agent.ingestors.competitive import CompetitiveIngestor
from signal_agent.ingestors.conferences import ConferenceIngestor
from signal_agent.ingestors.greenhouse import GreenhouseIngestor
from signal_agent.ingestors.lever import LeverIngestor
from signal_agent.ingestors.linkedin import LinkedInHiresIngestor
from signal_agent.ingestors.news import NewsIngestor
from signal_agent.ingestors.sec_edgar import SecEdgarIngestor
from signal_agent.ingestors.workday import WorkdayIngestor


def enabled_ingestors() -> list[Ingestor]:
    return [
        GreenhouseIngestor(),
        LeverIngestor(),
        AshbyIngestor(),
        WorkdayIngestor(),
        NewsIngestor(),
        SecEdgarIngestor(),
        CompetitiveIngestor(),
        ConferenceIngestor(),
        LinkedInHiresIngestor(),  # no-op without API key
    ]
