"""Competitive intel ingestor — Hacker News + Reddit.

Fires when a public post mentions an ICP company *alongside* an Arthur
competitor (Arize, Fiddler, WhyLabs, Credo AI). That co-occurrence is an
in-market signal: someone is comparing tools or evaluating the space.

Data sources:
 - HN Algolia API: https://hn.algolia.com/api/v1/search_by_date?query=...
   Free, no auth, returns titles + comments. Rate limit ≥10 req/s.
 - Reddit JSON: https://www.reddit.com/search.json?q=...&sort=new&restrict_sr=on
   Free, no auth if we use a descriptive UA and stay under ~60 req/min.

Query shape: for each competitor, issue one search per ICP company. The
response text is checked to ensure BOTH names appear within a bounded
distance (we don't want two unrelated posts about the same keyword set).
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from signal_agent.ingestors.base import CompanyTarget, Ingestor
from signal_agent.schemas import NormalizedSignal

log = structlog.get_logger()

# From docs/icp.md §6. When edited, keep in sync with the ICP doc.
COMPETITORS = [
    # Governance / system of record
    "Credo AI", "ModelOp",
    # AI security / runtime enforcement
    "WitnessAI", "Pillar Security", "Bifrost",
    # Cloud-native AI platforms (governance-adjacent)
    "Agentforce", "Dataiku", "DataRobot",
    # Observability
    "Arize", "Langfuse", "Braintrust", "Fiddler", "WhyLabs",
    # Data discovery / DLP
    "BigID", "OneTrust",
]
MAX_AGE_DAYS = 30
CO_OCCURRENCE_WINDOW_CHARS = 600  # ICP + competitor must appear within this span

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
REDDIT_API = "https://www.reddit.com/search.json"
REDDIT_UA = "SignalAgent/0.1 (contact: signal-agent@example.com)"


def _co_occurs(text: str, a: str, b: str, window: int = CO_OCCURRENCE_WINDOW_CHARS) -> bool:
    """Return True if both terms appear within `window` chars of each other."""
    t = text.lower()
    idx_a = t.find(a.lower())
    idx_b = t.find(b.lower())
    if idx_a < 0 or idx_b < 0:
        return False
    return abs(idx_a - idx_b) <= window


class CompetitiveIngestor(Ingestor):
    source = "competitive"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=20.0, headers={"User-Agent": REDDIT_UA},
            )
        return self._client

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        client = await self._get_client()
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

        # For each competitor, query the two platforms. Deduplicate by URL.
        seen_urls: set[str] = set()
        for competitor in COMPETITORS:
            hn_q = f'"{target.name}" "{competitor}"'
            reddit_q = f'"{target.name}" AND "{competitor}"'

            async for item in self._search_hn(client, hn_q, target, competitor, cutoff):
                if item.source_url in seen_urls:
                    continue
                seen_urls.add(item.source_url)
                yield item

            async for item in self._search_reddit(client, reddit_q, target, competitor, cutoff):
                if item.source_url in seen_urls:
                    continue
                seen_urls.add(item.source_url)
                yield item

    async def _search_hn(
        self, client: httpx.AsyncClient, query: str,
        target: CompanyTarget, competitor: str, cutoff: datetime,
    ) -> AsyncIterator[NormalizedSignal]:
        params = {"query": query, "tags": "(story,comment)", "hitsPerPage": "20"}
        url = f"{HN_API}?{urllib.parse.urlencode(params)}"
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True,
            ):
                with attempt:
                    r = await client.get(url)
                    r.raise_for_status()
                    data = r.json()
        except Exception as e:
            log.warning("competitive.hn_failed", err=str(e))
            return

        for hit in data.get("hits", []):
            created = datetime.fromtimestamp(hit.get("created_at_i", 0), tz=timezone.utc)
            if created < cutoff:
                continue
            title = hit.get("title") or hit.get("story_title") or ""
            body = hit.get("story_text") or hit.get("comment_text") or ""
            body = re.sub(r"<[^>]+>", " ", body)  # strip HN's HTML in comments
            combined = f"{title}\n{body}"
            if not _co_occurs(combined, target.name, competitor):
                continue
            object_id = hit.get("objectID", "")
            link = f"https://news.ycombinator.com/item?id={object_id}"
            excerpt = combined.strip()[:400]
            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type="competitive.mentioned_with",
                source="hn",
                source_url=link,
                signal_text=f"HN: {title}\n\n{excerpt}",
                raw_payload={
                    "platform": "hn",
                    "competitor": competitor,
                    "title": title,
                    "body_excerpt": excerpt,
                    "hn_object_id": object_id,
                },
                detected_at=created,
                matched_keywords=[target.name, competitor],
                suggested_tier_hint=3,
            )

    async def _search_reddit(
        self, client: httpx.AsyncClient, query: str,
        target: CompanyTarget, competitor: str, cutoff: datetime,
    ) -> AsyncIterator[NormalizedSignal]:
        params = {"q": query, "sort": "new", "limit": "20", "t": "month"}
        url = f"{REDDIT_API}?{urllib.parse.urlencode(params)}"
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True,
            ):
                with attempt:
                    r = await client.get(url)
                    r.raise_for_status()
                    data = r.json()
        except Exception as e:
            log.warning("competitive.reddit_failed", err=str(e))
            return

        for child in data.get("data", {}).get("children", []):
            p = child.get("data", {})
            created = datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)
            if created < cutoff:
                continue
            title = p.get("title", "")
            body = p.get("selftext", "")
            combined = f"{title}\n{body}"
            if not _co_occurs(combined, target.name, competitor):
                continue
            permalink = "https://reddit.com" + p.get("permalink", "")
            excerpt = body.strip()[:400] if body else ""
            subreddit = p.get("subreddit_name_prefixed", "")

            # Dedup via title hash in case reddit returns duplicates on paging
            _ = hashlib.sha1(f"{title}|{created.isoformat()}".encode()).hexdigest()[:12]

            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type="competitive.mentioned_with",
                source="reddit",
                source_url=permalink,
                signal_text=f"Reddit ({subreddit}): {title}\n\n{excerpt}",
                raw_payload={
                    "platform": "reddit",
                    "competitor": competitor,
                    "subreddit": subreddit,
                    "title": title,
                    "body_excerpt": excerpt,
                    "author": p.get("author"),
                    "score": p.get("score"),
                    "num_comments": p.get("num_comments"),
                },
                detected_at=created,
                matched_keywords=[target.name, competitor],
                suggested_tier_hint=3,
            )
