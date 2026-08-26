from __future__ import annotations

from abc import ABC, abstractmethod


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
