"""Microphone in, speaker out.

Two details here matter more than the rest:

1. ECHO. The Mac's speakers feed straight back into the Mac's microphone, so
   Jarvis hears itself talking and interrupts itself in a loop. Headphones make
   this vanish. If you're on laptop speakers, set audio.echo_mode to "mute" —
   you lose barge-in, but Jarvis stops arguing with itself.

2. THREADS. PortAudio callbacks fire on their own thread, not the event loop.
   Everything crossing that boundary goes through call_soon_threadsafe.
"""

from __future__ import annotations

import asyncio
import math
import struct
import threading

import numpy as np
import sounddevice as sd

# openWakeWord wants 80 ms frames of 16 kHz int16.
WAKE_FRAME_SAMPLES = 1280

# Frames at the start of each spoken reply used to measure how loud the echo
# of Jarvis's own voice is in the mic. The user is very unlikely to interrupt
# in the first ~100 ms, so these are treated as pure echo and dropped.
ECHO_CALIBRATION_FRAMES = 4


def _rms(pcm: bytes) -> float:
    arr = np.frombuffer(pcm, dtype=np.int16)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))


class AudioIO:
    def __init__(self, cfg) -> None:
        self.in_rate = int(cfg.get("audio.input_rate", 16000))
        self.out_rate = int(cfg.get("audio.output_rate", 24000))
        self.chunk_ms = int(cfg.get("audio.chunk_ms", 32))
        self.in_device = cfg.get("audio.input_device")
        self.out_device = cfg.get("audio.output_device")

        # How to stop Jarvis hearing itself. See config.yaml for the trade-offs.
        self.echo_mode = str(cfg.get("audio.echo_mode", "gate")).lower()
        self._gate_mult = float(cfg.get("audio.echo_gate_multiplier", 2.5))
        self._gate_min_rms = float(cfg.get("audio.echo_gate_min_rms", 900))
        self._echo_floor = 0.0
        self._echo_calibrated = 0

        self.mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_stream: sd.RawInputStream | None = None
        self._out_stream: sd.RawOutputStream | None = None

        # Playback buffer, drained by the output callback thread.
        self._play_buf = bytearray()
        self._play_lock = threading.Lock()
        self._speaking = threading.Event()

        # If the device refuses 16 kHz we open higher and decimate.
        self._native_in_rate = self.in_rate
        self._resample_ratio = 1

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._open_input()
        self._open_output()

    def _open_input(self) -> None:
        blocksize = int(self.in_rate * self.chunk_ms / 1000)
        try:
            self._in_stream = sd.RawInputStream(
                samplerate=self.in_rate,
                blocksize=blocksize,
                device=self.in_device,
                channels=1,
                dtype="int16",
                callback=self._on_mic,
            )
            self._in_stream.start()
        except Exception:
            # CoreAudio sometimes won't hand out 16 kHz on the built-in mic.
            # Open at 48 kHz and decimate 3:1 ourselves.
            self._native_in_rate = 48000
            self._resample_ratio = self._native_in_rate // self.in_rate
            self._in_stream = sd.RawInputStream(
                samplerate=self._native_in_rate,
                blocksize=blocksize * self._resample_ratio,
                device=self.in_device,
                channels=1,
                dtype="int16",
                callback=self._on_mic,
            )
            self._in_stream.start()

    def _open_output(self) -> None:
        self._out_stream = sd.RawOutputStream(
            samplerate=self.out_rate,
            blocksize=int(self.out_rate * self.chunk_ms / 1000),
            device=self.out_device,
            channels=1,
            dtype="int16",
            callback=self._on_speaker,
        )
        self._out_stream.start()

    def stop(self) -> None:
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    # ------------------------------------------------------------ callbacks

    def _echo_ok(self, pcm: bytes) -> bool:
        """Should this frame be forwarded while Jarvis is speaking?

        "gate" is the interesting mode. Speaker echo arriving back at the mic is
        much quieter than someone actually talking into it, so we measure the
        echo level at the start of each reply and then only pass audio that is
        clearly louder than that. You keep barge-in without headphones.
        """
        if self.echo_mode == "open":
            return True
        if self.echo_mode == "mute":
            return False

        level = _rms(pcm)

        # First few frames of a reply: assume pure echo, learn its loudness.
        if self._echo_calibrated < ECHO_CALIBRATION_FRAMES:
            self._echo_floor = max(self._echo_floor, level)
            self._echo_calibrated += 1
            return False

        threshold = max(self._echo_floor * self._gate_mult, self._gate_min_rms)
        return level > threshold

    def _on_mic(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        if self._loop is None or self._loop.is_closed():
            return

        pcm = bytes(indata)
        if self._resample_ratio > 1:
            arr = np.frombuffer(pcm, dtype=np.int16)
            # Mean-of-N decimation: cheap low-pass, good enough for speech and
            # far cheaper than a proper polyphase filter in a realtime callback.
            usable = (len(arr) // self._resample_ratio) * self._resample_ratio
            if usable == 0:
                return
            arr = arr[:usable].reshape(-1, self._resample_ratio).mean(axis=1)
            pcm = arr.astype(np.int16).tobytes()

        # Drop our own voice bleeding back in, so Gemini's VAD doesn't read it
        # as the user interrupting and cut Jarvis off mid-sentence.
        if self._speaking.is_set() and not self._echo_ok(pcm):
            return

        try:
            self._loop.call_soon_threadsafe(self._push_mic, pcm)
        except RuntimeError:
            pass

    def _push_mic(self, pcm: bytes) -> None:
        if self.mic_queue.full():
            # Drop the oldest frame rather than stalling the audio thread.
            try:
                self.mic_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.mic_queue.put_nowait(pcm)

    def _on_speaker(self, outdata, frames, time_info, status) -> None:  # PortAudio thread
        need = frames * 2  # int16 mono
        with self._play_lock:
            available = len(self._play_buf)
            take = min(need, available)
            chunk = bytes(self._play_buf[:take])
            del self._play_buf[:take]
            still_speaking = len(self._play_buf) > 0

        if take < need:
            chunk += b"\x00" * (need - take)
        outdata[:] = chunk

        if still_speaking:
            self._speaking.set()
        else:
            self._speaking.clear()

    # ------------------------------------------------------------ playback

    def play(self, pcm: bytes) -> None:
        """Queue 24 kHz mono int16 PCM for playback."""
        if not pcm:
            return
        with self._play_lock:
            starting = not self._play_buf
            self._play_buf.extend(pcm)
        # New utterance: re-measure the echo floor. Speaker volume and what's
        # around the laptop change between replies.
        if starting:
            self._echo_floor = 0.0
            self._echo_calibrated = 0
        self._speaking.set()

    def interrupt(self) -> None:
        """Barge-in: drop everything not yet played, immediately."""
        with self._play_lock:
            self._play_buf.clear()
        self._speaking.clear()
        self._echo_floor = 0.0
        self._echo_calibrated = 0

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    async def wait_until_quiet(self, timeout: float = 30.0) -> None:
        """Block until the playback buffer drains (used by half-duplex paths)."""
        waited = 0.0
        while self._speaking.is_set() and waited < timeout:
            await asyncio.sleep(0.05)
            waited += 0.05

    def chime(self, freq: float = 880.0, ms: int = 120, volume: float = 0.18) -> None:
        """Short blip so you know the wake word registered."""
        n = int(self.out_rate * ms / 1000)
        samples = bytearray()
        for i in range(n):
            # Fade the tail so it doesn't click.
            envelope = min(1.0, (n - i) / (n * 0.35))
            value = int(32767 * volume * envelope * math.sin(2 * math.pi * freq * i / self.out_rate))
            samples += struct.pack("<h", value)
        self.play(bytes(samples))


class WakeWordDetector:
    """openWakeWord wrapper. Feeds 16 kHz audio, fires on 'hey jarvis'."""

    def __init__(self, cfg) -> None:
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        self.name = str(cfg.get("wake.model", "hey_jarvis"))
        self.threshold = float(cfg.get("wake.threshold", 0.5))

        download_models([self.name])  # no-op once cached
        self._model = Model(wakeword_models=[self.name], inference_framework="onnx")
        self._buf = np.zeros(0, dtype=np.int16)
        # After a hit, ignore audio briefly so one utterance doesn't fire twice.
        self._cooldown = 0

    def feed(self, pcm: bytes) -> bool:
        """Push mic audio. Returns True the moment the wake word is detected."""
        self._buf = np.concatenate([self._buf, np.frombuffer(pcm, dtype=np.int16)])
        fired = False

        while len(self._buf) >= WAKE_FRAME_SAMPLES:
            frame = self._buf[:WAKE_FRAME_SAMPLES]
            self._buf = self._buf[WAKE_FRAME_SAMPLES:]

            if self._cooldown > 0:
                self._cooldown -= 1
                continue

            scores = self._model.predict(frame)
            if scores.get(self.name, 0.0) >= self.threshold:
                fired = True
                self._cooldown = 25  # ~2 s of frames
                self.reset()
                break

        return fired

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)
        try:
            self._model.reset()
        except Exception:
            pass
