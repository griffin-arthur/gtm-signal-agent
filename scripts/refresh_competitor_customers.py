"""Refresh the competitor-customer cache.

Scrapes each competitor's public customer pages, matches against ICP accounts,
and writes results to the `competitor_customers` table.

Usage:
    .venv/bin/python -m scripts.refresh_competitor_customers

Run daily (or before each big ICP review). Cache TTL is 7 days, so stale
entries drop out if a competitor removes a logo.
"""
from __future__ import annotations

import asyncio
import sys

from signal_agent.db import session_scope
from signal_agent.quality import competitor_customers


async def main() -> int:
    with session_scope() as s:
        results = await competitor_customers.refresh_cache(s)
    print("=== Competitor-customer refresh ===")
    for competitor, count in sorted(results.items(), key=lambda kv: -kv[1]):
        print(f"  {competitor}: {count} matches")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
