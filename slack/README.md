# Slack App Setup

## 1. Create a public tunnel to localhost

Slack needs a publicly reachable HTTPS URL for the interactivity endpoint.
Pick one:

```bash
# Option A: ngrok (simplest)
brew install ngrok
ngrok http 8000
# Copy the "Forwarding" URL, e.g. https://abc123.ngrok-free.app

# Option B: Cloudflare Tunnel (no signup required for quick tunnels)
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```

Keep this running in a separate terminal. The URL changes each ngrok restart
(unless you pay for a reserved domain), so you'll re-edit the Slack app config
on each restart during dev.

## 2. Create the Slack app from the manifest

1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest**.
2. Pick your workspace.
3. Paste the contents of `manifest.yaml`.
4. Review the scopes and click **Create**.
5. Slack will complain that `request_url` is invalid — that's expected. Open
   **Interactivity & Shortcuts** in the left nav and replace the placeholder
   with your tunnel URL: `https://<your-ngrok>.ngrok-free.app/slack/interactivity`
   Then click **Save Changes**.

## 3. Install the app to your workspace

1. In the left nav, click **Install App** → **Install to Workspace** → **Allow**.
2. Copy the **Bot User OAuth Token** (starts with `xoxb-`) into your `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   ```
3. Go to **Basic Information** in the left nav, scroll to **App Credentials**,
   reveal and copy the **Signing Secret**:
   ```
   SLACK_SIGNING_SECRET=...
   ```

## 4. Invite the bot to your alert channel

In Slack:

```
/invite @arthur-signal-agent
```

in the channel you set as `SLACK_ALERT_CHANNEL` (default `#gtm-signals-test`).
If you kept `chat:write.public` in the manifest, this is optional — the bot can
post without being a member — but inviting is still the conventional pattern.

## 5. (Optional) Get your Slack user id for circuit breaker DMs

If you want the circuit breaker to DM you when it trips:

```
SLACK_OWNER_USER_ID=U01ABCDEF23
```

To find your id: click your avatar in Slack → **Profile** → menu (`...`) →
**Copy member ID**.

## 6. Test it

With the FastAPI app running (`uvicorn signal_agent.api.main:app --port 8000`)
and ngrok forwarding, post a test message:

```bash
curl -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel":"#gtm-signals-test","text":"signal agent online"}'
```

You should see `"ok": true`. If you see `"not_in_channel"`, invite the bot.

The first real alert will arrive once the ingestion pipeline validates a
signal at or above the alert threshold.

## Scope cheat sheet

| Scope              | Why                                                  |
|--------------------|------------------------------------------------------|
| `chat:write`       | Post alerts + thread acknowledgments                 |
| `chat:write.public`| Post to channels without being invited (optional)    |
| `im:write`         | DM circuit-breaker notifications to the owner        |
| `channels:read`    | Resolve channel names for error surfacing            |
| `users:read`       | Resolve user id → handle on Claim button click       |

## Troubleshooting

- **Button click does nothing**: ngrok URL expired or wrong. Re-check
  **Interactivity & Shortcuts** in the Slack app UI. Tail your FastAPI logs —
  if you see no POST to `/slack/interactivity`, it's the URL.
- **"invalid signature" in logs**: the signing secret in `.env` doesn't match
  the app. Re-copy from **Basic Information** → **App Credentials**.
- **"channel_not_found"**: the channel doesn't exist or (if you removed
  `chat:write.public`) the bot isn't invited.
- **"missing_scope"**: add the scope in the manifest, then **Install App** →
  **Reinstall to Workspace** to apply.
