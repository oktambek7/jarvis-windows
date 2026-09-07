# Jarvis (Windows) — the long version

## How it fits together

```
jarvis/
├── __main__.py     CLI: --doctor, --text, --devices, --no-wake
├── app.py          wiring + the wake-word loop
├── winplat.py      every Windows-specific quirk, in one place
├── audio.py        microphone in, speaker out, echo control, wake word
├── live.py         the Gemini Live session (ears and mouth)
├── persona.py      the Uzbek system prompt
├── memory.py       SQLite: turns, facts, background jobs
├── reflect.py      mines finished conversations for durable facts
├── genai_util.py   turn-based generation with model fallback
├── audit.py        append-only log of every tool call
├── console.py      terminal output
├── config.py       config.yaml + .env loading
├── tools/
│   ├── base.py       the registry + the destructive-command safety net
│   ├── system.py     the 11 tools that touch Windows
│   ├── delegate.py   handing work to Claude Code (fallback hands)
│   ├── gemini_agent.py  handing work to Jarvis's own Gemini agent (default hands)
│   ├── openclaw.py   handing work to a self-hosted OpenClaw gateway (docs/OPENCLAW.md)
│   ├── custom_brain.py  handing work to a pluggable OpenAI-compatible model ("bring your own brain")
│   ├── agentwork.py  shared grace-period/background delivery for both of the above
│   ├── telegram.py   send_telegram_message, via Telethon
│   └── recall.py     memory tools
├── voice/
│   ├── base.py     native (Gemini) speech output
│   └── aisha.py    optional Uzbek TTS
└── ui/             the dynamic HUD overlay
    ├── bus.py       state pub/sub (no GUI deps — always importable)
    ├── overlay.py   the PySide6 widget itself
    └── runtime.py   merges Qt's event loop into asyncio via qasync
```

The daemon spends almost all its life in one cheap loop: read 80 ms of mic
audio, ask openWakeWord if it heard "hey jarvis", discard it. No network, no API
cost. Only when the wake word fires does a Gemini Live socket open — which is
also why you are not billed for sitting in silence.

## The tools

**Machine control** — `run_shell`, `open_app`, `close_app`, `open_url`,
`notify`, `clipboard_read`, `clipboard_write`, `see_screen`

**Files** — `read_file`, `write_file`, `list_dir`

**Memory** — `remember`, `recall`, `forget`, `search_history`

**Messaging** — `send_telegram_message` (hidden until `TELEGRAM_API_ID` /
`TELEGRAM_API_HASH` are set and `--telegram-login` has been run — see below)

**Delegation** — `delegate_to_gemini` (default hands, spends Gemini quota),
`delegate_to_claude` (fallback hands, spends Claude quota),
`delegate_to_openclaw` (hands for OpenClaw's own skills/model, spends
neither — hidden until the OpenClaw container is running, see
[docs/OPENCLAW.md](OPENCLAW.md)), `delegate_to_custom_model` (hands for a
pluggable OpenAI-compatible model you configure yourself — hidden until
`custom_model.base_url`/`model` and its API key are all set), `check_jobs`

### Delegation: grace period, then background

Both delegation tools share `tools/agentwork.py`'s `deliver()`: a task gets
`GRACE_SECONDS` (12s) to finish inline before Jarvis stops blocking the
conversation on it. Past that — or if the model set `background: true` up
front — the task keeps running and its result is delivered later through
`announce()` (spoken if a session is live, a Windows toast otherwise), same
as any other background job. Before this existed, a task that took longer
than the model expected but wasn't explicitly backgrounded would hang the
entire Live turn for as long as it ran — up to `claude.timeout` (900s) — and
read as Jarvis having gone silent or ignored the request.

### `delegate_to_gemini`

Runs the same tool registry through a private, non-Live text call, driving
its own turn-by-turn loop (up to `gemini_agent.MAX_STEPS`, 20) instead of
Gemini Live's one-tool-call-at-a-time Live turns. Its own declarations
exclude `delegate_to_gemini` / `delegate_to_claude` / `check_jobs` so it
can't recurse into delegating to itself. This is what the `--text` CLI mode
was already doing in an 8-step loop; `delegate_to_gemini` is that same
pattern exposed to the voice session with a higher step budget, since it now
also carries tasks that used to go straight to Claude.

