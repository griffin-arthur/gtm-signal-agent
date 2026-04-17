"""Greenhouse job board ingestor.

Uses the public Job Board API: `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
which returns all open postings including full HTML descriptions. No auth required.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import structlog
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from signal_agent.ingestors.base import CompanyTarget, Ingestor
from signal_agent.ingestors.html_util import strip_html
from signal_agent.ingestors.keywords import classify_job
from signal_agent.schemas import NormalizedSignal

log = structlog.get_logger()

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseIngestor(Ingestor):
    source = "greenhouse"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        if not target.greenhouse_slug:
            return
        url = f"{API_BASE}/{target.greenhouse_slug}/jobs?content=true"
        client = await self._get_client()

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        ):
            with attempt:
                resp = await client.get(url)
                if resp.status_code == 404:
                    log.warning("greenhouse.slug_not_found", slug=target.greenhouse_slug)
                    return
                resp.raise_for_status()
                data = resp.json()

        for job in data.get("jobs", []):
            title = job.get("title", "")
            content = strip_html(job.get("content", ""))
            classification = classify_job(title, content)
            if classification is None:
                continue
            signal_type, matched = classification

            # Keep description excerpt short — full raw stays in raw_payload.
            excerpt = content[:500]
            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type=signal_type,
                source=self.source,
                source_url=job.get("absolute_url", url),
                signal_text=f"{title}\n\n{excerpt}",
                raw_payload={
                    "title": title,
                    "location": (job.get("location") or {}).get("name"),
                    "departments": [d.get("name") for d in job.get("departments", [])],
                    "updated_at": job.get("updated_at"),
                    "internal_job_id": job.get("id"),
                },
                matched_keywords=matched,
                suggested_tier_hint=1 if signal_type in {
                    "job_posting.ai_governance", "job_posting.ai_leadership"
                } else 2,
            )
