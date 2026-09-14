from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# The half-cascade Live models (what `gemini.model` is pinned to) only accept
# 8 prebuilt voices: Puck, Charon, Kore, Fenrir, Aoede, Leda, Orus, Zephyr.
# These two are the defaults for `gemini.voice_gender`. Fenrir and Orus were
# both tried first and came back sounding noticeably accented and harder to
# follow in Uzbek. Charon is Google's "Informative" voice — even, measured
# diction closer to a newsreader than the others — and that steadier pacing
# is what actually reads as clear and understandable across languages,
# Uzbek included, rather than any of them being Uzbek-native. Kore stays the
# firm, confident female voice.
VOICE_BY_GENDER: dict[str, str] = {
    "male": "Charon",
    "female": "Kore",
}


def resolve_voice_name(cfg: Any) -> str:
    """The Gemini Live prebuilt voice name Jarvis speaks with.

    `gemini.voice` pins an exact voice and always wins when set. Otherwise
    `gemini.voice_gender` ("male" | "female") picks the default for that
    persona. This is the one place that decision is made, so the startup
    banner (console.py) and the actual Live session (live.py) never disagree.
    """
    explicit = cfg.get("gemini.voice")
    if explicit:
        return str(explicit)
    gender = str(cfg.get("gemini.voice_gender", "male")).lower()
    return VOICE_BY_GENDER.get(gender, VOICE_BY_GENDER["male"])


class TTSBackend(ABC):
    #: True when the Live model produces audio itself and there is no
    #: separate synthesis step to wait on.
    native: bool = False

    #: Response modality to request from the Live API for this backend.
    modality: str = "AUDIO"

    @abstractmethod
    async def speak(self, text: str) -> None:
        """Render text to the speakers. No-op for native backends."""

    async def aclose(self) -> None:
        return None


class NativeTTS(TTSBackend):
    """Gemini Live speaks for itself — audio arrives on the receive loop."""

    native = True
    modality = "AUDIO"

    async def speak(self, text: str) -> None:
        return None
