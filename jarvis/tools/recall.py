"""Memory tools — how Jarvis keeps things between conversations.

The Live session's own context dies when the session closes. These tools write
to the shared SQLite store, so a fact learned by voice is still there tomorrow.
"""

from __future__ import annotations

from .base import registry


@registry.tool(
    name="remember",
    description=(
        "Store a durable fact about the user so it survives across sessions and "
        "surfaces. Use when the user says 'remember that...', 'esla', 'yodda "
        "tut', or states a stable preference (their editor, main project path, "
        "wife's name, work hours). Do NOT use for one-off chatter."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "key": {
                "type": "STRING",
                "description": "Short snake_case identifier, e.g. 'main_project_path'.",
            },
            "value": {"type": "STRING", "description": "The fact itself, one line."},
        },
        "required": ["key", "value"],
    },
)
async def remember(key: str, value: str, ctx: dict) -> dict:
    ctx["memory"].remember(key, value)
    return {"ok": True, "result": f"Esladim: {key}"}


@registry.tool(
    name="recall",
    description=(
        "Look up a previously stored fact. Omit 'key' to list everything Jarvis "
        "remembers about the user."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {"key": {"type": "STRING", "description": "Optional. Omit to list all."}},
    },
)
async def recall(ctx: dict, key: str | None = None) -> dict:
    memory = ctx["memory"]
    if key:
        value = memory.recall(key)
        if value is None:
            return {"ok": True, "result": f"'{key}' haqida hech narsa eslamayman."}
        return {"ok": True, "key": key, "value": value}
    facts = memory.all_facts()
    return {"ok": True, "facts": facts or "Hozircha hech narsa eslab qolmaganman."}


@registry.tool(
    name="search_history",
    description=(
        "Search everything ever said in ANY past conversation. The prompt "
        "only carries the most recent turns, so use this "
        "whenever the user refers to something older: 'ertalab nima "
        "gaplashgandik', 'o'sha aytgan narsam', 'what did we discuss about X', "
        "'senga aytgandim-ku'. Do not claim you don't remember without "
        "searching first."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "query": {"type": "STRING", "description": "A distinctive word or phrase to look for."},
            "limit": {"type": "NUMBER", "description": "Max results. Default 12."},
        },
        "required": ["query"],
    },
)
async def search_history(query: str, ctx: dict, limit: float = 12) -> dict:
    hits = ctx["memory"].search_turns(query, int(limit))
    if not hits:
        return {"ok": True, "result": f"'{query}' haqida suhbatlarimizda hech narsa topilmadi."}
    return {"ok": True, "count": len(hits), "hits": hits}


@registry.tool(
    name="forget",
    description="Delete a stored fact by key. Use when the user says to forget something.",
    parameters={
        "type": "OBJECT",
        "properties": {"key": {"type": "STRING"}},
        "required": ["key"],
    },
)
async def forget(key: str, ctx: dict) -> dict:
    removed = ctx["memory"].forget(key)
    return {"ok": True, "result": f"O'chirdim: {key}" if removed else f"'{key}' topilmadi."}
