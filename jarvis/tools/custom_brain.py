"""Hand hard work to a pluggable OpenAI-compatible model instead of Gemini or
Claude — a "bring your own brain" slot, for anyone who wants delegated tasks
to run on a different model/provider than the two Jarvis ships with, without
spending Gemini or Claude quota.

Any provider that speaks the OpenAI chat-completions API (with tool calling)
works here: point `custom_model.base_url` at it, name the model, and put its
key in the env var `custom_model.api_key_env` names. Hidden entirely until
all three are set — offering a tool that always 401s is worse than not
having it.

Division of labour, alongside the other delegate_to_* tools:
  Gemini Live          = ears, mouth, reflexes.
  delegate_to_gemini   = default hands, spends Gemini quota.
  delegate_to_claude   = fallback hands, spends Claude quota.
  delegate_to_openclaw = hands via a separate self-hosted OpenClaw gateway.
  delegate_to_custom_model = hands via whatever OpenAI-compatible model/
                        endpoint you've configured — spends neither Gemini's
                        nor Claude's quota, and is what the Telegram bot uses
                        by default when telegram_bot.brain is "custom".
"""

from __future__ import annotations

import json
import os

import aiohttp

from .agentwork import deliver
from .base import registry

# How many tool-call round trips a delegated task gets before it must be done.
MAX_STEPS = 20

AGENT_CONTEXT = (
    "You are operating as the execution engine for a voice/text assistant "
    "called Jarvis. Do the work fully and autonomously — you have full tool "
    "access; do not ask for permission. Give your final answer as 1-3 short, "
    "clear sentences describing what you did and the result. If you could "
    "not finish, say plainly what blocked you."
)

# Tools hidden from this sub-agent's own declarations so it can't delegate to
# itself (or another backend) and recurse instead of doing the work.
_SELF_TOOLS = {
    "delegate_to_custom_model",
    "delegate_to_gemini",
    "delegate_to_claude",
    "delegate_to_openclaw",
    "check_jobs",
}


def enabled(cfg) -> bool:
    return bool(cfg.get("custom_model.enabled", True))


def base_url(cfg) -> str:
    return str(cfg.get("custom_model.base_url", "") or "").rstrip("/")


def model_name(cfg) -> str:
    return str(cfg.get("custom_model.model", "") or "")


def api_key(cfg) -> str | None:
    env_name = str(cfg.get("custom_model.api_key_env", "CUSTOM_MODEL_API_KEY"))
    value = os.getenv(env_name, "").strip()
    return value or None


def configured(cfg) -> bool:
    return enabled(cfg) and bool(base_url(cfg)) and bool(model_name(cfg)) and api_key(cfg) is not None


def _lower_types(schema):
    """Gemini's tool declarations use upper-case JSON-schema type names
    (OBJECT, STRING, ...); OpenAI's tool-calling format wants the standard
    lower-case ones. Recursively fix the whole parameter tree so the same
    registry declarations can serve both backends.
    """
    if isinstance(schema, dict):
        return {
            k: (v.lower() if k == "type" and isinstance(v, str) else _lower_types(v))
            for k, v in schema.items()
        }
    if isinstance(schema, list):
        return [_lower_types(v) for v in schema]
    return schema


def _openai_tools(exclude: set[str]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": decl["name"],
                "description": decl["description"],
                "parameters": _lower_types(decl["parameters"]),
            },
        }
        for decl in registry.gemini_declarations()
        if decl["name"] not in exclude
    ]


async def _chat(session: aiohttp.ClientSession, cfg, messages: list[dict], tools: list[dict]) -> dict:
    url = f"{base_url(cfg)}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key(cfg)}", "Content-Type": "application/json"}
    body = {"model": model_name(cfg), "messages": messages, "tools": tools, "tool_choice": "auto"}
    timeout = aiohttp.ClientTimeout(total=float(cfg.get("custom_model.timeout", 120)))
    async with session.post(url, headers=headers, json=body, timeout=timeout) as resp:
        text = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {text[:500]}")
        return json.loads(text)


async def run_custom_agent(
    cfg,
    task: str,
    log=None,
    *,
    context: str = AGENT_CONTEXT,
    max_steps: int | None = None,
    exclude_tools: set[str] = _SELF_TOOLS,
    surface: str = "custom-brain",
) -> tuple[bool, str]:
    """Drive Jarvis's own tool registry through a configured OpenAI-compatible
    model's tool-calling loop. Shared by delegate_to_custom_model and the
    Telegram bot (when telegram_bot.brain is "custom")."""
    if not configured(cfg):
        return False, "Custom model sozlanmagan (custom_model.base_url/model va API kaliti kerak)."

    tools = _openai_tools(exclude_tools)
    messages: list[dict] = [
        {"role": "system", "content": context},
        {"role": "user", "content": task},
    ]

    steps = max_steps or int(cfg.get("custom_model.max_steps", MAX_STEPS))
    async with aiohttp.ClientSession() as session:
        for _ in range(steps):
            try:
                data = await _chat(session, cfg, messages, tools)
            except Exception as exc:  # noqa: BLE001 - a crashed agent must not kill the caller
                return False, f"Custom model xato qaytardi: {exc}"

            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                text = (message.get("content") or "").strip()
                return True, text or "Vazifa bajarildi."

            messages.append(message)
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if log:
                    log.tool(name, args)
                result = await registry.invoke(name, args, surface=surface)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

    return False, "Juda ko'p qadam kerak bo'ldi — vazifa belgilangan chegarada tugallanmadi."


@registry.tool(
    name="delegate_to_custom_model",
    description=(
        "Hand a substantial task to a pluggable, self-configured OpenAI-"
        "compatible model instead of Gemini or Claude — use only when the "
        "user explicitly asks for it (e.g. 'boshqa model orqali', 'custom "
        "model bilan'), or when preserving Gemini/Claude quota matters more "
        "than which model does the work. Otherwise prefer delegate_to_gemini. "
        "Set background=true for anything expected to take over a minute."
    ),
    parameters={
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": (
                    "Full, self-contained instructions. This model cannot see "
                    "the conversation, so restate all needed context and the "
                    "definition of done."
                ),
            },
            "background": {
                "type": "BOOLEAN",
                "description": "Run without blocking the conversation. Default false.",
            },
        },
        "required": ["task"],
    },
)
async def delegate_to_custom_model(
    task: str, ctx: dict, surface: str = "voice", background: bool = False
) -> dict:
    cfg = ctx["config"]
    if not configured(cfg):
        return {
            "ok": False,
            "error": "Custom model sozlanmagan — config.yaml dagi custom_model bo'limini to'ldiring.",
        }

    log = ctx.get("log")

    async def _runner() -> tuple[bool, str]:
        return await run_custom_agent(cfg, task, log)

    return await deliver(ctx, surface, task, "custom-model", background, _runner, backend="CustomModel")
