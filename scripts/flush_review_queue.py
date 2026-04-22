"""Manage the signal review queue.

Signals that the LLM validator marked with low confidence (below
LLM_CONFIDENCE_FLOOR) or whose JSON couldn't be parsed are set to
`status=review`. Nothing else in the system touches them — they just
accumulate forever unless someone deals with them.

This script provides three operations:

  list   (default)
        Print every signal currently in review, grouped by company.
        Includes the LLM reasoning so you can eyeball what went wrong.

  retry
        Re-run the validator against each review-status signal. Uses the
        normal caching path; cache is typically stale for reviewed rows
        so expect real LLM spend. Moves signals to whatever the new
        validation result says (validated / rejected / review again).

  reject-stale --older-than-days N
        Bulk-move signals older than N days (default: 14) from review to
        rejected. Useful housekeeping — old low-confidence signals rarely
        become relevant later.

Usage:
    .venv/bin/python -m scripts.flush_review_queue            # default: list
    .venv/bin/python -m scripts.flush_review_queue retry
    .venv/bin/python -m scripts.flush_review_queue reject-stale --older-than-days 14
    .venv/bin/python -m scripts.flush_review_queue reject-stale --dry-run
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update

from signal_agent.db import session_scope
from signal_agent.models import Signal, SignalStatus
from signal_agent.schemas import NormalizedSignal
from signal_agent.scoring.validator import validate_signal

log = structlog.get_logger()


def cmd_list() -> int:
    # Do all reads inside the session to avoid DetachedInstanceError on
    # the `company` relationship. Materialize the output as plain tuples
    # so we can print after the session closes if we want.
    with session_scope() as s:
        rows = s.execute(
            select(Signal).where(Signal.status == SignalStatus.REVIEW)
            .order_by(Signal.detected_at)
        ).scalars().all()

        if not rows:
            print("No signals in review.")
            return 0

        print(f"=== {len(rows)} signals in review ===")

        by_company: dict[str, list] = {}
        for sig in rows:
            by_company.setdefault(sig.company.name, []).append(sig)

        for company_name in sorted(by_company):
            sigs = by_company[company_name]
            print(f"\n{company_name} ({len(sigs)})")
            print("-" * (len(company_name) + 10))
            for sig in sigs:
                age_days = (datetime.now(timezone.utc)
                            - sig.detected_at.replace(tzinfo=timezone.utc)).days
                print(f"  signal {sig.id}  ({sig.signal_type}, {age_days}d old)")
                conf = sig.llm_confidence if sig.llm_confidence is not None else "?"
                print(f"    confidence: {conf}")
                if sig.llm_reasoning:
                    print(f"    reasoning:  {sig.llm_reasoning[:200]}")
                print(f"    source_url: {sig.source_url[:100]}")
    return 0


def cmd_retry() -> int:
    """Re-validate each review-status signal. Doesn't try to be clever about
    cache — we just call validate_signal; caller sees real LLM spend if caches
    are cold."""
    with session_scope() as s:
        ids = s.execute(
            select(Signal.id).where(Signal.status == SignalStatus.REVIEW)
        ).scalars().all()

    if not ids:
        print("No signals to retry.")
        return 0

    print(f"Retrying validation for {len(ids)} signals...\n")
    outcomes = {"validated": 0, "rejected": 0, "review": 0, "error": 0}

    for i, sid in enumerate(ids, 1):
        try:
            with session_scope() as s:
                sig = s.get(Signal, sid)
                if sig is None:
                    continue
                norm = NormalizedSignal(
                    company_domain=sig.company.domain,
                    company_name=sig.company.name,
                    signal_type=sig.signal_type,
                    source=sig.source,
                    source_url=sig.source_url,
                    signal_text=sig.signal_text,
                    raw_payload=sig.raw_payload,
                    detected_at=sig.detected_at,
                )
                result = validate_signal(norm)
                sig.llm_confidence = result.confidence
                sig.llm_reasoning = result.reasoning
                sig.llm_summary = result.summary_for_ae

                if not result.is_valid:
                    sig.status = SignalStatus.REJECTED
                    outcomes["rejected"] += 1
                    verdict = "rejected"
                elif result.confidence < 0.7:  # LLM_CONFIDENCE_FLOOR
                    sig.status = SignalStatus.REVIEW
                    outcomes["review"] += 1
                    verdict = "still review"
                else:
                    sig.status = SignalStatus.VALIDATED
                    outcomes["validated"] += 1
                    verdict = "validated"

                print(f"  [{i:>3}/{len(ids)}] signal {sid} ({sig.company.name}) → {verdict}")
        except Exception as e:
            outcomes["error"] += 1
            print(f"  [{i:>3}/{len(ids)}] signal {sid} → error: {type(e).__name__}: {e}")

    print(f"\n=== summary ===")
    for k, v in outcomes.items():
        if v > 0:
            print(f"  {v:>4}  {k}")
    return 0


def cmd_reject_stale(older_than_days: int, dry_run: bool) -> int:
    """Bulk-move old review-status signals to rejected."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with session_scope() as s:
        stale = s.execute(
            select(Signal).where(
                Signal.status == SignalStatus.REVIEW,
                Signal.detected_at < cutoff,
            )
        ).scalars().all()
        print(f"Found {len(stale)} stale review signals (> {older_than_days}d old).")
        for sig in stale[:20]:
            print(f"  signal {sig.id}  {sig.company.name}  ({sig.signal_type})")
        if len(stale) > 20:
            print(f"  ... and {len(stale) - 20} more")

        if dry_run or not stale:
            print("\n(dry-run or nothing to do)" if dry_run else "")
            return 0

        rowcount = s.execute(
            update(Signal)
            .where(
                Signal.status == SignalStatus.REVIEW,
                Signal.detected_at < cutoff,
            )
            .values(
                status=SignalStatus.REJECTED,
                llm_reasoning="bulk-rejected: stale review queue entry (>N days)",
            )
        ).rowcount
    print(f"\nMoved {rowcount} signals from review → rejected.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="List all signals currently in review (default)")
    sub.add_parser("retry", help="Re-run LLM validation against every review-status signal")
    rs = sub.add_parser("reject-stale",
                        help="Move review signals older than N days to rejected")
    rs.add_argument("--older-than-days", type=int, default=14)
    rs.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.cmd == "retry":
        return cmd_retry()
    if args.cmd == "reject-stale":
        return cmd_reject_stale(args.older_than_days, args.dry_run)
    return cmd_list()


if __name__ == "__main__":
    sys.exit(main())
