"""Hand hard work to Jarvis's own Gemini tool loop, instead of Claude Code.

Division of labour, updated for a Gemini "GO" plan where Gemini tokens are
the abundant resource and Claude's are the scarce one:

  Gemini Live    = ears, mouth, reflexes. One tool call at a time, sub-second,
                    kept snappy on purpose.
  Gemini agent   = same brain, same tools (run_shell, open_app, file I/O,
                    UI automation, ...), but driven turn-by-turn through a
                    private text call so it can chain many steps without the
                    Live session's one-or-two-sentence budget. Default hands
                    for anything substantial, because it spends Gemini quota
                    instead of Claude's.
  Claude Code    = fallback hands, still available via delegate_to_claude —
                    for when the Gemini agent gives up, or the user explicitly
                    asks for Claude.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.genai import types

from .agentwork import deliver
from .base import registry

# How many tool-call round trips the sub-agent gets before it has to be done.
# Higher than the --text CLI loop's 8: this backend now carries tasks that
# used to go to Claude Code, which routinely takes more steps.
MAX_STEPS = 20

# What the sub-agent is told about the context it's running in. Mirrors
# delegate.py's VOICE_CONTEXT so the two backends read the same to the user
# regardless of which one actually did the work.
AGENT_CONTEXT = (
    "Siz Jarvis ovozli yordamchisining ICHKI BAJARUVCHI rejimidasiz — "
    "foydalanuvchi bilan bevosita gaplashmayapsiz, sizga bitta aniq vazifa "
    "berilgan va uni oxirigacha o'zingiz bajarishingiz kerak.\n"
    "- Ruxsat so'ramang; avval bajaring, keyin natijani ayting.\n"
    "- Har qadamda kerakli asbobni chaqiring, taxmin qilmang.\n"
    "- OXIRGI xabaringiz ovoz bilan o'qiladi: faqat 1-3 qisqa o'zbekcha "
    "jumla bilan nima qilganingizni va natijani ayting. Markdown, kod bloki "
    "yoki ro'yxat ishlatmang — ularni eshitib bo'lmaydi.\n"
    "- Tugata olmasangiz, nima to'sqinlik qilganini ochiq ayting."
)

# Tools hidden from the sub-agent's own declarations so it can't delegate to
# itself (or to Claude) and recurse instead of doing the work.
_SELF_TOOLS = {"delegate_to_gemini", "delegate_to_claude", "check_jobs"}


async def run_gemini_agent(
    cfg,
    client,
    log,
    task: str,
    cwd: str,
    *,
    context: str = AGENT_CONTEXT,
    max_steps: int = MAX_STEPS,
    exclude_tools: set[str] | None = _SELF_TOOLS,
    surface: str = "gemini-agent",
) -> tuple[bool, str]:
    """Drive Jarvis's own tool registry through Gemini's turn-by-turn loop.

    Generalized out of delegate_to_gemini so other entry points (the
    Telegram bot) can run the exact same loop with their own system prompt,
    step budget and audit surface, instead of a second copy of this loop.
    """
    from .. import genai_util

    config = genai_util.agent_config(cfg, context, exclude_tools=exclude_tools)
    contents = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=f"Ishchi papka: {cwd}\n\nVazifa: {task}")],
        )
    ]

    for _ in range(max_steps):
        try:
            response = await genai_util.generate(client, cfg, contents, config, log=log)
        except Exception as exc:  # noqa: BLE001 - a crashed agent must not kill the caller
            return False, f"Gemini agent xato qaytardi: {exc}"

        calls = response.function_calls or []
        if not calls:
            text = (response.text or "").strip()
            return True, text or "Vazifa bajarildi."

        contents.append(response.candidates[0].content)
        results = []
        for fc in calls:
            args = dict(fc.args or {})
            if log:
                log.tool(fc.name, args)
            result = await registry.invoke(fc.name, args, surface=surface)
            results.append(types.Part.from_function_response(name=fc.name, response=result))
        contents.append(types.Content(role="user", parts=results))

    return False, "Juda ko'p qadam kerak bo'ldi — vazifa belgilangan chegarada tugallanmadi."


@registry.tool(
    name="delegate_to_gemini",
    description=(
        "Hand a substantial multi-step task to Jarvis's own Gemini-powered "
        "agent, which chains many tool calls (run_shell, open_app, file I/O, "
        "UI automation via PowerShell, etc.) to get a task done end to end. "
        "This spends Gemini quota, not Claude's — PREFER THIS over "
        "delegate_to_claude for the same class of task: writing or editing "
        "code, fixing bugs, multi-step desktop automation (e.g. finding a "
        "contact and sending them a message inside an app), research across "
        "files, installing or configuring something, anything needing several "
        "steps. Only reach for delegate_to_claude if this fails, or the user "
        "explicitly asks for Claude. Do NOT use either for one-liners like "
        "checking the time or opening a single app (use run_shell / open_app); "
        "looking something up or opening a link (use google_search / "
        "open_url). Set background=true for anything expected to take over a "
        "minute; the user will be notified when it finishes rather than "
        "waiting in silence."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": (
                    "Full, self-contained instructions (Uzbek or English). The "
                    "agent cannot see the conversation, so restate all needed "
                    "context, names, file paths and the definition of done."
                ),
            },
            "cwd": {
                "type": "STRING",
                "description": "Working directory. Defaults to the user's home.",
            },
            "background": {
                "type": "BOOLEAN",
                "description": "Run without blocking the conversation. Default false.",
            },
        },
        "required": ["task"],
    },
)
async def delegate_to_gemini(
    task: str, ctx: dict, surface: str = "voice", cwd: str | None = None, background: bool = False
) -> dict:
    cfg = ctx["config"]
    client = ctx.get("genai_client")
    if client is None:
        return {"ok": False, "error": "Gemini client unavailable."}

    workdir = Path(os.path.expandvars(cwd or str(cfg.get("claude.default_cwd", "~")))).expanduser()
    if not workdir.is_dir():
        return {"ok": False, "error": f"Directory does not exist: {workdir}"}

    log = ctx.get("log")

    async def _runner() -> tuple[bool, str]:
        return await run_gemini_agent(cfg, client, log, task, str(workdir))

    return await deliver(ctx, surface, task, str(workdir), background, _runner, backend="Gemini")
