"""Tests for the retry/fallback logic that drives every turn-based Gemini
call. Two behaviours matter for how fast Jarvis feels in practice:

1. A hard daily quota cap (RESOURCE_EXHAUSTED) must not be retried a second
   time on the same model — that retry is guaranteed to fail identically
   and just adds latency before Jarvis moves on or gives up.
2. Once a model is learned to reject Search grounding alongside our tools,
   later calls must skip straight to the no-search config instead of
   re-paying that round trip every single turn.
"""

from __future__ import annotations

import asyncio

from google.genai import errors

from jarvis import genai_util
from jarvis.genai_util import _is_daily_quota_exhausted, generate


def _client_error(code: int, message: str) -> errors.ClientError:
    return errors.ClientError(
        code,
        {"error": {"code": code, "message": message, "status": "RESOURCE_EXHAUSTED"}},
    )


class _FakeConfig:
    """Stand-in for GenerateContentConfig: just enough for the search-tool
    stripping logic to recognise a "has search tool" vs "stripped" config.
    """

    def __init__(self, tools):
        self.tools = tools
        self.tool_config = "set"

    def model_copy(self, update):
        merged = {"tools": self.tools, "tool_config": self.tool_config}
        merged.update(update)
        out = _FakeConfig(merged["tools"])
        out.tool_config = merged["tool_config"]
        return out


class _SearchTool:
    google_search = object()


class _FnTool:
    google_search = None


def test_daily_quota_exhaustion_detected():
    exc = _client_error(429, "You exceeded your current quota, please check your plan and billing details.")
    assert _is_daily_quota_exhausted(exc)


def test_daily_quota_exhaustion_not_confused_with_plain_rate_limit():
    exc = _client_error(429, "Rate limit exceeded, please slow down.")
    assert not _is_daily_quota_exhausted(exc)


class _FakeModels:
    """Records how many times generate_content is actually called, so the
    test can assert the retry loop didn't pay for a doomed second attempt.
    """

    def __init__(self, fail_times: int, exc_factory):
        self.calls = 0
        self.fail_times = fail_times
        self.exc_factory = exc_factory

    async def generate_content(self, model, contents, config):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return f"ok:{model}:{config is not None and config.tools}"


class _FakeAio:
    def __init__(self, models):
        self.models = models


class _FakeClient:
    def __init__(self, models):
        self.aio = _FakeAio(models)


class _FakeCfg:
    def __init__(self, data):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


def test_quota_exhaustion_skips_second_attempt_on_same_model():
    """generate_content should be called once for the exhausted model, then
    once for the fallback — never twice for the same exhausted model.
    """
    quota_exc = lambda: _client_error(  # noqa: E731
        429, "You exceeded your current quota, please check your plan and billing details."
    )
    models = _FakeModels(fail_times=1, exc_factory=quota_exc)
    client = _FakeClient(models)
    cfg = _FakeCfg({"gemini.text_model": "primary", "gemini.text_model_fallbacks": ["fallback"]})

    response = asyncio.run(generate(client, cfg, contents=[], config=None, attempts_per_model=2))

    assert response == "ok:fallback:False"
    assert models.calls == 2  # one failed attempt on "primary", one success on "fallback"


def test_search_unsupported_model_is_remembered_across_calls():
    genai_util._search_unsupported_models.clear()

    search_exc = lambda: _client_error(  # noqa: E731
        429, "You exceeded your current quota, please check your plan and billing details."
    )
    models = _FakeModels(fail_times=1, exc_factory=search_exc)
    client = _FakeClient(models)
    cfg = _FakeCfg({"gemini.text_model": "flaky-model", "gemini.text_model_fallbacks": []})
    config = _FakeConfig([_SearchTool(), _FnTool()])

    # First call: pays for one failed attempt with search, then strips it
    # and succeeds — same as _blames_search_tool's existing behaviour.
    first = asyncio.run(generate(client, cfg, contents=[], config=config, attempts_per_model=2))
    assert first.startswith("ok:flaky-model:")
    assert "flaky-model" in genai_util._search_unsupported_models
    calls_after_first = models.calls
    assert calls_after_first == 2  # one failed (with search), one succeeded (without)

    # Second call on the same (now-learned) model: should go straight to
    # the no-search config and succeed on the FIRST attempt.
    second = asyncio.run(
        generate(
            client, cfg, contents=[], config=_FakeConfig([_SearchTool(), _FnTool()]), attempts_per_model=2
        )
    )
    assert second is not None
    assert models.calls == calls_after_first + 1

    genai_util._search_unsupported_models.clear()
