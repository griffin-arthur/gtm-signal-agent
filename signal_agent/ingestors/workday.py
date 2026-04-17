"""Workday ATS ingestor.

Most large enterprises host careers on Workday. The customer-facing page is
heavily JS-rendered, BUT the underlying JSON search endpoint is accessible
without auth when you know the `tenant` and `portal` slug. The URL pattern is:

    https://<tenant>.<pod>.myworkdayjobs.com/wday/cxs/<tenant>/<portal>/jobs

We POST a lightweight search body and get structured results. No Playwright
needed for listing — keeps infra simple.

Seed format (CompanyTarget.workday config): the YAML entry gains three fields:

    workday_tenant: acme          # subdomain before .myworkdayjobs.com
    workday_pod: wd5              # usually wd1, wd3, wd5 — depends on region
    workday_portal: AcmeCareers   # path segment after the tenant

Fallback: if the JSON endpoint returns 403 (some tenants lock it down),
we can't ingest from that company without Playwright. For Phase 3 we log
and skip rather than ship a Playwright path that may be flaky locally.
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

# A wide-ish search that covers AI/ML/governance roles. Workday's search is
# full-text; we lean on our own classifier after fetching titles.
SEARCH_BODY = {
    "appliedFacets": {},
    "limit": 50,
    "offset": 0,
    "searchText": "AI OR ML OR governance OR risk",
}


class WorkdayIngestor(Ingestor):
    source = "workday"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "User-Agent": "Mozilla/5.0 (SignalAgent)",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator[NormalizedSignal]:
        cfg = getattr(target, "workday", None)
        if not cfg:
            return
        tenant, pod, portal = cfg.get("tenant"), cfg.get("pod", "wd5"), cfg.get("portal")
        if not tenant or not portal:
            return

        base = f"https://{tenant}.{pod}.myworkdayjobs.com"
        list_url = f"{base}/wday/cxs/{tenant}/{portal}/jobs"

        client = await self._get_client()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True,
            ):
                with attempt:
                    r = await client.post(list_url, json=SEARCH_BODY)
                    if r.status_code in (401, 403):
                        log.warning(
                            "workday.access_denied",
                            tenant=tenant, portal=portal, status=r.status_code,
                        )
                        return
                    r.raise_for_status()
                    data = r.json()
        except Exception as e:
            log.warning("workday.list_failed", tenant=tenant, portal=portal, err=str(e))
            return

        for posting in data.get("jobPostings", []):
            title = posting.get("title", "").strip()
            # Workday titles only — fetch the detail to get description.
            external_path = posting.get("externalPath") or ""
            if not external_path:
                continue
            detail_url = f"{base}/wday/cxs/{tenant}/{portal}{external_path}"
            public_url = f"{base}/en-US/{portal}{external_path}"

            description = ""
            try:
                d = await client.get(detail_url)
                if d.status_code == 200:
                    payload = d.json()
                    description = strip_html(
                        (payload.get("jobPostingInfo") or {}).get("jobDescription", "")
                    )
            except Exception:
                # Skip description — classifier still works off the title alone
                # for obvious Tier 1 roles like "Head of AI".
                description = ""

            classification = classify_job(title, description)
            if classification is None:
                continue
            signal_type, matched = classification

            yield NormalizedSignal(
                company_domain=target.domain,
                company_name=target.name,
                signal_type=signal_type,
                source=self.source,
                source_url=public_url,
                signal_text=f"{title}\n\n{description[:500]}",
                raw_payload={
                    "title": title,
                    "location": posting.get("locationsText"),
                    "posted_on": posting.get("postedOn"),
                    "tenant": tenant,
                    "portal": portal,
                },
                matched_keywords=matched,
                suggested_tier_hint=1 if signal_type in {
                    "job_posting.ai_governance", "job_posting.ai_leadership"
                } else 2,
            )
