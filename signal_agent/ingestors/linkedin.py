"""LinkedIn executive-hire ingestor (SCAFFOLD — no API key = no signals).

LinkedIn doesn't offer a real-time hiring API. The three practical options
for monitoring exec-level AI hires are:

 1. **Coresignal** — https://coresignal.com  (structured LinkedIn data, paid)
 2. **BrightData** — https://brightdata.com  (proxy-based scraping, paid)
 3. **Clay** — https://clay.com  (workflow UI on top of the above, paid)

All return similar shapes: a list of people with title + company + linkedin_url
+ `started_at`. This module wraps a generic "hire event" provider so we can
swap vendors by implementing one adapter class.

To activate:
 1. Pick a vendor, get an API key.
 2. Set LINKEDIN_HIRES_API_KEY in .env and (optionally) LINKEDIN_HIRES_PROVIDER.
 3. Implement the vendor-specific adapter in `_fetch_recent_hires`.

Without a key, this ingestor returns nothing. The rubric already has a
`linkedin.exec_hire_ai` entry so scores work once signals start flowing.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import structlog

from signal_agent.ingestors.base import CompanyTarget, Ingestor
from signal_agent.schemas import NormalizedSignal

log = structlog.get_logger()

AI_LEADERSHIP_TITLES = [
    "chief ai officer", "head of ai", "vp of ai", "vp, ai",
    "chief artificial intelligence officer",
    "head of machine learning", "head of ml", "head of mlops",
    "vp of machine learning", "director of ai",
]


def _is_ai_leadership(title: str) -> bool:
    t = title.lower()
    return any(needle in t for needle in AI_LEADERSHIP_TITLES)


class LinkedInHiresIngestor(Ingestor):
    """Non-functional until a vendor API key is configured."""
    source = "linkedin"

    def __init__(self) -> None:
        self._api_key = os.environ.get("LINKEDIN_HIRES_API_KEY", "")
        self._provider = os.environ.get("LINKEDIN_HIRES_PROVIDER", "")

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        if not self._api_key:
            # Log once per process would be ideal, but this is cheap.
            return

        try:
            hires = await self._fetch_recent_hires(target)
        except NotImplementedError:
            log.warning("linkedin.provider_not_wired", provider=self._provider)
            return
        except Exception as e:
            log.warning("linkedin.fetch_failed", company=target.name, err=str(e))
            return

        for hire in hires:
            title = hire.get("title", "")
            if not _is_ai_leadership(title):
                continue
            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type="linkedin.exec_hire_ai",
                source=self.source,
                source_url=hire.get("linkedin_url", f"linkedin://{target.domain}"),
                signal_text=(
                    f"{hire.get('name', 'Unknown')} joined {target.name} as {title}"
                    + (f" (prev: {hire.get('previous_company', '')})"
                       if hire.get("previous_company") else "")
                ),
                raw_payload=hire,
                detected_at=self._parse_start(hire.get("started_at")),
                matched_keywords=[title.lower()],
                suggested_tier_hint=1,
            )

    async def _fetch_recent_hires(self, target: CompanyTarget) -> list[dict]:
        """Adapter for whichever vendor you plug in. Returns a list of dicts:
            {name, title, linkedin_url, started_at, previous_company}
        """
        raise NotImplementedError(
            "Wire a vendor adapter (Coresignal / BrightData / Clay) here."
        )

    @staticmethod
    def _parse_start(s: str | None) -> datetime:
        if not s:
            return datetime.now(timezone.utc)
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
