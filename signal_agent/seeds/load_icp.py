"""Idempotent seed loader.

Usage:
    uv run python -m signal_agent.seeds.load_icp

Reads icp_companies.yaml + suppression.yaml and upserts rows. Safe to re-run.
"""
from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from sqlalchemy import select

from signal_agent.db import session_scope
from signal_agent.models import Company, Suppression

log = structlog.get_logger()

SEEDS_DIR = Path(__file__).parent


def load() -> None:
    with (SEEDS_DIR / "icp_companies.yaml").open() as f:
        companies = yaml.safe_load(f) or []
    with (SEEDS_DIR / "suppression.yaml").open() as f:
        suppressions = yaml.safe_load(f) or []

    with session_scope() as s:
        for entry in companies:
            existing = s.execute(
                select(Company).where(Company.domain == entry["domain"])
            ).scalar_one_or_none()
            if existing is None:
                s.add(Company(
                    domain=entry["domain"],
                    name=entry["name"],
                    greenhouse_slug=entry.get("greenhouse_slug"),
                    lever_slug=entry.get("lever_slug"),
                    ashby_slug=entry.get("ashby_slug"),
                    ticker=entry.get("ticker"),
                    workday_config=entry.get("workday"),
                    segment=entry.get("segment"),
                    target_tier=entry.get("target_tier", 2),
                    is_icp=True,
                ))
            else:
                existing.name = entry["name"]
                existing.greenhouse_slug = entry.get("greenhouse_slug")
                existing.lever_slug = entry.get("lever_slug")
                existing.ashby_slug = entry.get("ashby_slug")
                existing.ticker = entry.get("ticker")
                existing.workday_config = entry.get("workday")
                existing.segment = entry.get("segment")
                existing.target_tier = entry.get("target_tier", 2)
                existing.is_icp = True

        for entry in suppressions:
            exists = s.execute(
                select(Suppression).where(
                    Suppression.pattern == entry["pattern"],
                    Suppression.field == entry["field"],
                )
            ).scalar_one_or_none()
            if exists is None:
                s.add(Suppression(
                    pattern=entry["pattern"],
                    field=entry["field"],
                    reason=entry["reason"],
                ))

    log.info("seeds.loaded", companies=len(companies), suppressions=len(suppressions))


if __name__ == "__main__":
    load()
