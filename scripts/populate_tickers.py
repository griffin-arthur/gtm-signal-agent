"""Populate the `ticker` column for ICP companies using the SEC's public
company_tickers.json file.

Usage:
    .venv/bin/python -m scripts.populate_tickers

What it does:
  1. Downloads SEC's authoritative ticker/name mapping (~10 MB, no auth).
  2. For each active ICP company without a ticker, tries to find a match
     in the SEC list via a normalized-name comparison + fuzzy fallback.
  3. Writes the ticker. Skips dry_run check; prints a report.

Conservative matching:
  - Exact name match (case + punctuation normalized) wins immediately
  - rapidfuzz `token_set_ratio` ≥ 90 accepted; 80–89 flagged for review
  - <80 left unset — the SEC mapping doesn't have every company (private
    companies never will)

The SEC file uses company names as registered with SEC — often differs from
common brand names (e.g. "Goldman Sachs Group Inc" vs "Goldman Sachs"). The
fuzzy path handles that case well.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

import httpx
import structlog
from rapidfuzz import fuzz
from sqlalchemy import select

from signal_agent.db import session_scope
from signal_agent.models import Company

log = structlog.get_logger()

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_HEADERS = {
    # SEC requires a descriptive UA on all requests.
    "User-Agent": "Arthur Signal Agent (ops@example.com)",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}

ACCEPT_THRESHOLD = 90   # fuzzy ratio — accept without review
REVIEW_THRESHOLD = 80   # 80–89 → flagged, not written

# Common corporate suffixes we strip before matching. Order matters: longer
# suffixes first so "Inc." doesn't nibble "incorporated".
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
    r"\s+&\s+co\.?$",
    r"\s+n\.?a\.?$",          # National Association (banks)
    r",\s+inc\.?$",
    r",\s+the$",
    r"\s*\(the\)$",
]
_SUFFIX_RE = re.compile("|".join(_SUFFIX_PATTERNS), re.IGNORECASE)
_AMPERSAND_WORDS = re.compile(r"\s+and\s+", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9\s]")


def _normalize(name: str) -> str:
    """Aggressively normalize for exact-match fallback. Matches both sides of
    a comparison so we can catch 'Goldman Sachs Group, Inc.' ≈ 'Goldman Sachs'."""
    s = name.lower().strip()
    s = _AMPERSAND_WORDS.sub(" & ", s)
    # Iteratively strip suffixes — "Goldman Sachs Group, Inc." → "Goldman Sachs"
    for _ in range(3):
        new = _SUFFIX_RE.sub("", s).rstrip(",. ")
        if new == s:
            break
        s = new
    s = _NON_WORD.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Match:
    name: str
    ticker: str
    score: float
    sec_name: str


def _fetch_sec_map() -> dict[str, dict]:
    """Return SEC's ticker/name dict. Rows: {'ticker': 'AAPL', 'title': 'Apple Inc'}."""
    with httpx.Client(timeout=30.0, headers=SEC_HEADERS) as client:
        resp = client.get(SEC_TICKERS_URL)
        resp.raise_for_status()
        return resp.json()


def _build_lookup(sec_rows: dict[str, dict]) -> tuple[dict[str, tuple[str, str]], list[tuple[str, str, str]]]:
    """Two views of the same data:
      - exact_lookup: normalized_name -> (ticker, original_sec_name)
      - fuzzy_pool:   list of (normalized_name, ticker, original_sec_name)
                      for rapidfuzz scanning
    """
    exact: dict[str, tuple[str, str]] = {}
    fuzzy: list[tuple[str, str, str]] = []
    for row in sec_rows.values():
        ticker = row["ticker"].upper()
        sec_name = row["title"]
        norm = _normalize(sec_name)
        if norm and norm not in exact:
            # First-come-first-served on ties — SEC's dict order follows CIK,
            # so larger/older companies win, which is usually what we want.
            exact[norm] = (ticker, sec_name)
        fuzzy.append((norm, ticker, sec_name))
    return exact, fuzzy


def _find_ticker(
    company_name: str, exact: dict[str, tuple[str, str]],
    fuzzy: list[tuple[str, str, str]],
) -> tuple[Match | None, Match | None]:
    """Return (accepted_match, review_match). accepted is >=90 fuzzy or
    normalized-exact; review is 80-89 for a human to confirm."""
    normed = _normalize(company_name)
    if not normed:
        return None, None

    # Path 1: exact normalized match
    if normed in exact:
        t, sec_name = exact[normed]
        return Match(company_name, t, 100.0, sec_name), None

    # Path 2: fuzzy scan — rapidfuzz is vectorized but calling per-company is
    # still plenty fast for our 214-company list.
    best: tuple[float, str, str] = (0.0, "", "")
    for norm, t, sec_name in fuzzy:
        score = fuzz.token_set_ratio(normed, norm)
        if score > best[0]:
            best = (score, t, sec_name)
            if score >= 100:
                break
    if best[0] >= ACCEPT_THRESHOLD:
        return Match(company_name, best[1], best[0], best[2]), None
    if best[0] >= REVIEW_THRESHOLD:
        return None, Match(company_name, best[1], best[0], best[2])
    return None, None


def main() -> int:
    log.info("ticker_populate.fetching_sec_map")
    sec_rows = _fetch_sec_map()
    log.info("ticker_populate.sec_map_loaded", rows=len(sec_rows))

    exact, fuzzy = _build_lookup(sec_rows)

    accepted: list[Match] = []
    review: list[Match] = []
    unmatched: list[str] = []

    with session_scope() as s:
        companies = s.execute(
            select(Company).where(Company.is_icp.is_(True), Company.ticker.is_(None))
        ).scalars().all()
        print(f"[scan] {len(companies)} active ICP companies without a ticker\n")

        for c in companies:
            accept, maybe = _find_ticker(c.name, exact, fuzzy)
            if accept is not None:
                c.ticker = accept.ticker
                accepted.append(accept)
            elif maybe is not None:
                review.append(maybe)
            else:
                unmatched.append(c.name)

    print(f"\n=== accepted: {len(accepted)} (written to DB) ===")
    for m in sorted(accepted, key=lambda x: x.name):
        print(f"  {m.ticker:<7}  {m.name}  ↔  {m.sec_name}  (score={m.score:.0f})")

    if review:
        print(f"\n=== review: {len(review)} (80-89 fuzzy, NOT written) ===")
        for m in sorted(review, key=lambda x: -x.score):
            print(f"  {m.ticker:<7}  {m.name}  ↔  {m.sec_name}  (score={m.score:.0f})")
        print("\nConfirm manually with:")
        print("  UPDATE companies SET ticker='XYZ' WHERE name='<Name>';")

    if unmatched:
        print(f"\n=== unmatched: {len(unmatched)} (private companies + noise) ===")
        for name in sorted(unmatched)[:20]:
            print(f"  {name}")
        if len(unmatched) > 20:
            print(f"  ... and {len(unmatched) - 20} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
