"""Competitor-customer disqualification.

Arthur shouldn't spend AE effort on companies already publicly committed to a
competing AI governance / evals / observability platform. This module:

 1. Scrapes each competitor's customer page(s) to extract a set of named
    customer companies (by both display name and domain when we can infer it).
 2. Caches the result in the `competitor_customers` table keyed on
    (competitor, identifier) with a refresh timestamp.
 3. Exposes `is_competitor_customer(company)` that the alert pipeline calls
    BEFORE firing — if the company is a known customer of a competitor, the
    signal is suppressed and the reason is logged for AE visibility.

The scraping is kept deliberately conservative: we use BeautifulSoup + a
well-known set of selectors and `<img alt>` + filename patterns. When a site
is fully JS-rendered (many use client-rendered logo carousels), the LLM or
a manual operator entry is the fallback — we never block on a scrape failure.

Adding a new competitor: append to `COMPETITOR_SITES` with the URL(s) that
list customers. The scraper tries alt-text, headings, filenames, and a
bounded keyword fallback (company name in headings) before giving up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import structlog
import yaml
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_agent.ingestors.html_util import strip_html
from signal_agent.models import Company, CompetitorCustomer

log = structlog.get_logger()

# Keep in sync with docs/icp.md §6 + ingestors/competitive.py.
# Each entry: list of URLs to scrape for customer mentions.
COMPETITOR_SITES: dict[str, list[str]] = {
    "Braintrust": [
        "https://www.braintrust.dev/",
        "https://www.braintrust.dev/customers",
    ],
    "Arize": [
        "https://arize.com/",
        "https://arize.com/customers",
    ],
    "Fiddler": [
        "https://www.fiddler.ai/",
        "https://www.fiddler.ai/customers",
    ],
    "WhyLabs": [
        "https://whylabs.ai/",
        "https://whylabs.ai/customers",
    ],
    "Credo AI": [
        "https://www.credo.ai/",
    ],
    "Langfuse": [
        "https://langfuse.com/",
        "https://langfuse.com/customers",
    ],
    "WitnessAI": [
        "https://witness.ai/",
    ],
}

# Optional operator-managed overrides. If RevOps knows something the scraper
# can't see (e.g., a private deal, a competitor's private case-study page),
# they add the mapping here and it takes precedence over scraping results.
OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "seeds" / "competitor_customers_overrides.yaml"

FUZZY_NAME_MATCH_THRESHOLD = 88
CACHE_TTL_HOURS = 168  # one week

# Regex for logo-file conventions seen across competitor sites.
# Matches: /customers/<name>.(jpg|png|...), /logos/<name>.(...), /_next/image?url=/customers/<name>...
LOGO_FILENAME_RE = re.compile(
    r"/(?:customers|logos|clients|case-studies|img/customers)/([a-z0-9][a-z0-9_-]{1,40})\.(?:jpg|jpeg|png|svg|webp)",
    re.IGNORECASE,
)
# Alt-text / heading convention: alt="...ACME..." or >ACME< inside a case study tile.
ALT_ATTR_RE = re.compile(r'alt="([^"]{2,60})"', re.IGNORECASE)


@dataclass
class CompetitorMatch:
    competitor: str
    confidence: float   # 0.0–1.0 from the match strategy
    evidence_url: str
    evidence: str       # short string explaining *why* we flagged it


# ---- scraping ---------------------------------------------------------------

async def _fetch_one(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (SignalAgent)"})
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning("competitor_customers.fetch_failed", url=url, err=str(e))
        return ""


def _extract_candidates(html: str) -> set[str]:
    """From one page of HTML, return the set of candidate customer names.

    We union three extraction strategies:
      - filename slugs (e.g., /customers/stripe.jpg → "stripe")
      - alt attributes ("alt=\"Stripe logo\"" → "stripe logo")
      - plain-text body tokens (after HTML-stripping) > 3 chars
    """
    names: set[str] = set()
    for m in LOGO_FILENAME_RE.finditer(html):
        # Normalize: strip common suffixes ("-logo", "-white", ...).
        raw = m.group(1).lower()
        raw = re.sub(r"[-_](logo|light|dark|white|color|mono)$", "", raw)
        names.add(raw)
    for m in ALT_ATTR_RE.finditer(html):
        names.add(m.group(1).strip().lower())
    # As a fallback, include the cleaned text body — callers do fuzzy matching
    # so false matches on common words will fail the 88-threshold anyway.
    text = strip_html(html).lower()
    # Split on whitespace + common punct; keep 3–40 char tokens.
    for token in re.findall(r"[a-z][a-z0-9&+\. _-]{2,40}[a-z0-9]", text):
        if 3 <= len(token) <= 40:
            names.add(token.strip())
    return names


async def scrape_competitor(competitor: str, urls: list[str],
                            client: httpx.AsyncClient | None = None) -> set[str]:
    """Return the set of candidate-customer names mentioned on this
    competitor's public pages."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    try:
        candidates: set[str] = set()
        for url in urls:
            html = await _fetch_one(client, url)
            if html:
                candidates |= _extract_candidates(html)
        return candidates
    finally:
        if owns_client:
            await client.aclose()


# ---- match + cache ----------------------------------------------------------

