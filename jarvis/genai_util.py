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


def agent_config(cfg, system_instruction: str | None = None):
    """Build the GenerateContentConfig used by every turn-based surface.

    Google Search and our own function declarations can only be sent together
    when tool_config.include_server_side_tool_invocations is set — without it
    the API returns 400. Getting this wrong means the CLI path silently loses
    the ability to look anything up, which is how the weather and
    exchange-rate questions failed.
    """
    from google.genai import types

    from .tools import registry

    tools = []
    if cfg.get("gemini.google_search", True):
        tools.append(types.Tool(google_search=types.GoogleSearch()))
    tools.append(types.Tool(function_declarations=registry.gemini_declarations()))

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
        for attempt in range(attempts_per_model):
            try:
                response = await client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
                if index > 0 and log:
                    log.info(f"(fallback modeli ishlatildi: {model})")
                return response

            except (errors.ServerError, errors.ClientError) as exc:
                code = getattr(exc, "code", None)
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
