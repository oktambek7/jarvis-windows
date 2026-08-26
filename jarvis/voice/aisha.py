"""Aisha AI text-to-speech (Uzbek).

Only used when tts.backend = "aisha". Trade-off, stated plainly:

  + A voice actually trained on Uzbek. Better accent than a general
    multilingual model.
  - Their API is request/response, no streaming and no WebSocket. You POST
    text, wait for synthesis, download a WAV, then play it. That's ~1.5-3 s
    added per reply, and barge-in becomes impossible because the whole
    utterance exists before playback starts.

Understanding still runs on Gemini either way — Aisha's STT is also
non-streaming, so putting it on the input side would be much worse.

Endpoint: POST https://back.aisha.group/api/v1/tts/post/  (X-Api-Key header)
"""

from __future__ import annotations

import asyncio
import re

import aiohttp

API_BASE = "https://back.aisha.group"
TTS_ENDPOINT = f"{API_BASE}/api/v1/tts/post/"


def split_for_tts(text: str, limit: int) -> list[str]:
    """Break text into <=limit chunks on sentence boundaries where possible."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return [text] if text else []

    sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        # A single sentence longer than the limit gets hard-split on words.
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()

        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= limit:
            current += " " + sentence
        else:
            chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)
    return [c for c in chunks if c]


class AishaTTS:
    native = False
    modality = "TEXT"  # ask Gemini for text; we do the speaking

    def __init__(self, cfg, audio, api_key: str) -> None:
        self._audio = audio
        self._key = api_key
        self._model = str(cfg.get("tts.aisha.model", "Gulnoza"))
        self._mood = str(cfg.get("tts.aisha.mood", "Neutral"))
        self._speed = float(cfg.get("tts.aisha.speed", 1.0))
        self._limit = int(cfg.get("tts.aisha.max_chars", 1000))
        self._out_rate = int(cfg.get("audio.output_rate", 24000))
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-Api-Key": self._key},
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self._session

    async def _synthesize(self, chunk: str) -> bytes | None:
        """Return WAV bytes for one chunk, or None on failure."""
        http = await self._http()

        form = aiohttp.FormData()
        form.add_field("transcript", chunk)
        form.add_field("language", "uz")
        form.add_field("model", self._model)
        form.add_field("mood", self._mood)
        form.add_field("speed", str(self._speed))

        async with http.post(TTS_ENDPOINT, data=form) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise RuntimeError(f"Aisha TTS {resp.status}: {body}")
            payload = await resp.json()

        audio_path = payload.get("audio_path")
        if not audio_path:
            # Async mode (returns a task id) is only triggered by supplying a
            # webhook, which we never do — so this means something changed.
            raise RuntimeError(f"Aisha returned no audio_path: {payload}")

        url = audio_path if audio_path.startswith("http") else API_BASE + audio_path
        async with http.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Aisha audio download failed: {resp.status}")
            return await resp.read()

    async def _to_pcm(self, wav: bytes) -> bytes:
        """Transcode whatever Aisha sent into the raw PCM our speaker wants."""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(self._out_rate), "-ac", "1",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(wav)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {err.decode()[:200]}")
        return out

    async def speak(self, text: str) -> None:
        if not text or not text.strip():
            return

        # Synthesise chunk-by-chunk and play each as it arrives, so a long
        # answer starts speaking after the first chunk instead of all of it.
        for chunk in split_for_tts(text, self._limit):
            try:
                wav = await self._synthesize(chunk)
                if wav:
                    self._audio.play(await self._to_pcm(wav))
            except Exception as exc:  # noqa: BLE001 - never let TTS kill the session
                print(f"[aisha] {exc}")
                return

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
