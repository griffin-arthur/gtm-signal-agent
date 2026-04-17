# Production Deployment Plan — Render + Inngest Cloud

**Goal:** Move the signal agent from `python -m scripts.run_pipeline` on your
laptop to a continuously-running, scheduled service. Target setup:

- **FastAPI app** on Render (serves Inngest webhooks + Slack interactivity)
- **Managed Postgres** on Render
- **Inngest Cloud** for scheduling (daily cron + event-driven fan-out)
- **Tracing** continues to go to the shared Arthur engine at
  `https://engine.development.arthur.ai`

**Estimated setup time:** 60–90 minutes first time.
**Estimated monthly cost:** $7–15 (Render web service $7, Postgres free tier
or $7, Inngest Cloud free tier up to 50K steps/day — ample for our load).

---

## Phase A: Prerequisites (you already have these)

- [x] Private git repo containing signal-agent (needed for Render deploy-from-git)
- [x] `.env` variables known (HubSpot, Slack, Anthropic, Arthur)
- [x] ICP companies imported
- [x] Slack app installed to workspace
- [x] Arthur tracing verified against remote engine

---

## Phase B: Inngest Cloud setup (~15 min)

1. **Create an Inngest Cloud account** at https://app.inngest.com. Use the
   same Google/GitHub login you'd use for the team.
2. **Create an app** named `signal-agent-prod`. Grab two values from the
   Settings → Keys page:
   - **Event Key** (`EVENT_KEY_*`) — goes in `INNGEST_EVENT_KEY`
   - **Signing Key** (starts with `signkey-prod-`) — goes in `INNGEST_SIGNING_KEY`
3. Don't register the app yet — we need the Render URL first.

---

## Phase C: Render deployment (~30 min)

### C1. Create the Postgres service
1. Render dashboard → New → Postgres.
2. Plan: **Free** (1 GB, enough for ~100K signals). Upgrade to $7/mo
   Starter when we hit 50% of that.
