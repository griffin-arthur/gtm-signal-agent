"""Idempotent seed loader.

Usage:
    uv run python -m signal_agent.seeds.load_icp

Reads three seed files and upserts rows. Safe to re-run any time:
  - icp_companies.yaml      — bootstrap ICP accounts (always_icp=true on load)
  - suppression.yaml        — disqualification patterns
  - icp_drops.yaml          — companies explicitly excluded from polling;
                              sets is_icp=false AFTER the main load, so a
                              company that appears in both files ends up
                              excluded (drops win).
"""
from __future__ import annotations

from pathlib import Path

import structlog
import yaml
from sqlalchemy import select, update

from signal_agent.db import session_scope
from signal_agent.models import Company, Suppression

log = structlog.get_logger()

SEEDS_DIR = Path(__file__).parent


def load() -> None:
    with (SEEDS_DIR / "icp_companies.yaml").open() as f:
        companies = yaml.safe_load(f) or []
    with (SEEDS_DIR / "suppression.yaml").open() as f:
        suppressions = yaml.safe_load(f) or []

    drops_path = SEEDS_DIR / "icp_drops.yaml"
    drop_names: list[str] = []
    if drops_path.exists():
        with drops_path.open() as f:
            data = yaml.safe_load(f) or {}
        drop_names = data.get("drops", []) or []

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

        # Apply the drops LAST, so any bootstrap company that also appears
        # in icp_drops.yaml ends up excluded. Row count is tracked separately
        # for observability.
        dropped = 0
        if drop_names:
            dropped = s.execute(
                update(Company)
                .where(Company.name.in_(drop_names))
                .values(is_icp=False)
            ).rowcount or 0

    log.info(
        "seeds.loaded",
        companies=len(companies),
        suppressions=len(suppressions),
        drops_file_size=len(drop_names),
        drops_applied=dropped,
    )


if __name__ == "__main__":
    load()