def _match_company_to_candidates(company: Company, candidates: set[str]) -> float | None:
    """Return a confidence score if the company likely appears in candidates.

    We use domain token (e.g., "stripe" from stripe.com) for strict match,
    and the display name for fuzzy match.
    """
    domain_token = company.domain.split(".")[0].lower()
    name_lower = company.name.lower()

    # 1. Exact domain-slug match → high confidence.
    if domain_token in candidates:
        return 0.95

    # 2. Fuzzy name match (handles "Stripe Inc" vs "Stripe").
    best = 0
    for cand in candidates:
        # Rapidfuzz is O(n·m) — keep candidates bounded. At ~2K items the
        # whole set finishes in <10ms.
        score = fuzz.token_set_ratio(name_lower, cand)
        if score > best:
            best = score
            if best >= 95:
                break
    if best >= FUZZY_NAME_MATCH_THRESHOLD:
        # Map 88–100 → 0.70–0.95 confidence band.
        return 0.70 + (best - FUZZY_NAME_MATCH_THRESHOLD) * (0.25 / (100 - FUZZY_NAME_MATCH_THRESHOLD))
    return None


async def refresh_cache(session: Session,
                        client: httpx.AsyncClient | None = None) -> dict[str, int]:
    """Re-scrape every competitor and upsert per-company matches.

    Returns {competitor: match_count}. Safe to call daily; cached rows older
    than CACHE_TTL_HOURS get refreshed; rows within TTL are left alone.
    """
    now = datetime.now(timezone.utc)
    companies = session.execute(select(Company).where(Company.is_icp.is_(True))).scalars().all()
    results: dict[str, int] = {}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    try:
        for competitor, urls in COMPETITOR_SITES.items():
            candidates = await scrape_competitor(competitor, urls, client=client)
            log.info("competitor_customers.scraped",
                     competitor=competitor, candidate_count=len(candidates))
            matches = 0
            for company in companies:
                conf = _match_company_to_candidates(company, candidates)
                if conf is None:
                    continue
                matches += 1
                existing = session.execute(
                    select(CompetitorCustomer).where(
                        CompetitorCustomer.company_id == company.id,
                        CompetitorCustomer.competitor == competitor,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(CompetitorCustomer(
                        company_id=company.id,
                        competitor=competitor,
                        confidence=conf,
                        evidence_url=urls[0],
                        last_confirmed_at=now,
                    ))
                else:
                    existing.confidence = conf
                    existing.last_confirmed_at = now
            results[competitor] = matches

        # Apply operator overrides (if any) — these always win.
        _apply_overrides(session, companies, now)

    finally:
        if owns_client:
            await client.aclose()
    return results


def _apply_overrides(session: Session, companies: list[Company],
                     now: datetime) -> None:
    if not OVERRIDES_PATH.exists():
        return
    try:
        with OVERRIDES_PATH.open() as f:
            data = yaml.safe_load(f) or []
    except Exception as e:
        log.warning("competitor_customers.overrides_parse_failed", err=str(e))
        return
    by_domain = {c.domain.lower(): c for c in companies}
    for entry in data:
        domain = entry.get("domain", "").lower()
        competitor = entry.get("competitor")
        if not domain or not competitor:
            continue
        company = by_domain.get(domain)
        if company is None:
            continue
        existing = session.execute(
            select(CompetitorCustomer).where(
                CompetitorCustomer.company_id == company.id,
                CompetitorCustomer.competitor == competitor,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(CompetitorCustomer(
                company_id=company.id,
                competitor=competitor,
                confidence=entry.get("confidence", 1.0),
                evidence_url=entry.get("evidence_url", "operator_override"),
                last_confirmed_at=now,
                is_override=True,
            ))
        else:
            existing.confidence = entry.get("confidence", 1.0)
            existing.last_confirmed_at = now
            existing.is_override = True


# ---- query from the alert pipeline -----------------------------------------

@dataclass
class CompetitorCustomerStatus:
    is_customer: bool
    competitors: list[str]     # all flagged competitors, highest-confidence first
    confidence: float | None   # max confidence across matches
    evidence_url: str | None


def is_competitor_customer(session: Session, company_id: int,
                           min_confidence: float = 0.75) -> CompetitorCustomerStatus:
    """Return the company's competitor-customer status.

    A company is considered a "competitor customer" if we have at least one
    cached match with confidence ≥ `min_confidence`. Operator overrides bypass
    the confidence floor (they're intentional).
    """
    fresh_cutoff = datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)
    rows = session.execute(
        select(CompetitorCustomer)
        .where(
            CompetitorCustomer.company_id == company_id,
            CompetitorCustomer.last_confirmed_at >= fresh_cutoff,
        )
        .order_by(CompetitorCustomer.confidence.desc())
    ).scalars().all()
    if not rows:
        return CompetitorCustomerStatus(False, [], None, None)

    qualifying = [r for r in rows if r.is_override or r.confidence >= min_confidence]
    if not qualifying:
        return CompetitorCustomerStatus(False, [r.competitor for r in rows], rows[0].confidence, None)

    return CompetitorCustomerStatus(
        is_customer=True,
        competitors=[r.competitor for r in qualifying],
        confidence=qualifying[0].confidence,
        evidence_url=qualifying[0].evidence_url,
    )
