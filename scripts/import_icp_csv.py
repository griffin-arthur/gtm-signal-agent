"""Bulk-import ICP companies from a CSV.

Reads a CSV with at minimum a `Company` (or `name`) column and upserts each
row into the `companies` table. Domains are resolved via Claude when missing.

Usage:
    .venv/bin/python -m scripts.import_icp_csv <path-to-csv>
    .venv/bin/python -m scripts.import_icp_csv <path> --dry-run    # no DB writes
    .venv/bin/python -m scripts.import_icp_csv <path> --tier 2     # override default tier
    .venv/bin/python -m scripts.import_icp_csv <path> --segment B  # override default segment
    .venv/bin/python -m scripts.import_icp_csv <path> --skip-resolve  # leave domain blank

Design notes:
 - **Idempotent.** Re-running the same CSV upserts by name+domain, so adding
   companies later is safe. Existing target_tier/segment are NOT overwritten
   once set — we only fill them in on first insert.
 - **Domain resolution caches.** Claude lookups are cached to
   `~/.signal_agent/domain_cache.json` so re-runs don't repeat $ and API calls.
 - **Concurrent resolution.** Up to 8 Claude calls in parallel. 372 companies
   at ~1s each → ~45s to resolve the full sheet.
 - **Manual review bucket.** Any company whose domain Claude couldn't confirm
   (low confidence or blank) is inserted with `is_icp=false` + a `needs_review`
   marker in the name. Nothing polls them until RevOps confirms.
 - **CSV format flexibility.** Accepts `Company`, `Name`, `Company Name`,
   `company_name` as the name column. BOM-tolerant. CRLF-tolerant.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import structlog
from anthropic import Anthropic
from anthropic._exceptions import RateLimitError
from sqlalchemy import select

from signal_agent.config import settings
from signal_agent.db import session_scope
from signal_agent.models import Company

log = structlog.get_logger()

CACHE_PATH = Path.home() / ".signal_agent" / "domain_cache.json"
RESOLVE_CONCURRENCY = 3   # conservative — Anthropic rate-limits aggressively on cheap tiers
RESOLVE_MAX_RETRIES = 5
NAME_COLUMN_CANDIDATES = ("Company", "Name", "Company Name", "company_name", "company", "name")

SYSTEM_PROMPT = """\
You resolve US/global company names to their primary corporate web domain.

Given a company name, return ONLY a JSON object:
{
  "domain": "example.com",                  // lowercase, no protocol, no www
  "confidence": 0.0-1.0,
  "ambiguous": boolean,                     // true if multiple well-known companies share this name
  "notes": "brief explanation if ambiguous or uncertain"
}