### `delegate_to_openclaw`

Runs a task through a *separate* self-hosted agent (OpenClaw), not through
Jarvis's own tool loop. Set up and configured independently of Jarvis — see
[docs/OPENCLAW.md](OPENCLAW.md) for what it is and how to run it — this tool
is just a thin bridge: it shells out to `docker exec <container> node
/app/dist/index.js agent --agent main --message "<task>"` and reports back
whatever OpenClaw's agent replies. Reach for it when the user explicitly asks
for OpenClaw, or wants one of the things OpenClaw is set up to do that Jarvis
isn't (Google Workspace automation, browser automation, its own Telegram
bot's skills) — otherwise `delegate_to_gemini` remains the default.

### `delegate_to_custom_model`

A "bring your own brain" slot: any model/provider that speaks the OpenAI
chat-completions API with tool calling works here. Set `custom_model.
base_url` and `custom_model.model` in `config.yaml`, and put its key in the
env var named by `custom_model.api_key_env` (`CUSTOM_MODEL_API_KEY` by
default). `tools/custom_brain.py` converts the same tool declarations Gemini
gets — its JSON-schema `type` values are upper-case (`OBJECT`, `STRING`),
OpenAI's want lower-case, so `_lower_types()` recursively fixes the tree —
and drives an identical turn-by-turn loop against that endpoint instead.
Hidden entirely until all three settings are present.

### `run_shell` is the important one

On Windows it runs **PowerShell**, and PowerShell is not just a command line —
it is the whole automation layer. Services, processes, the registry, WMI/CIM,
COM objects, window management, volume and media keys are all reachable from it.
The persona tells Jarvis to reach for `run_shell` before giving up on anything.

Scripts are sent via `-EncodedCommand` (base64 UTF-16LE), not as a quoted
string. Windows gives every process one flat command line and makes it parse
its own arguments, so passing a voice-transcribed script through that is a
quoting minefield where a stray apostrophe silently changes what runs. Base64
sidesteps the parser completely.

PowerShell also does not propagate a native command's exit code by default — run
a failing `.exe` via `-Command` and PowerShell still exits 0. Every script is
wrapped so `$LASTEXITCODE` is returned properly, otherwise every tool call would
report success.

### `open_app`

Tries `Start-Process` first, which handles anything on PATH or with a registered
App Path (`notepad`, `calc`, `code`, `chrome`). If that fails it falls back to
`Get-StartApps`, which is the only way to reach Microsoft Store apps — their
real identity is an AppUserModelID, not an `.exe` anywhere on disk.

## Memory

Three layers, all in one SQLite file (`data/jarvis.db`):

- **turns** — everything said. The prompt carries the most recent
  `storage.context_turns` (default 40); anything older is reachable via
  `search_history`, so memory is a recency buffer, not a limit.
- **facts** — durable things worth keeping (`remember` / `recall` / `forget`).
- **jobs** — background delegated tasks (Gemini agent or Claude Code) and
  their results, listed by `check_jobs`.

`memory.auto_facts` runs a cheap model over each finished conversation and
extracts durable facts automatically. Without it, memory only grows when the
model volunteers a `remember` call, which in practice it almost never does.

## Autonomy and the safety net

`agent.autonomy` is the single most important line in `config.yaml`.

- **`"full"`** (current default) — everything runs immediately, no
  confirmations. A misheard voice command CAN run something destructive.
- **`"guarded"`** — reads, screenshots and app launches run instantly.
  Anything matching the destructive-command list speaks a confirmation in
  Uzbek and waits for you to type `y` or `ha`.

The destructive list lives in `jarvis/tools/base.py` and covers `Remove-Item`
and its aliases, `format`, `diskpart`, `Stop-Computer`, `taskkill`,
`winget`/`choco` installs, registry writes under `HKLM:`, `Set-ExecutionPolicy`,
`bcdedit`, `schtasks`, account changes, `git push`/`reset --hard`/`clean`,
non-GET web requests, and `Invoke-Expression`. It errs toward catching too much:
a false positive costs one spoken "ha", a false negative can cost a directory.

It is covered by `tests/test_destructive.py`. If you add a pattern, add a test.

