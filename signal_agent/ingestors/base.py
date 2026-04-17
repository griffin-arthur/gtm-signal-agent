"""Base class for source ingestors.

Each source (Greenhouse, Lever, news, SEC, ...) implements `fetch_for_company`
returning an iterable of `NormalizedSignal`. Phase 1 only uses `CompanyTarget`
with board slugs; later sources will use domain/name/ticker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class CompanyTarget:
    """What an ingestor needs to know about a company to poll it."""

    company_id: int
    domain: str
    name: str
    greenhouse_slug: str | None = None
    lever_slug: str | None = None
    ashby_slug: str | None = None
    ticker: str | None = None                # SEC EDGAR
    workday: dict[str, str] | None = None    # {"tenant","pod","portal"}


class Ingestor(ABC):
    source: str  # "greenhouse" | "lever" | ...

    @abstractmethod
    async def fetch_for_company(self, target: CompanyTarget) -> AsyncIterator:
        """Yield NormalizedSignal objects for this company."""
        ...
        yield  # pragma: no cover  (for type checker)
