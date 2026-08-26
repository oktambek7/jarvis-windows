"""Speech-output backends.

Default is Gemini's native audio: the Live model emits PCM directly on the same
WebSocket it's listening on, so there is no separate TTS step and no extra
latency. `speak()` is a no-op there — audio arrives through the receive loop.

The Aisha backend exists for one reason: if Gemini's Uzbek accent isn't good
enough for you, you can route spoken output through a voice that was actually
trained on Uzbek. You pay for it in latency. Flip tts.backend in config.yaml.
"""

from __future__ import annotations

from .base import NativeTTS, TTSBackend


def build_tts(cfg, audio) -> TTSBackend:
    backend = str(cfg.get("tts.backend", "gemini")).lower()

    if backend == "aisha":
        from .aisha import AishaTTS

        key = cfg.aisha_key
        if not key:
            raise RuntimeError(
                "tts.backend is 'aisha' but AISHA_API_KEY is missing from .env.\n"
                "Either add the key or set tts.backend back to 'gemini'."
            )
        return AishaTTS(cfg, audio, key)

    return NativeTTS()


__all__ = ["build_tts", "TTSBackend", "NativeTTS"]
