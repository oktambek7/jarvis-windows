# OpenClaw — the self-hosted "second brain"

Jarvis's own tools (`delegate_to_gemini`, `delegate_to_claude`) spend Gemini
or Claude quota. **OpenClaw** is a separate, self-hosted agent gateway
(Docker) that already has its own Telegram bot, its own Google Workspace and
browser-automation skills, and — deliberately — a *non*-Gemini/Claude model
as its default brain. `delegate_to_openclaw` (in `jarvis/tools/openclaw.py`)
is a thin bridge that hands it a task and reports back the reply, so heavy
work can go there instead without touching Jarvis's own model budgets.

OpenClaw is not part of the jarvis-windows codebase and is not installed by
`pip install -e .` — it runs entirely on its own, and Jarvis only talks to it
if it's already up.

## Where it lives on this machine

The full deployment (config, workspace, skills, secrets) lives at:

```
C:\Users\<you>\OpenClawAdvanced\workshop
```

with the original export kept as a backup archive one level up, at
`C:\Users\<you>\OpenClawAdvanced\jarvis-full-export-backup.zip`. Both are
**outside** this git repo and outside Downloads on purpose — the workshop
folder's `.env` holds real secrets (Telegram bot token, the gateway auth
token, the custom model's API key, MTProto credentials), and its
`workspace/` holds real exported Telegram chat history. Never copy either
into `jarvis-windows/` or commit them anywhere.

## Starting it

Requires Docker Desktop (WSL2 backend). From the workshop folder:

```powershell
cd C:\Users\<you>\OpenClawAdvanced\workshop
docker compose --profile mtproto up -d
docker compose --profile mtproto ps
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18789/healthz
```

This starts the stock `ghcr.io/openclaw/openclaw:latest` image (pulled
fresh). The original deployment also had a `docker-compose.override.yml`
pinning everything to a custom-built `live-talk` image for a native
Telegram Mini App voice feature — that image's source wasn't part of the
export, so the override was renamed to
`docker-compose.override.yml.disabled-no-custom-image` and is not applied.
Jarvis already has its own voice front end (Gemini Live + the wake word), so
OpenClaw's Mini App Live Talk path is not needed for this integration.

The `mtproto` profile additionally starts `tg-watcher`, which needs
`TG_API_ID` / `TG_API_HASH` / `TG_USER_SESSION` in `.env` (already present in
the export). If that session has gone stale on this machine, re-auth with:

```powershell
bash scripts/mtproto-setup.sh
```

## How Jarvis talks to it

`jarvis/tools/openclaw.py` shells out to the gateway container directly —
the same mechanism OpenClaw's own `tg-watcher.js` uses internally to spawn a
one-shot agent run:

```
docker exec <container> node /app/dist/index.js agent --agent main --local --timeout <N> --message "<task>"
```

`config.yaml`'s `openclaw:` section controls the container name
(`workshop-openclaw-gateway-1` by default — matches `docker compose ps`),
the agent id, and the timeout. Jarvis checks at startup whether that
container is actually running (`docker inspect`) and hides
`delegate_to_openclaw` entirely if it isn't, the same way it hides
`delegate_to_claude` when the Claude CLI isn't installed — so a stopped
OpenClaw stack just means one fewer tool offered, not a broken one.

## Verifying it works

```powershell
.venv\Scripts\python -m jarvis --text "OpenClaw orqali <biror vazifa>ni bajar"
```

should route through `delegate_to_openclaw` once the container is up. You
can also message the OpenClaw Telegram bot directly (same bot as before —
the token didn't change) to confirm the stack itself is healthy independent
of Jarvis.

## Troubleshooting

- **Tool never offered / Jarvis says nothing changed**: the container isn't
  running, or its name doesn't match `openclaw.container` in `config.yaml` —
  check with `docker compose ps` and adjust.
- **"OpenClaw xato qaytardi" with a docker error**: read the detail in the
  error — usually the container name mismatch above, or the gateway crashed
  (`docker compose logs -f openclaw-gateway`).
- **Bot doesn't reply on Telegram**: check `docker compose logs -f
  openclaw-gateway`; this is unrelated to Jarvis and would fail the same way
  without it.