Every tool call lands in `logs/audit.jsonl` regardless of autonomy — twice, once
before it runs and once after, so a crash mid-command still leaves a trace.
The "after" entry logs the tool's actual returned payload (truncated to 2000
chars), so if Jarvis ever reports something wrong, this is where to check
what data it was actually working from.

## Audio: echo, and why Jarvis interrupts itself

This is the single most common problem. On laptop speakers the microphone picks
up Jarvis's own voice, Gemini's voice-activity detection reads that as you
interrupting, and it stops talking mid-sentence.

There are two independent fixes and they work together.

**Client side — `audio.echo_mode`:**

| mode | behaviour | use when |
|---|---|---|
| `mute` | mic is deaf while Jarvis speaks | **default**, laptop speakers |
| `gate` | measures echo loudness, only passes audio clearly louder | speakers, if you want barge-in |
| `open` | no gating at all | **headphones** |

`mute` is the shipped default. `gate` calibrates in the first ~130 ms after
audio is *queued*, and laptop speakers have not physically ramped up by then, so
it measures near-silence and sets the bar far too low.

**Server side — `gemini.vad.allow_interruption`:** when `false`, the server
refuses to let incoming audio cut off a reply in progress. Jarvis physically
cannot interrupt itself no matter what leaks into the mic. You lose the ability
to interrupt it too — set it to `true` once you are on headphones.

**On headphones, set `echo_mode: "open"` and `allow_interruption: true`.** That
is the good configuration; everything else is working around speaker bleed.

### Microphone sample rate

Gemini Live requires 16 kHz mono PCM. Windows WASAPI often refuses 16 kHz on
built-in laptop microphones. When that happens Jarvis opens the device at 48 kHz
and decimates 3:1 itself, so it works either way — but if audio sounds wrong,
this is the first place to look.

To pin a specific device:

```powershell
.venv\Scripts\python -m jarvis --devices
```

then put the integer index into `audio.input_device` / `audio.output_device`.

## Language

`gemini.language_code: "uz-UZ"` is pinned deliberately. Left unset, the model
auto-detects per utterance and reliably mistakes Uzbek for Spanish or Turkish,
because those are far commoner in its training data. It usually still gets your
*intent* right, but the transcript is wrong and it occasionally answers the
wrong question.

The cost is no automatic mid-sentence switching to Russian or English. Set it to
`null` if you would rather have the switching.

**Note:** only half-cascade Live models accept this field. The
`gemini-2.5-flash-native-audio-*` models reject `uz-UZ` and will fail to
connect — set it to `null` if you switch to one.

## The Aisha Uzbek voice