Rules:
- Return the canonical company homepage domain, not a subdomain.
- If the company name contains legal suffixes (Inc., Corp., LLC), strip them for matching.
- If the company is publicly traded, the ticker's parent company domain is preferred.
- If you're not confident (score < 0.7), return your best guess but set ambiguous=true.
- Never wrap the JSON in code fences.
"""


@dataclass
class ResolvedCompany:
    name: str
    domain: str | None
    confidence: float
    ambiguous: bool
    notes: str


def _load_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _read_names(csv_path: Path) -> list[str]:
    """Extract the Company column from the CSV, handling BOM + quoted rows."""
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        name_col = next(
            (c for c in NAME_COLUMN_CANDIDATES if c in reader.fieldnames), None
        )
        if name_col is None:
            # Fallback: single-column CSV with no proper header.
            f.seek(0)
            rows = [row[0].strip() for row in csv.reader(f) if row and row[0].strip()]
            # Skip the header if it looks like one.
            if rows and rows[0].lower() in {c.lower() for c in NAME_COLUMN_CANDIDATES}:
                rows = rows[1:]
            return [r for r in rows if r]
        return [row[name_col].strip() for row in reader if row.get(name_col, "").strip()]


def _resolve_one(client: Anthropic, name: str, cache: dict[str, dict]) -> ResolvedCompany:
    cached = cache.get(name.lower())
    # Only treat SUCCESSFUL resolutions as cached. Failures (no domain, or
    # explicit error notes) should be retried on re-runs, not persisted.
    if cached and cached.get("domain") and not cached.get("notes", "").startswith("error:"):
        return ResolvedCompany(
            name=name,
            domain=cached.get("domain"),
            confidence=cached.get("confidence", 0.0),
            ambiguous=cached.get("ambiguous", False),
            notes=cached.get("notes", "from cache"),
        )

    # Exponential backoff on rate limits. Anthropic's 429 response includes
    # a retry-after header that the SDK surfaces; we just sleep + retry.
    resp = None
    last_err: Exception | None = None
    for attempt in range(RESOLVE_MAX_RETRIES):
        try:
            resp = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"Company name: {name}"}],
            )
            break
        except RateLimitError as e:
            last_err = e
            sleep_s = min(30, 2 ** attempt + 1)  # 2, 3, 5, 9, 17s
            time.sleep(sleep_s)
        except Exception as e:
            last_err = e
            break

    try:
        if resp is None:
            raise last_err or RuntimeError("no response")
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        # Strip code fences if the model ignored instructions.
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(text)
        resolved = ResolvedCompany(
            name=name,
            domain=(data.get("domain") or "").lower().strip() or None,
            confidence=float(data.get("confidence", 0.0)),
            ambiguous=bool(data.get("ambiguous", False)),
            notes=data.get("notes", ""),
        )
    except Exception as e:
        log.warning("import_icp.resolve_failed", name=name, err=str(e))
        resolved = ResolvedCompany(
            name=name, domain=None, confidence=0.0,
            ambiguous=False, notes=f"error: {e}",
        )

    cache[name.lower()] = {
        "domain": resolved.domain,
        "confidence": resolved.confidence,
        "ambiguous": resolved.ambiguous,
        "notes": resolved.notes,
    }
    return resolved


def resolve_all(names: list[str]) -> list[ResolvedCompany]:
    cache = _load_cache()
    client = Anthropic(api_key=settings.anthropic_api_key)
    results: list[ResolvedCompany] = []

    with ThreadPoolExecutor(max_workers=RESOLVE_CONCURRENCY) as pool:
        futures = {
            pool.submit(_resolve_one, client, name, cache): name for name in names
        }
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            tag = "✓" if r.domain and r.confidence >= 0.7 else ("?" if r.domain else "✗")
            print(f"  [{i:>3}/{len(names)}] {tag} {r.name} → {r.domain or '???'}"
                  + (f"  ({r.notes[:60]})" if r.ambiguous or not r.domain else ""))

    _save_cache(cache)
    # Re-sort to CSV order so DB inserts are stable + reviewable.
    by_name = {r.name: r for r in results}
    return [by_name[n] for n in names if n in by_name]


def upsert_companies(resolved: list[ResolvedCompany], default_tier: int,
                     default_segment: str, dry_run: bool) -> dict:
    inserted = updated = needs_review = skipped = 0
    with session_scope() as s:
        for r in resolved:
            if not r.domain:
                # No domain — still record but mark needs_review + is_icp=false.
                domain_value = f"unresolved.needs-review.{r.name.lower().replace(' ', '-').replace(',','').replace('.', '')[:80]}"
                is_icp = False
                needs_review += 1
            elif r.confidence < 0.7 or r.ambiguous:
                # Low confidence — insert but gate out of polling.
                domain_value = r.domain
                is_icp = False
                needs_review += 1
            else:
                domain_value = r.domain
                is_icp = True

            existing = s.execute(
                select(Company).where(Company.domain == domain_value)
            ).scalar_one_or_none()

            if existing is None:
                if not dry_run:
                    s.add(Company(
                        domain=domain_value,
                        name=r.name,
                        segment=default_segment,
                        target_tier=default_tier,
                        is_icp=is_icp,
                    ))
                inserted += 1
            else:
                # Fill in missing tier/segment but don't override explicit values.
                if not dry_run:
                    if existing.target_tier is None:
                        existing.target_tier = default_tier
                    if not existing.segment:
                        existing.segment = default_segment
                    # Re-enable polling if we now have confidence on a domain we previously flagged.
                    if is_icp and not existing.is_icp:
                        existing.is_icp = True
                updated += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "needs_review": needs_review,
        "skipped": skipped,
        "total": len(resolved),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-import ICP companies from a CSV")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve + print, don't write to DB")
    parser.add_argument("--tier", type=int, default=2,
                        help="Default target_tier for new rows (1-3, default 2)")
    parser.add_argument("--segment", default="B",
                        help="Default segment for new rows (A|B|C, default B)")
    parser.add_argument("--skip-resolve", action="store_true",
                        help="Skip Claude domain resolution (all rows go to needs_review)")
    args = parser.parse_args()

    if not args.csv_path.exists():
        print(f"✗ CSV not found: {args.csv_path}", file=sys.stderr)
        return 1

    names = _read_names(args.csv_path)
    print(f"=== Import {args.csv_path.name}: {len(names)} companies ===\n")
    print(f"[resolve] name → domain via Claude (cached, concurrent × {RESOLVE_CONCURRENCY})...\n")

    if args.skip_resolve:
        resolved = [ResolvedCompany(name=n, domain=None, confidence=0.0,
                                    ambiguous=False, notes="--skip-resolve")
                    for n in names]
    else:
        resolved = resolve_all(names)

    counts_by_tier = {"high": 0, "low": 0, "missing": 0}
    for r in resolved:
        if not r.domain:
            counts_by_tier["missing"] += 1
        elif r.confidence >= 0.7 and not r.ambiguous:
            counts_by_tier["high"] += 1
        else:
            counts_by_tier["low"] += 1

    print(f"\n[resolve] summary: ✓ high={counts_by_tier['high']}  "
          f"? low/ambiguous={counts_by_tier['low']}  "
          f"✗ unresolved={counts_by_tier['missing']}")

    print(f"\n[db] upserting{' (DRY RUN)' if args.dry_run else ''}...")
    stats = upsert_companies(resolved, args.tier, args.segment, args.dry_run)
    print(f"\n=== Import complete ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if stats["needs_review"] > 0:
        print(f"\n{stats['needs_review']} companies flagged for review. "
              f"They're in the DB with is_icp=false and won't be polled until "
              f"you confirm their domain. Query with:")
        print(f"  docker exec signalagent-postgres-1 psql -U signal -d signal_agent \\")
        print(f"    -c \"SELECT name, domain FROM companies WHERE is_icp=false ORDER BY name;\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
