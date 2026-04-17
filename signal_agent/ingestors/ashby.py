"""Ashby job board ingestor.

Public API: `https://api.ashbyhq.com/posting-api/job-board/{slug}`
Returns `{ jobs: [ { title, descriptionPlain, descriptionHtml, jobUrl, ... } ] }`
No auth required for published listings.

Used for companies like Ramp that are on Ashby rather than Greenhouse/Lever.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import structlog
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from signal_agent.ingestors.base import CompanyTarget, Ingestor
from signal_agent.ingestors.keywords import classify_job
from signal_agent.schemas import NormalizedSignal

log = structlog.get_logger()

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyIngestor(Ingestor):
    source = "ashby"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        if not target.ashby_slug:
            return
        url = f"{API_BASE}/{target.ashby_slug}?includeCompensation=false"
        client = await self._get_client()

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        ):
            with attempt:
                resp = await client.get(url)
                if resp.status_code == 404:
                    log.warning("ashby.slug_not_found", slug=target.ashby_slug)
                    return
                resp.raise_for_status()
                data = resp.json()

        for job in data.get("jobs", []):
            if not job.get("isListed", True):
                continue
            title = job.get("title", "").strip()
            description = (job.get("descriptionPlain") or "").strip()
            classification = classify_job(title, description)
            if classification is None:
                continue
            signal_type, matched = classification

            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type=signal_type,
                source=self.source,
                source_url=job.get("jobUrl", url),
                signal_text=f"{title}\n\n{description[:500]}",
                raw_payload={
                    "title": title,
                    "location": job.get("location"),
                    "department": job.get("department"),
                    "team": job.get("team"),
                    "employment_type": job.get("employmentType"),
                    "published_at": job.get("publishedAt"),
                    "internal_job_id": job.get("id"),
                },
                matched_keywords=matched,
                suggested_tier_hint=1 if signal_type in {
                    "job_posting.ai_governance", "job_posting.ai_leadership"
                } else 2,
            )
