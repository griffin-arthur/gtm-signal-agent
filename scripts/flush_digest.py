"""Manually flush any pending digest items.

Usage:
    .venv/bin/python -m scripts.flush_digest
"""
from __future__ import annotations

import sys

from signal_agent.db import session_scope
from signal_agent.integrations.slack import SlackAlerter
from signal_agent.quality import digest


def main() -> int:
    with session_scope() as s:
        result = digest.flush_pending(s, SlackAlerter())
    print(f"flushed {result.get('flushed', 0)} items "
          f"across {result.get('companies', 0)} companies "
          f"(slack_ts={result.get('ts')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
