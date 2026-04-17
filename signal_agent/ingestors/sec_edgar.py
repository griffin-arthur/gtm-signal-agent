"""SEC EDGAR ingestor.

Scans recent 10-K / 10-Q / 8-K filings for ICP companies that have a ticker,
looking for AI/ML-related keywords in MD&A and Risk Factors sections. When the
company discusses AI in regulatory filings, that's a strong Tier 2 signal
(formal, public, executive-signed-off acknowledgement of AI in the business).

Data sources (all free, no API key, just a User-Agent):
 - https://www.sec.gov/files/company_tickers.json  — ticker → CIK map
 - https://data.sec.gov/submissions/CIK{cik}.json  — recent filings for a CIK
 - https://www.sec.gov/Archives/edgar/data/...     — filing documents

Rate limit: SEC asks for <= 10 req/s and requires a descriptive User-Agent.
We run this at daily cadence against a small ICP list so the budget is plenty.

We do NOT fetch the full filing body — that's megabytes. Instead we fetch the
filing's index page, find the primary document (.htm), and stream only the first
~200KB which is where MD&A and Risk Factors sit for most filers. Good enough for
a keyword scan; precise parsing would need `edgartools` or a structured parser.
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from signal_agent.ingestors.base import CompanyTarget, Ingestor
from signal_agent.ingestors.html_util import strip_html
from signal_agent.schemas import NormalizedSignal

log = structlog.get_logger()

# SEC requires a descriptive UA. Update email when this goes to production.
SEC_HEADERS = {
    "User-Agent": "Arthur Signal Agent (dev contact@example.com)",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}
DATA_SEC_HEADERS = {**SEC_HEADERS, "Host": "data.sec.gov"}

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FILING_INDEX_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

FILING_TYPES = {"10-K", "10-Q", "8-K"}
MAX_AGE_DAYS = 120          # a full quarter's lookback
BODY_FETCH_BYTES = 200_000  # cap per filing

# Keyword set for SEC filings (source: docs/icp.md §8 regulatory anchors +
# §9 Arthur-specific phrases). Higher bar than news — filings are long and
# noisy, so phrases must indicate *substantive* AI disclosure.
FILING_KEYWORDS = [
    # Core AI terminology
    "generative ai",
    "generative artificial intelligence",
    "agentic ai",
    "ai agent",
    "large language model",
    "llm",
    # Governance / risk / assurance
    "ai governance",
    "ai risk",
    "ai risk management",
    "model risk",
    "model risk management",
    "ai assurance",
    "ai audit",
    "ai incident",
    "responsible ai",
    "algorithmic bias",
    "artificial intelligence governance",
    # Regulatory frameworks — these are Arthur's strongest urgency hooks
    "eu ai act",
    "nist ai rmf",
    "iso 42001",
    "sr 11-7",
    "occ guidance",
    "finra 2026",
    "sec exam priorities",
    "hti-1",
    "hipaa ai",
    # Platforms that accompany Arthur's pitch
    "aws bedrock", "google vertex", "agent foundry",
]


# Module-level ticker→CIK cache. Rebuilt on first call per process.
_ticker_cache: dict[str, str] | None = None


async def _get_ticker_cik_map(client: httpx.AsyncClient) -> dict[str, str]:
    global _ticker_cache
    if _ticker_cache is not None:
        return _ticker_cache
    resp = await client.get(TICKER_MAP_URL, headers=SEC_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    _ticker_cache = {row["ticker"].upper(): f"{row['cik_str']:010d}" for row in data.values()}
    return _ticker_cache


def _extract_relevant_excerpt(text: str, keyword: str, window: int = 240) -> str:
    """Return a short excerpt around the first case-insensitive match, else empty."""
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window // 2)
    end = min(len(text), idx + len(keyword) + window // 2)
    excerpt = text[start:end]
    # Tidy leading/trailing partial words.
    excerpt = re.sub(r"^\S*\s", "", excerpt, count=1)
    excerpt = re.sub(r"\s\S*$", "", excerpt, count=1)
    return excerpt.strip()


class SecEdgarIngestor(Ingestor):
    source = "sec_edgar"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        if not target.ticker:
            return
        client = await self._get_client()

        try:
            ticker_map = await _get_ticker_cik_map(client)
        except Exception as e:
            log.warning("sec.ticker_map_failed", err=str(e))
            return

        cik = ticker_map.get(target.ticker.upper())
        if cik is None:
            log.warning("sec.ticker_not_found", ticker=target.ticker)
            return

        # Fetch the submissions doc — lists recent filings.
        sub_url = SUBMISSIONS_URL.format(cik=int(cik))
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                reraise=True,
            ):
                with attempt:
                    r = await client.get(sub_url, headers=DATA_SEC_HEADERS)
                    r.raise_for_status()
                    subs = r.json()
        except Exception as e:
            log.warning("sec.submissions_failed", cik=cik, err=str(e))
            return

        recent = subs.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])

        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

        for form, date_str, accession, doc in zip(forms, dates, accessions, primary_docs):
            if form not in FILING_TYPES:
                continue
            try:
                filing_date = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if filing_date < cutoff:
                continue
            if not doc:
                continue

            accession_nodash = accession.replace("-", "")
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{accession_nodash}/{doc}"
            )

            try:
                r = await client.get(doc_url, headers=SEC_HEADERS)
                r.raise_for_status()
                raw = r.text[:BODY_FETCH_BYTES]
            except Exception as e:
                log.warning("sec.doc_fetch_failed", url=doc_url, err=str(e))
                continue

            text = strip_html(raw)
            text_lower = text.lower()
            # Word-boundary match so short keywords like "llm" don't match
            # inside unrelated words or company names (e.g., "Lucie, LLC").
            matched = [
                kw for kw in FILING_KEYWORDS
                if re.search(rf"\b{re.escape(kw)}\b", text_lower)
            ]
            if not matched:
                continue

            excerpt = _extract_relevant_excerpt(text, matched[0]) or text[:300]

            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type="filing.sec_ai_mention",
                source=self.source,
                source_url=doc_url,
                signal_text=f"{form} ({date_str}): AI mention — {matched[0]}\n\n{excerpt}",
                raw_payload={
                    "form": form,
                    "filing_date": date_str,
                    "accession": accession,
                    "ticker": target.ticker,
                    "matched_keywords": matched,
                    "excerpt": excerpt,
                },
                detected_at=filing_date,
                matched_keywords=matched,
                suggested_tier_hint=2,
            )
