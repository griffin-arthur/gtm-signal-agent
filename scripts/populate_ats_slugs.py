"""Populate greenhouse_slug / lever_slug / ashby_slug for active ICP companies.

Why this exists:
  After the 372-company CSV import, ~74 active ICP companies had NO ingestor
  hooks at all — not one of greenhouse_slug, lever_slug, ashby_slug,
  workday_config, or ticker was set. That means only news / Reddit / HN
  could reach them. Job postings were effectively invisible for those
  companies.

Strategy:
  For each company lacking all three ATS slugs, generate a few candidate
  slug variations from the name and domain. Probe each ATS's public API.
  The first one that returns HTTP 200 wins.

  Candidate variations (based on observed patterns across 400+ ATS boards):
    - Lowercased name, no spaces / punct:  "Ramp Inc."      -> "ramp"
    - Lowercased domain token:             "ramp.com"       -> "ramp"
    - Name with hyphens:                   "Old Navy"       -> "old-navy"
    - Name with underscores:               "Bank of America" -> "bank_of_america"
    - Name + Inc/Corp stripped:            "Brex Inc"       -> "brex"

Probing is cheap (one HEAD/GET per candidate per ATS). With 3 ATS x
~5 variations x 74 companies = ~1100 requests max, parallelized 8-way.

Safety:
  - Never overwrites an existing slug. Only fills nulls.
  - Caches results in ~/.signal_agent/ats_slug_cache.json so re-runs
    don't re-probe URLs known to 404.
  - Respects each ATS's public rate limits (generous on Greenhouse/Lever,
    Ashby seems rate-limited around 10 req/s).

Usage:
    .venv/bin/python -m scripts.populate_ats_slugs
    .venv/bin/python -m scripts.populate_ats_slugs --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
from sqlalchemy import select, or_

from signal_agent.db import session_scope
from signal_agent.models import Company

log = structlog.get_logger()

CACHE_PATH = Path.home() / ".signal_agent" / "ats_slug_cache.json"
PROBE_CONCURRENCY = 8
REQUEST_TIMEOUT = 10.0

# Minimum signs-of-life for each ATS endpoint.
#   GET {greenhouse}/boards/{slug}/jobs?content=false -> 200 + {"jobs":[...]}
#   GET {lever}/postings/{slug}?mode=json            -> 200 + JSON array
#   GET {ashby}/posting-api/job-board/{slug}         -> 200 + {"jobs":[...]}

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

# Lazy default headers; ATSes don't require auth on their public board APIs.
HEADERS = {"User-Agent": "SignalAgent/0.1"}

# Suffix cleanup — same list used by populate_tickers for the name normalizer.
_SUFFIX_PATTERNS = [
    r"\s+inc\.?$",
    r"\s+incorporated$",
    r"\s+corp\.?$",
    r"\s+corporation$",
    r"\s+company$",
    r"\s+co\.?$",
    r"\s+llc\.?$",
    r"\s+ltd\.?$",
    r"\s+plc\.?$",
    r"\s+holdings?\s+inc\.?$",
    r"\s+holdings?$",
    r"\s+group\s+inc\.?$",
    r"\s+group$",
    r",\s+inc\.?$",
    r",\s+the$",
    r"\s*\(the\)$",
]
_SUFFIX_RE = re.compile("|".join(_SUFFIX_PATTERNS), re.IGNORECASE)


def _strip_suffix(s: str) -> str:
    for _ in range(3):
        new = _SUFFIX_RE.sub("", s).rstrip(",. ")
        if new == s:
            break
        s = new
    return s


def _candidate_slugs(name: str, domain: str) -> list[str]:
    """Produce a de-duplicated ordered list of slug candidates to probe.
    Order matters: most likely matches come first to avoid unnecessary calls."""
    candidates: list[str] = []

    def _push(s: str) -> None:
        s = s.strip().lower()
        if s and s not in candidates:
            candidates.append(s)

    # Domain token first — most likely to match when the brand = domain root
    if domain:
        _push(domain.split(".")[0])

    # Cleaned name, multiple separators
    cleaned = _strip_suffix(name.lower())
    compact = re.sub(r"[^a-z0-9]", "", cleaned)
    hyphens = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    underscores = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")

    _push(compact)
    _push(hyphens)
    _push(underscores)

    # Original name as-is (handles "IBM" / "AT&T" style)
    raw = re.sub(r"[^a-z0-9]", "", name.lower())
    _push(raw)

    return candidates


# ---- ATS probes -------------------------------------------------------------

@dataclass
class ProbeResult:
    ats: str
    slug: str
    ok: bool


def _probe_one(client: httpx.Client, url_template: str, slug: str, ats: str,
               cache: dict, cache_lock=None) -> ProbeResult:
    """Return a ProbeResult and update cache. Cached negatives short-circuit."""
    cache_key = f"{ats}:{slug}"
    if cache_key in cache:
        return ProbeResult(ats=ats, slug=slug, ok=cache[cache_key])

    url = url_template.format(slug=slug)
    try:
        resp = client.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        # 200 with some JSON body is success. 404 is a clean "no such board".
        # 403/429 are ambiguous — cache as failure but don't treat as authoritative
        # (rate limits could cause false negatives).
        ok = resp.status_code == 200 and len(resp.content) > 2
    except Exception:
        ok = False

    cache[cache_key] = ok
    return ProbeResult(ats=ats, slug=slug, ok=ok)


def _find_slug_for_company(client: httpx.Client, company: Company,
                           cache: dict) -> tuple[str | None, str | None, str | None]:
    """Return (greenhouse_slug, lever_slug, ashby_slug) — any / all may be None."""
    candidates = _candidate_slugs(company.name, company.domain or "")
    found: dict[str, str] = {}

    # Probe each ATS in parallel across candidates — first hit wins per ATS.
    for ats, url_template in [
        ("greenhouse", GREENHOUSE_URL),
        ("lever", LEVER_URL),
        ("ashby", ASHBY_URL),
    ]:
        if getattr(company, f"{ats}_slug"):
            continue  # already set — don't re-probe or overwrite
        for slug in candidates:
            r = _probe_one(client, url_template, slug, ats, cache)
            if r.ok:
                found[ats] = slug
                break

    return found.get("greenhouse"), found.get("lever"), found.get("ashby")


# ---- Cache ------------------------------------------------------------------

def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


# ---- Main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Find slugs but don't write to DB")
    parser.add_argument("--only-unreachable", action="store_true",
                        default=True,
                        help="Only probe companies with NO ingestor hook (default)")
    parser.add_argument("--all", dest="only_unreachable", action="store_false",
                        help="Probe every ICP company missing at least one of the three ATS slugs")
    args = parser.parse_args()

    cache = _load_cache()

    with session_scope() as s:
        query = select(Company).where(Company.is_icp.is_(True))
        if args.only_unreachable:
            # Companies with NO hook at all — these are the biggest wins
            query = query.where(
                Company.greenhouse_slug.is_(None),
                Company.lever_slug.is_(None),
                Company.ashby_slug.is_(None),
                Company.workday_config.is_(None),
                Company.ticker.is_(None),
            )
        else:
            # Companies missing at least one ATS slug
            query = query.where(or_(
                Company.greenhouse_slug.is_(None),
                Company.lever_slug.is_(None),
                Company.ashby_slug.is_(None),
            ))
        companies = s.execute(query).scalars().all()

    label = "unreachable" if args.only_unreachable else "missing-at-least-one-ats"
    print(f"[scan] probing {len(companies)} companies ({label})")
    print(f"[concurrency] {PROBE_CONCURRENCY} threads × 3 ATS × ~5 slug variants each\n")

    hits: list[tuple[Company, str | None, str | None, str | None]] = []

    with httpx.Client(http2=False) as client:
        with ThreadPoolExecutor(max_workers=PROBE_CONCURRENCY) as pool:
            futures = {
                pool.submit(_find_slug_for_company, client, c, cache): c
                for c in companies
            }
            for i, fut in enumerate(as_completed(futures), 1):
                c = futures[fut]
                gh, lv, ash = fut.result()
                if any([gh, lv, ash]):
                    hits.append((c, gh, lv, ash))
                    sources = ", ".join(
                        f"{k}={v}" for k, v in (("gh", gh), ("lever", lv), ("ashby", ash)) if v
                    )
                    print(f"  [{i:>3}/{len(companies)}] ✓ {c.name}  →  {sources}")
                else:
                    print(f"  [{i:>3}/{len(companies)}]   {c.name}  (no slug found)")

    _save_cache(cache)

    print(f"\n=== found hooks for {len(hits)}/{len(companies)} companies ===")

    if args.dry_run:
        print("(dry-run — DB not updated)")
        return 0

    # Apply the findings in a single session so the update is atomic-ish.
    with session_scope() as s:
        for c_snapshot, gh, lv, ash in hits:
            c = s.get(Company, c_snapshot.id)
            if c is None:
                continue
            if gh and not c.greenhouse_slug:
                c.greenhouse_slug = gh
            if lv and not c.lever_slug:
                c.lever_slug = lv
            if ash and not c.ashby_slug:
                c.ashby_slug = ash
    print(f"[db] wrote {len(hits)} company updates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
