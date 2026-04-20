#!/usr/bin/env bash
# Post-deploy seed/setup script for prod.
#
# Run this once via the Render Shell after the first successful deploy.
# Idempotent — safe to re-run whenever seeds change.
#
# Usage (from Render Shell or `render ssh`):
#   bash scripts/post_deploy.sh
#
# What it does:
#   1. Ensures Alembic is at head (should already be — the start command
#      runs this too, but we're explicit here).
#   2. Loads ICP companies + suppression rules from seeds/*.yaml.
#   3. Creates the 4 custom HubSpot company properties (idempotent).
#   4. Scrapes competitor customer pages and populates the cache.
#
# Exit codes:
#   0 — everything succeeded
#   non-zero — bail and check the output; fix the problem before re-running

set -euo pipefail

echo "==> alembic upgrade head"
alembic upgrade head

echo ""
echo "==> loading ICP seeds"
python -m signal_agent.seeds.load_icp

echo ""
echo "==> ensuring HubSpot custom properties exist"
python -m scripts.setup_hubspot

echo ""
echo "==> populating competitor-customer cache"
python -m scripts.refresh_competitor_customers

echo ""
echo "==> post-deploy setup complete"
