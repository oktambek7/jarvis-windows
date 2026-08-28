"""Shared delivery logic for tools that hand work to another agent (Claude
Code, Jarvis's own Gemini tool loop) and must report back without freezing
the conversation.

Both backends can run for minutes. Blocking the whole Live turn on a slow
task made Jarvis go silent mid-conversation until the task finished — which
read as "so slow" and, on anything that ran past the model's patience, as
"never did it at all". Requiring the model to correctly predict
`background=true` up front was unreliable, so it wasn't a fix either.

Instead every delegated task gets a short grace period to finish inline —
most tasks are done well within it, so the reply lands right in the
conversation like any other tool call — and past that it is silently
promoted to background: the caller is told "still working" immediately and
the real result is delivered later via `announce`. Either way the result is
never lost, which is the part that was actually broken.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

# How long a delegated task is allowed to hold up the conversation before
# Jarvis stops waiting and switches to "I'll tell you when it's done".
GRACE_SECONDS = 12.0


async def deliver(
    ctx: dict,
    surface: str,
    task: str,
    cwd: str,
    background: bool,
    runner: Callable[[], Awaitable[tuple[bool, str]]],
    backend: str,
) -> dict:
    """Run `runner` (which does the actual work) and report back.

    Returns the dict to hand back to the model as the tool result. If the
    task doesn't finish within the grace period (or `background` was set),
    the real result is delivered later through `ctx["announce"]` instead.
    """
    memory = ctx.get("memory")
    job_id = memory.job_start(surface, task, cwd) if memory else -1

    async def _finish(ok: bool, text: str, announce_late: bool) -> None:
        if memory:
            memory.job_finish(job_id, ok, text)
        if announce_late:
            announce = ctx.get("announce")
            if announce:
                prefix = "Vazifa bajarildi" if ok else "Vazifa bajarilmadi"
                await announce(f"{prefix} ({backend}): {text}")

    job_task: asyncio.Task[tuple[bool, str]] = asyncio.create_task(runner())

    if background:
        asyncio.create_task(_await_and_finish(job_task, _finish))
        return {
            "ok": True,
            "job_id": job_id,
            "result": (
                "Vazifa fonda ishga tushirildi. Tugagach xabar beraman. "
                "Tell the user you've started it and will report back."
            ),
        }

    done, _pending = await asyncio.wait({job_task}, timeout=GRACE_SECONDS)
    if job_task in done:
        ok, text = job_task.result()
        await _finish(ok, text, announce_late=False)
        return {"ok": ok, "job_id": job_id, ("result" if ok else "error"): text}

    # Grace period elapsed. Stop blocking the conversation but let the task
    # keep running — its result still lands, just via an announcement.
    asyncio.create_task(_await_and_finish(job_task, _finish))
    return {
        "ok": True,
        "job_id": job_id,
        "result": (
            "Vazifa hali davom etyapti, fonda tugataman va tugagach xabar "
            "beraman. Tell the user this is taking a bit longer and you'll "
            "let them know the moment it's done."
        ),
    }


async def _await_and_finish(job_task: asyncio.Task, finish: Callable) -> None:
    ok, text = await job_task
    await finish(ok, text, announce_late=True)