`tts.backend: "aisha"` routes spoken output through [Aisha AI](https://aisha.group)'s
Uzbek voice instead of Gemini's. The trade-off, stated plainly:

- **Better Uzbek accent** — the voice was actually trained on Uzbek.
- **Slower** — their API is request/response with no streaming, so expect
  +1.5–3 s per reply.
- **No barge-in** — the whole utterance exists before playback starts.

Understanding still runs on Gemini either way. Requires `AISHA_API_KEY` in
`.env` and **ffmpeg** on PATH:

```powershell
winget install Gyan.FFmpeg
```

Try `"gemini"` first. Only switch if the accent bothers you.

## The HUD overlay

A small frameless, always-on-top widget lives in a screen corner
(bottom-right by default — drag it anywhere, it remembers where you leave
it). It's a small dim dot while asleep, and physically **grows** into a
bigger, movie-style translucent glass panel the moment there's anything to
show, then contracts back to the dot once the conversation ends. No text is
ever drawn on it — it's a pure visual/motion indicator:

| State | Look |
|---|---|
| Asleep | A small dim dot, faded almost to nothing after ~2.5s of idling |
| Awake (wake word fired) | Grows into a wide glass panel: a steady cyan ring, pulsing gently |
| Running a tool | Panel stays open: a rotating amber comet-trail arc |
| Speaking | Panel stays open: a pulsing cyan ring of bars, like a compact equalizer |
| Error | Panel stays open: a brief red flash |

`config.yaml`'s `ui.overlay.size` is a base scale, not a fixed pixel size —
idle is ~0.45x it, the active panel is ~1.7x wide by ~0.85x tall. Growth is
centered on a fixed anchor point (wherever you last dropped it), clamped to
stay on-screen, so a wide panel never runs off the edge it's docked against.

It's driven by `jarvis/ui/bus.py`, a tiny state pub/sub that `app.py` and
`live.py` publish into at the same points they already log to the terminal —
sleeping/waking, tool calls, audio playback, turn completion, barge-in, and
session errors. The overlay is just one subscriber; nothing about the voice
loop depends on a HUD being attached.

Turn it off with `ui.overlay.enabled: false` in `config.yaml`. It needs
`PySide6` + `qasync` (both pulled in by `pip install -e .`); if either is
missing, `--doctor` flags it and Jarvis falls back to the plain console
daemon instead of failing to start — a missing GUI library should never cost
you the ability to talk to Jarvis.

## Troubleshooting

**`--doctor` says PowerShell not found.** Windows ships `powershell.exe`; if
this fails, your PATH or `SystemRoot` is unusual. Installing PowerShell 7 from
https://aka.ms/powershell fixes it and is better anyway.

**`WinError 193: %1 is not a valid Win32 application`.** Something tried to
execute a `.cmd` shim directly. `winplat.launch_argv` routes those through
`cmd /c`; if you add a new subprocess call, use it.

**Uzbek text shows as `?` or crashes with `UnicodeEncodeError`.** The console is
on cp1252. `winplat.setup_console()` forces UTF-8 and runs on import of
`__main__`; if you are embedding Jarvis elsewhere, call it yourself first.

**Jarvis cuts itself off mid-sentence.** Echo. See the audio section above.

**The wake word never fires.** Lower `wake.threshold` toward 0.4. Check
`--doctor` shows the right microphone, and that Windows has not muted it or
picked a different default device.

**The wake word fires constantly.** Raise `wake.threshold` toward 0.6–0.7.

**`503` from Gemini.** Preview models get overloaded. Turn-based calls walk
`gemini.text_model_fallbacks` automatically. The Live socket does not — it is
stateful, and silently reconnecting to a different model mid-conversation would
drop the audio context, so there a failure surfaces to you instead.

**`429 RESOURCE_EXHAUSTED` on a free-tier key.** Some models (e.g.
`gemini-2.5-flash`) cap free-tier usage at a small number of requests **per
day**, not per minute, so a busy session can exhaust it and every later call
that reaches it fails outright. `gemini.text_model_fallbacks` deliberately does
not include any such model by default — if you add one back in, expect this.

## Differences from the macOS original

The reference implementation is
[mukhitdinov0107/ai_agent](https://github.com/mukhitdinov0107/ai_agent). What
changed in this port:

| Area | macOS | Windows |
|---|---|---|
| Shell | `/bin/zsh` | PowerShell 7, falling back to `powershell.exe` |
| GUI automation | `osascript` / AppleScript | folded into `run_shell` (see below) |
| App launching | `open -a` | `Start-Process` + `Get-StartApps` |
| Opening URLs | `open <url>` | `os.startfile` |
| Notifications | `display notification` | Windows toast via `winotify` |
| Clipboard | `pbcopy` / `pbpaste` | `pyperclip` |
| Screenshots | `screencapture` | `mss` |
| Permissions | Accessibility, Screen Recording | microphone privacy setting only |

**The `applescript` tool is gone, not replaced.** On macOS, `run_shell` (zsh)
and `applescript` are genuinely different things. On Windows both collapse into
PowerShell, and shipping two tools that invoke the same interpreter would just
make the model dither over which to pick. The AppleScript use cases — driving
apps, window management, system automation — are advertised in `run_shell`'s
description instead.

**Not ported:** the Obsidian and Notion tools. The original hides unconfigured
integrations cleanly, so they can be added back without restructuring
anything.

**Telegram is ported, differently.** The macOS original's Telegram bridge
isn't documented here in enough detail to port as-is, so this port's
`send_telegram_message` (`jarvis/tools/telegram.py`) was built fresh on
Telethon (the MTProto *user* API): it logs in as the user via
`python -m jarvis --telegram-login` and can message anyone already in their
contacts or chat list, which a Bot API integration cannot do (a bot can only
message a chat that started the conversation with it).

**Default autonomy:** both the macOS original and this port currently ship
`autonomy: "full"` — no confirmation before destructive commands. Set it to
`"guarded"` in `config.yaml` if you'd rather have a spoken confirmation first.
