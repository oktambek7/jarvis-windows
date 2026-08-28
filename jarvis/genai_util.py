"""Resilient text generation.

Preview models get overloaded and return 503 for minutes at a time. A voice
assistant that dies because Google is busy is not an assistant, so every
turn-based call walks a list of models: the preferred one first, then
progressively more boring and more available ones.

The Live socket is deliberately NOT covered here — it's a stateful WebSocket,
and silently reconnecting it to a different model mid-conversation would drop
the audio context. There, a failure surfaces to the user instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

from google.genai import errors

# Codes worth trying again or trying elsewhere.
RETRYABLE = {429, 500, 502, 503, 504}


def _without_search_tool(config):
    """Drop the Google Search tool, for keys/models that reject combining it
    with our function declarations. Returns None if there was no search tool
    to drop.
    """
    if config is None or not getattr(config, "tools", None):
        return None
    tools = [t for t in config.tools if getattr(t, "google_search", None) is None]
    if len(tools) == len(config.tools):
        return None
    return config.model_copy(update={"tools": tools, "tool_config": None})


def _blames_search_tool(exc: Exception) -> bool:
    """Whether Search grounding is the real culprit behind this failure.

    Some keys/tiers can't use Search grounding at all, and that shows up
    differently per model: a hard 400 ("tool call context circulation is
    not enabled") on older models, or a 429 "exceeded your quota... billing"
    on newer ones — Search grounding is billed/gated separately from base
    model quota, so a key with plenty of quota for plain generation can
    still get quota-rejected the instant Search is attached.
    """
    text = str(exc).lower()
    return "tool call context circulation" in text or ("quota" in text and "billing" in text)


def agent_config(cfg, system_instruction: str | None = None, exclude_tools: set[str] | None = None):
    """Build the GenerateContentConfig used by every turn-based surface.

    Google Search and our own function declarations can only be sent together
    when tool_config.include_server_side_tool_invocations is set — without it
    the API returns 400. Getting this wrong means the CLI path silently loses
    the ability to look anything up, which is how the weather and
    exchange-rate questions failed.

    `exclude_tools` drops named tools from the declarations sent to the model.
    Used by delegate_to_gemini's own sub-loop so its agent can't call
    delegate_to_gemini (or delegate_to_claude) on itself and recurse.
    """
    from google.genai import types

    from .tools import registry

    declarations = registry.gemini_declarations()
    if exclude_tools:
        declarations = [d for d in declarations if d["name"] not in exclude_tools]

    tools = []
    if cfg.get("gemini.google_search", True):
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    tools.append(types.Tool(function_declarations=declarations))

    config = types.GenerateContentConfig(
        tools=tools,
        # We drive the tool loop ourselves so every call hits the audit log.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    if system_instruction:
        config.system_instruction = system_instruction
    if len(tools) > 1:
        config.tool_config = types.ToolConfig(include_server_side_tool_invocations=True)
    return config


def model_chain(cfg, primary: str | None = None) -> list[str]:
    first = primary or str(cfg.get("gemini.text_model", "gemini-3.7-flash"))
    chain = [first]
    for fallback in cfg.get("gemini.text_model_fallbacks", []) or []:
        if fallback not in chain:
            chain.append(str(fallback))
    return chain


async def generate(
    client,
    cfg,
    contents: Any,
    config: Any = None,
    primary: str | None = None,
    log=None,
    attempts_per_model: int = 2,
):
    """generate_content with retry-then-downgrade. Raises the last error if all fail."""
    last: Exception | None = None

    for index, model in enumerate(model_chain(cfg, primary)):
        active_config = config
        for attempt in range(attempts_per_model):
            try:
                response = await client.aio.models.generate_content(
                    model=model, contents=contents, config=active_config
                )
                if index > 0 and log:
                    log.info(f"(fallback modeli ishlatildi: {model})")
                return response

            except (errors.ServerError, errors.ClientError) as exc:
                code = getattr(exc, "code", None)
                if _blames_search_tool(exc):
                    stripped = _without_search_tool(active_config)
                    if stripped is not None:
                        active_config = stripped
                        if log:
                            log.warn(f"{model} qidiruvni funksiyalar bilan birga qo'llamaydi — qidiruvsiz qayta urinildi")
                        continue
                if code not in RETRYABLE:
                    raise
                last = exc
                # Back off within a model before writing it off entirely.
                if attempt + 1 < attempts_per_model:
                    await asyncio.sleep(0.8 * (attempt + 1))

        if log:
            log.warn(f"{model} javob bermadi ({getattr(last, 'code', '?')}) — keyingisiga o'tdim")

    assert last is not None
    raise last
