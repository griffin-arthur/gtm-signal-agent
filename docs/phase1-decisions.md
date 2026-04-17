# Phase 1 Build Decisions

Concise record of choices made during the Phase 1 build. Revisit these in the
Phase 2 kickoff.

## Stack
- **Python** for everything. Best SDKs for Anthropic, HubSpot, Slack, and common
  patterns for scraping. TS would have worked for Inngest but Python won on
  overall fit.
- **Inngest** for orchestration (per the brief). Dev server makes local iteration
  fast; no Redis/Postgres-queue to babysit. Swap to Temporal later only if we
  hit scheduling complexity Inngest can't express (multi-week waits etc.).
- **Postgres** for signal state. ON CONFLICT upserts make dedup a one-liner.
- **FastAPI** to host Inngest endpoints + Slack interactivity. Lightweight, async.

## Where intelligence lives
1. **Keyword pre-filter** (`ingestors/keywords.py`) — cheap, deterministic, and
   decides whether to spend an LLM call. False negatives here silently cost us
   signals; false positives cost LLM dollars but are caught downstream.
2. **LLM validator** (`scoring/validator.py`) — authoritative classifier.
   Returns confidence; anything <0.7 goes to review, not an alert.
3. **Rubric** (`scoring/rubric.py`) — weights + decay. Single file tuning knob
   for RevOps to adjust after the weekly review.

The split matters because the three levers fail differently: keywords are coarse
and cheap to edit, the rubric is coarse and cheap to tune, the LLM is fine-grained
and expensive to change (prompt iteration + eval). Keep prompts stable once tuned.

## Idempotency
- **Ingest fan-out:** Inngest event IDs include the date, so a manual re-run of
  the daily job won't double-fire per-company events within 24h.
- **Signal upsert:** `(company_id, signal_type, source_url)` unique key. Re-seeing
  a posting bumps `last_seen_at` only; it does not trigger the alert pipeline again.
- **LLM cache:** SHA256(signal_type + signal_text) → validation result, 30-day TTL.

## What was deliberately left out
- **News, SEC, LinkedIn, earnings calls, competitive intel.** Phase 2+.
  `ingestors/base.py` is the extension point.
- **Digest mode** when alerts cluster. Circuit breaker covers the "outage" case
  but we don't yet batch normal-rate bursts.
- **AE territory routing.** Phase 3 — needs HubSpot owner/territory data.
- **HubSpot custom property auto-creation.** The four `arthur_signal_*`
  properties must exist in the portal before Phase 1 writes land. Build a
  `scripts/setup_hubspot.py` in Phase 2 to own this.
- **Pipeline attribution.** Phase 4. The `Alert` table is designed so we can
  join to HubSpot deals after the fact.

## Known risks
1. **Job board coverage.** Greenhouse + Lever cover most tech scaleups but miss
   Workday-hosted boards (a lot of Fortune 1000 HR orgs). Phase 2 must add
   Workday scraping or a commercial feed (Coresignal / TheirStack).
2. **LLM prompt drift.** The validator prompt has no eval harness. Before
   Phase 2, build a labeled set of ~50 postings and track precision/recall as
   the prompt changes.
3. **Suppression list atrophy.** The weekly review loop must feed rejected
   alerts into either rubric tweaks or suppression rules, otherwise the same
   false positives recur.