3. Region: pick the same one you'll deploy the web service in.
4. Name: `signal-agent-postgres-prod`.
5. Once provisioned, copy the **Internal Database URL** (postgresql://…).

### C2. Create the web service
1. Render dashboard → New → Web Service.
2. Connect the GitHub repo for signal-agent.
3. Settings:
   - Runtime: **Python 3**
   - Build command: `pip install -e .`
   - Start command: `uvicorn signal_agent.api.main:app --host 0.0.0.0 --port $PORT`
   - Plan: **Starter** ($7/mo) — free tier spins down after 15 min idle,
     which would miss scheduled runs.
   - Auto-deploy: **on** (deploy on every push to `main`)
4. Environment variables — paste all of these, replacing the localhost
   `DATABASE_URL` with the Internal Database URL from C1:
   ```
   DATABASE_URL=<internal DB URL from C1>
   ANTHROPIC_API_KEY=<real key>
   ANTHROPIC_MODEL=claude-sonnet-4-5-20250929
   HUBSPOT_ACCESS_TOKEN=<real token>
   SLACK_BOT_TOKEN=<xoxb-…>
   SLACK_SIGNING_SECRET=<…>
   SLACK_ALERT_CHANNEL=#gtm-signals
   SLACK_OWNER_USER_ID=U09CR14FREU
   INNGEST_APP_ID=signal-agent-prod
   INNGEST_EVENT_KEY=<from B2>
   INNGEST_SIGNING_KEY=<from B2>
   INNGEST_DEV=0
   ARTHUR_TRACING_ENABLED=1
   ARTHUR_ENGINE_BASE_URL=https://engine.development.arthur.ai
   ARTHUR_ENGINE_API_KEY=<remote engine API key>
   ARTHUR_TASK_ID=77ff2f48-e174-42da-a1e2-9b5326328fff
   ARTHUR_SERVICE_NAME=signal-agent-prod
   # Scoring
   ALERT_SCORE_THRESHOLD=8
   ALERT_CUMULATIVE_THRESHOLD=12
   ALERT_CUMULATIVE_WINDOW_DAYS=60
   LLM_CONFIDENCE_FLOOR=0.7
   CIRCUIT_BREAKER_ALERTS_PER_HOUR=20
   DIGEST_RATE_THRESHOLD=5
   DIGEST_FLUSH_INTERVAL_MINUTES=15
   ALERT_COOLDOWN_HOURS=24
   ALERT_MATERIAL_CHANGE_RATIO=0.5
   ```
5. Wait for the first deploy to succeed. It'll give you a URL like
   `https://signal-agent-prod.onrender.com`.

### C3. Run migrations + load seeds on the new Postgres
One-shot command via Render shell (from the service's "Shell" tab):
```bash
alembic upgrade head
python -m signal_agent.seeds.load_icp
python -m scripts.setup_hubspot          # creates the 4 custom HubSpot properties
```

If you also want to bulk-import from the CSV on prod:
```bash
# Upload the CSV to the Render shell or fetch from a URL, then:
python -m scripts.import_icp_csv /path/to/csv
```

---

## Phase D: Connect Inngest Cloud → Render (~10 min)

1. In Inngest Cloud → Apps → Add App → paste
   `https://signal-agent-prod.onrender.com/inngest`.
2. Inngest will ping that URL to sync your registered functions. You should
   see `ingest_jobs_daily`, `ingest_jobs_for_company`, `process_signal`,
   `flush_digest` appear.
3. The cron triggers fire automatically once registered:
   - `ingest_jobs_daily` at 13:00 UTC daily
   - `flush_digest` every 15 minutes

---

## Phase E: Slack interactivity (~5 min)

1. Slack app → Interactivity & Shortcuts → update **Request URL** to
   `https://signal-agent-prod.onrender.com/slack/interactivity`.
2. Save.
3. Click a Claim/Snooze button on an old alert in `#gtm-signals` to verify
   the endpoint is reachable.

---

## Phase F: Verification (~10 min)

1. **Manual pipeline trigger.** From Inngest Cloud dashboard, find
   `ingest_jobs_daily` → "Invoke" → empty payload → Run. Watch the function
   run logs.
2. **Check Slack.** Any Tier 1 signals should post. Expect a modest volume
   since most of the 361 companies are cache-cold on their first run.
3. **Check Arthur.** Open the Trace Viewer at
   https://engine.development.arthur.ai/tasks/77ff2f48-e174-42da-a1e2-9b5326328fff/traces.
   You should see `signal_agent.process_signal` parent spans with nested
   Anthropic LLM spans for every new signal.
4. **Check HubSpot.** Companies that crossed the threshold should have
   `arthur_signal_score` populated.

---

## Phase G: Operational runbook

### Update deployed code
Push to `main` → Render auto-deploys. Inngest re-syncs functions on every
deploy. If function signatures changed, Inngest's dashboard flags "out of
sync."

### Rotate a secret
Render → service → Environment → edit the var → Render restarts. No code
change needed.

### Increase polling cadence
Edit the cron schedule in
`signal_agent/workflows/jobs_daily.py::ingest_jobs_daily` — currently
`0 13 * * *`. Push. Inngest picks up the new schedule on sync.

### Pause alerting temporarily
Set `CIRCUIT_BREAKER_ALERTS_PER_HOUR=0` in Render env. Every alert will be
suppressed until you revert.

### Back up the Postgres
Render managed Postgres backs up daily automatically. For manual:
```bash
PGPASSWORD=<pw> pg_dump -h <host> -U <user> -d <db> > signal_agent_$(date +%F).sql
```

### Costs to watch
- **Anthropic spend.** Each validated signal = one Claude call. Cache
  means re-ingesting the same posting is free. Budget ~$0.01/signal.
  With 361 companies × ~3 new signals/day × $0.01 = ~$11/month.
- **Render compute.** Starter $7/mo covers the web service. Postgres free
  tier up to 1 GB.
- **Inngest.** Free tier = 50K steps/day. Our load is ~5K steps/day.

---

## Security checklist before deploy

- [ ] Verify `.env` is in `.gitignore` (it is)
- [ ] No secrets committed to the repo (run `git log -p | grep -iE "api_key|secret|token"` as a paranoia check)
- [ ] Slack app restricted to your workspace
- [ ] HubSpot private app has minimal scopes (companies read/write, properties write, timeline write)
- [ ] Arthur API key is task-scoped (not account-wide admin)
- [ ] Render environment variables are encrypted at rest (Render default)

---

## Migration questions to resolve before executing this plan

1. **Who owns the git repo?** Currently this is local on your laptop. Needs
   to be pushed to GitHub (or GitLab/Bitbucket) so Render can deploy from it.
2. **Do you want prod and dev to share an Arthur task?** Easier for now, but
   mixes traces. Consider a separate prod task later.
3. **Slack channel for prod.** Should prod post to `#gtm-signals` same as
   dev, or a separate `#gtm-signals-prod`?
4. **HubSpot instance.** Is the private app token you're using the real
   production HubSpot, or a sandbox?
5. **Which branch does Render deploy from?** `main` is the convention; if
   you want a `prod` branch with gated merges, configure that at deploy time.

Answer these, then in a separate session I can walk through executing the
plan step-by-step.
