"""Lever job postings ingestor.

Public API: `https://api.lever.co/v0/postings/{slug}?mode=json`
Returns list of postings with descriptionPlain already stripped.
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

API_BASE = "https://api.lever.co/v0/postings"


class LeverIngestor(Ingestor):
    source = "lever"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        if not target.lever_slug:
            return
        url = f"{API_BASE}/{target.lever_slug}?mode=json"
        client = await self._get_client()

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            reraise=True,
        ):
            with attempt:
                resp = await client.get(url)
                if resp.status_code == 404:
                    log.warning("lever.slug_not_found", slug=target.lever_slug)
                    return
                resp.raise_for_status()
                postings = resp.json()

        for posting in postings:
            title = posting.get("text", "")
            description = posting.get("descriptionPlain") or posting.get("description", "")
            classification = classify_job(title, description)
            if classification is None:
                continue
            signal_type, matched = classification

            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type=signal_type,
                source=self.source,
                source_url=posting.get("hostedUrl", url),
                signal_text=f"{title}\n\n{description[:500]}",
                raw_payload={
                    "title": title,
                    "location": (posting.get("categories") or {}).get("location"),
                    "team": (posting.get("categories") or {}).get("team"),
                    "commitment": (posting.get("categories") or {}).get("commitment"),
                    "created_at": posting.get("createdAt"),
                    "internal_job_id": posting.get("id"),
                },
                matched_keywords=matched,
                suggested_tier_hint=1 if signal_type in {
                    "job_posting.ai_governance", "job_posting.ai_leadership"
                } else 2,
            )
