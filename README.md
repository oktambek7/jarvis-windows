# Jarvis 🎙 (Windows)

An Uzbek-speaking voice agent that lives on your Windows PC and can actually do
things on it.

Say **"Hey Jarvis"**, talk to it in Uzbek, and it runs commands, reads your
screen, writes code, and remembers what you told it.

```
Siz:    Hey Jarvis… batareyam necha foiz?
Jarvis: Batareyangiz 10 foiz qolgan, quvvatlagichga ulashni tavsiya qilaman.
```

```
  mic ──► openWakeWord ──► Gemini Live ──┬──► ~21 tools ──► your PC
        ("hey jarvis",     (WebSocket)   │      (incl. Telegram)
         local, free)                    ├──► delegate_to_gemini       ──► same tools, many steps
                                          ├──► delegate_to_claude       ──► Claude Code (fallback)
                                          ├──► delegate_to_openclaw     ──► self-hosted OpenClaw (optional)
                                          └──► delegate_to_custom_model ──► your own model (optional)
                                                                            │
  Telegram bot ──► text / voice notes ────────────────────────────────────┤
        (your own bot, optional)                                          │
                                  SQLite memory ◄──────────────────────────┘
```

**Gemini Live** is the ears and mouth — one WebSocket, ~0.5 s round trip, native
Uzbek. For anything needing several steps, Jarvis first hands it to its own
**Gemini agent** (`delegate_to_gemini`), which drives the same tools through a
private multi-turn loop — this spends your Gemini quota instead of Claude's.
**Claude Code** (`delegate_to_claude`) is the fallback hands, used only if the
Gemini agent fails or you explicitly ask for Claude. The wake word runs
locally, so no audio leaves your machine and nothing is billed until you
actually say it.

Two more hands are optional and hidden until set up: **`delegate_to_openclaw`**
bridges to a separate self-hosted OpenClaw gateway ([docs/OPENCLAW.md](docs/OPENCLAW.md)),
and **`delegate_to_custom_model`** is a "bring your own brain" slot for any
OpenAI-compatible model/endpoint — both spend neither Gemini's nor Claude's
quota. There's also a second way *in*: Jarvis's own **Telegram bot**
(`telegram_bot.py`) takes text or voice notes from your phone and runs them
through the same tools, so you're not limited to the microphone — see
[docs/GUIDE.md](docs/GUIDE.md#telegram-bot-tasks-from-your-phone).

## Setup

Requires **Windows 10/11** and **Python 3.11 or 3.12** (not 3.13 — openWakeWord
does not support it yet).

```powershell
git clone https://github.com/oktambek7/jarvis-windows
cd jarvis-windows

py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .

copy .env.example .env       # add a free key from aistudio.google.com/apikey
.venv\Scripts\python -m jarvis --doctor
.venv\Scripts\python -m jarvis
```

Windows will ask for **microphone** permission the first time. If it does not,
enable it manually under *Settings → Privacy & security → Microphone → Let
desktop apps access your microphone*. `--doctor` checks this and everything else
before you trust it with your PC.

Multi-step work (writing code, multi-step automation, research) is handled by
Jarvis's own Gemini agent by default — no extra install needed. The Claude
Code CLI is optional and used only as a fallback if that fails. Install it
with:

```powershell
npm install -g @anthropic-ai/claude-code
```

Without it, Jarvis hides the Claude delegation tools rather than offering
something that always fails — everything else, including `delegate_to_gemini`,
still works.

Telegram messaging (`send_telegram_message`) is also optional — see
[docs/GUIDE.md](docs/GUIDE.md) for the one-time `--telegram-login` setup.

## Try it without a microphone first

The fastest way to confirm the setup is sound, before debugging any audio:

```powershell
.venv\Scripts\python -m jarvis --text "papkamda qanday fayllar bor?"
.venv\Scripts\python -m jarvis --text "kalkulyatorni och"
```

If those work, the tool layer, the Uzbek persona and your API key are all fine
and anything left is audio tuning.

## The HUD

Run `jarvis` and a real, sizeable holographic panel opens front-and-center on
your screen — dark glass, a glowing arc-reactor ring, a sweeping radar bezel
and targeting-bracket corners, movie-JARVIS style. It settles to a smaller
idle ring while asleep, and the instant "Hey Jarvis" fires it grows into the
full panel — cyan while listening, amber while a tool runs, a big pulsing
equalizer while it speaks, red on error. Purely visual, no on-screen text.
It's optional (`ui.overlay.enabled` in `config.yaml`) and needs no setup
beyond the normal `pip install -e .`. See
[docs/GUIDE.md](docs/GUIDE.md#the-hud-overlay).

Jarvis's voice defaults to a deep male persona; set `gemini.voice_gender` to
`"male"` or `"female"` in `config.yaml` to choose.

## Configuration

Everything you'd want to change lives in **`config.yaml`**, including
`agent.autonomy`:

- `"full"` (current default) — no confirmations, fastest, most Jarvis-like
- `"guarded"` — reads, screenshots and app launches run instantly;
  destructive commands ask first

Every tool call is written to `logs/audit.jsonl` either way.

> ⚠️ Jarvis runs PowerShell with your full user rights, and speech recognition
> isn't perfect. On `autonomy: "full"` (the current default) a misheard command
> CAN delete files, with nothing standing in the way except your own attention
> to what you actually said. If you want destructive commands to ask first
> while you get a feel for it, set `autonomy: "guarded"` — spend a day there
> before switching to `"full"`.

## More

**[docs/GUIDE.md](docs/GUIDE.md)** — the full tool list, how memory works, echo and
microphone tuning, the Aisha Uzbek voice, troubleshooting, code layout, and the
full list of differences from the macOS original.

**[docs/OPENCLAW.md](docs/OPENCLAW.md)** — bridging `delegate_to_openclaw` to a
separately self-hosted OpenClaw gateway, for heavy tasks that shouldn't spend
Gemini or Claude quota. Optional; hidden entirely if it isn't running.

MIT licensed, like the project it is ported from.
