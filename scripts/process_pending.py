"""Process-only fast path — skip ingestion entirely.

Takes every pending Signal in the DB and runs it through the same
validate → score → alert pipeline as `scripts/run_pipeline.py`, but
without polling any external job/news/SEC sources first. Useful when:

 - You already have signals in the DB from a prior (partial) ingest run
 - You just changed scoring / alerting code and want to exercise it
   against real data without waiting ~30 min for full re-ingestion
 - You're verifying a specific behavior change (e.g. this run was added
   to prove the per-run hard-cap actually suppresses duplicates)

Uses the same `process_signal()` function as the main runner so the
per-run alerted-companies hard cap still applies.

Usage:
    .venv/bin/python -m scripts.process_pending
    .venv/bin/python -m scripts.process_pending --limit 50
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

# Initialize tracing before any module that imports Anthropic / httpx.
from signal_agent.observability import tracing as _tracing
_tracing.initialize()

from sqlalchemy import select  # noqa: E402

from signal_agent.db import session_scope  # noqa: E402
from signal_agent.models import Signal, SignalStatus  # noqa: E402
from scripts.run_pipeline import process_signal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N signals (default: process all pending)")
    args = parser.parse_args()

    with session_scope() as s:
        pending_ids = s.execute(
            select(Signal.id).where(Signal.status == SignalStatus.PENDING).order_by(Signal.id)
        ).scalars().all()
    if args.limit:
        pending_ids = pending_ids[:args.limit]

    print(f"=== process_pending @ {datetime.utcnow().isoformat()}Z ===")
    print(f"[queue] {len(pending_ids)} pending signals\n")
    if not pending_ids:
        print("nothing to do")
        return 0

    # Shared across all signals in this run so the hard cap engages.
    alerted_this_run: set[int] = set()

    outcomes: dict[str, int] = {}
    for i, sid in enumerate(pending_ids, 1):
        try:
            result = process_signal(sid, per_run_alerted_companies=alerted_this_run)
            oc = result.get("outcome", "error")
            outcomes[oc] = outcomes.get(oc, 0) + 1
            tag = "✓" if oc == "alerted" else " "
            cumulative = result.get("cumulative", "")
            reason = result.get("alert_reason", "")
            print(f"  [{i:>3}/{len(pending_ids)}] {tag} signal {sid}: {oc}"
                  + (f"  score={cumulative}" if cumulative else "")
                  + (f"  ({reason})" if reason and reason != oc.removeprefix("suppressed_") else ""))
        except Exception as e:
            outcomes["error"] = outcomes.get("error", 0) + 1
            print(f"  [{i:>3}/{len(pending_ids)}]  ! signal {sid}: {type(e).__name__}: {e}")

    print("\n=== summary ===")
    for k, v in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>4}  {k}")
    print(f"\nalerted distinct companies: {len(alerted_this_run)}")

    _tracing.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
