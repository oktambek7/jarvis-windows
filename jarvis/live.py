"""The Gemini Live session — Jarvis's ears and mouth.

One WebSocket carries microphone audio up and spoken audio down, with function
calls interleaved. Three concurrent tasks run inside a session:

  _pump_mic   mic queue -> socket
  _receive    socket -> speakers, transcripts, tool calls
  _watchdog   closes the session after a stretch of silence

Tool calls are dispatched on their own tasks. That matters: a 60-second shell
command must not block the receive loop, or audio playback stutters and
barge-in stops working while it runs.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from google import genai
from google.genai import types

from .persona import build_system_prompt


class LiveSession:
    def __init__(self, cfg, client: genai.Client, registry, memory, audio, audit, tts, log) -> None:
        self.cfg = cfg
        self.client = client
        self.registry = registry
        self.memory = memory
        self.audio = audio
        self.audit = audit
        self.tts = tts
        self.log = log

        self.model = str(cfg.get("gemini.model", "gemini-3.1-flash-live-preview"))
        self.silence_timeout = float(cfg.get("wake.silence_timeout", 12.0))

        self._session: Any = None
        self._last_activity = time.monotonic()
        self._stop = asyncio.Event()
        # Set if the server rejects Search grounding alongside our function
        # declarations, so we stop asking for it instead of failing every turn.
        self._search_unsupported = False
        self._retry_without_search = False
        self.session_started_at = time.time()

        # Partial transcripts, flushed to memory on turn_complete.
        self._user_buf: list[str] = []
        self._model_buf: list[str] = []

    # ------------------------------------------------------------- config

    def _live_config(self) -> types.LiveConnectConfig:
        prompt = build_system_prompt(
            self.memory.context_block(int(self.cfg.get("storage.context_turns", 20)))
        )

        tools: list[types.Tool] = []
        # Google Search grounding: lets Jarvis actually ANSWER factual questions
        # ("bugun havo qanaqa?") instead of opening a browser tab at you.
        if self.cfg.get("gemini.google_search", True) and not self._search_unsupported:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
        tools.append(types.Tool(function_declarations=self.registry.gemini_declarations()))

        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality(self.tts.modality)],
            system_instruction=types.Content(parts=[types.Part(text=prompt)]),
            tools=tools,
            # Keep a long conversation from blowing the context window; the
            # model summarises older turns instead of the socket dying.
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
        )

        # Server-side half of the echo fix. The client gate stops most echo
        # reaching Google at all; this makes Gemini conservative about calling
        # whatever does get through "the user interrupting".
        vad = self.cfg.get("gemini.vad") or {}
        if vad:
            detection = types.AutomaticActivityDetection()
            start = str(vad.get("start_sensitivity", "")).upper()
            end = str(vad.get("end_sensitivity", "")).upper()
            if start in {"HIGH", "LOW"}:
                detection.start_of_speech_sensitivity = types.StartSensitivity(
                    f"START_SENSITIVITY_{start}"
                )
            if end in {"HIGH", "LOW"}:
                detection.end_of_speech_sensitivity = types.EndSensitivity(
                    f"END_SENSITIVITY_{end}"
                )
            if vad.get("prefix_padding_ms") is not None:
                detection.prefix_padding_ms = int(vad["prefix_padding_ms"])
            if vad.get("silence_duration_ms") is not None:
                detection.silence_duration_ms = int(vad["silence_duration_ms"])
            realtime = types.RealtimeInputConfig(automatic_activity_detection=detection)

            # The decisive echo fix. With NO_INTERRUPTION the server will not
            # let incoming audio cut off a reply in progress — so Jarvis
            # physically cannot interrupt itself, whatever leaks into the mic.
            # Cost: you can't interrupt it either. Set allow_interruption: true
            # once you're on headphones.
            if not vad.get("allow_interruption", False):
                realtime.activity_handling = types.ActivityHandling.NO_INTERRUPTION

            cfg.realtime_input_config = realtime

        if self.cfg.get("gemini.transcripts", True):
            cfg.input_audio_transcription = types.AudioTranscriptionConfig()
            if self.tts.modality == "AUDIO":
                cfg.output_audio_transcription = types.AudioTranscriptionConfig()

        # Voice selection only applies when Gemini is doing the speaking.
        if self.tts.modality == "AUDIO":
            voice = str(self.cfg.get("gemini.voice", "Charon"))
            speech = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            )
            # Pinning the language stops Uzbek being transcribed as Spanish.
            # Native-audio models reject this field — leave it null for those.
            language = self.cfg.get("gemini.language_code")
            if language:
                speech.language_code = str(language)
            cfg.speech_config = speech

        return cfg

    # ------------------------------------------------------------- public

    async def converse(self, seed_text: str | None = None) -> None:
        """Hold one conversation, returning when the user goes quiet."""
        self._stop.clear()
        self._touch()
        # Wall-clock, not monotonic: reflection queries the DB by timestamp.
        self.session_started_at = time.time()

        # Drop audio captured before the wake word so the session doesn't
        # open by hearing "hey Jarvis" and replying to it.
        self._drain_mic()

        await self._run_session(seed_text)

        # If the very first connect was rejected for asking Search grounding
        # and function calling together, drop Search and try once more rather
        # than leaving the user with a dead wake word.
        if self._retry_without_search:
            self._retry_without_search = False
            self._search_unsupported = True
            self.log.warn("Search grounding qabul qilinmadi — usiz qayta ulandim.")
            self._stop.clear()
            self._touch()
            await self._run_session(seed_text)

    async def _run_session(self, seed_text: str | None = None) -> None:
        try:
            async with self.client.aio.live.connect(
                model=self.model, config=self._live_config()
            ) as session:
                self._session = session
                self.log.session_open()

                if seed_text:
                    await session.send_realtime_input(text=seed_text)

                tasks = [
                    asyncio.create_task(self._pump_mic(session), name="mic"),
                    asyncio.create_task(self._receive(session), name="recv"),
                    asyncio.create_task(self._watchdog(), name="watchdog"),
                ]
                try:
                    await self._stop.wait()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as exc:  # noqa: BLE001
            # Distinguish "this config is invalid" from "the network died".
            # Only the former is worth retrying with Search switched off, and
            # only if the session never opened — a mid-conversation drop isn't
            # the tool config's fault.
            searching = (
                self.cfg.get("gemini.google_search", True) and not self._search_unsupported
            )
            never_opened = self._session is None
            # Search grounding is billed/gated separately from base model
            # quota, so a key with plenty of quota otherwise still gets
            # quota-rejected the instant Search is attached — same failure
            # as the hard "not supported" one, just phrased as quota/billing.
            blames_tools = any(
                word in str(exc).lower()
                for word in ("tool", "google_search", "search", "invalid", "not supported", "quota", "billing")
            )
            if searching and never_opened and blames_tools:
                self._retry_without_search = True
            else:
                self.log.error(f"Live session error: {type(exc).__name__}: {exc}")
                self.audit.note("voice", "live_session_error", error=str(exc))
        finally:
            self._session = None
            self.audio.interrupt()
            self._flush_transcripts()
            self.log.session_close()

    async def announce(self, message: str) -> bool:
        """Have Jarvis say something unprompted (e.g. a background job landed).

        Returns False if no session is live, so the caller can fall back to a
        desktop notification.
        """
        session = self._session
        if session is None:
            return False
        try:
            await session.send_realtime_input(
                text=(
                    f"[TIZIM XABARI] {message}\n"
                    "Buni foydalanuvchiga o'zbek tilida, bir jumlada qisqacha yetkaz."
                )
            )
            self._touch()
            return True
        except Exception:
            return False

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ internals

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _drain_mic(self) -> None:
        while True:
            try:
                self.audio.mic_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _pump_mic(self, session) -> None:
        rate = int(self.cfg.get("audio.input_rate", 16000))
        mime = f"audio/pcm;rate={rate}"
        while not self._stop.is_set():
            chunk = await self.audio.mic_queue.get()
            try:
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=mime)
                )
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"mic send failed: {exc}")
                self._stop.set()
                return

    async def _watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
            if self.audio.is_speaking:
                self._touch()
                continue
            if time.monotonic() - self._last_activity > self.silence_timeout:
                self.log.info("Sukunat — uxlash rejimiga qaytdim.")
                self._stop.set()
                return

    async def _receive(self, session) -> None:
        try:
            # session.receive() is a PER-TURN iterator — the SDK breaks out of
            # it as soon as it sees turn_complete. Without this outer loop the
            # session stays open but stops listening after the first answer,
            # and the silence watchdog then puts Jarvis to sleep. Re-entering
            # receive() is what makes the conversation continue.
            while not self._stop.is_set():
                await self._receive_turn(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"receive loop ended: {type(exc).__name__}: {exc}")
            self._stop.set()

    async def _receive_turn(self, session) -> None:
        """Consume exactly one model turn. Returns when the turn completes."""
        async for response in session.receive():
            self._touch()

            if response.tool_call:
                # Fire and forget: the receive loop must stay responsive.
                asyncio.create_task(self._handle_tools(session, response.tool_call))

            content = response.server_content
            if content is None:
                continue

            # Barge-in: the user started talking over Jarvis. Everything
            # already buffered for playback is now stale — drop it.
            if getattr(content, "interrupted", False):
                self.audio.interrupt()
                self.log.interrupted()

            if getattr(content, "input_transcription", None):
                text = content.input_transcription.text or ""
                if text:
                    self._user_buf.append(text)
                    self.log.user_partial(text)

            if getattr(content, "output_transcription", None):
                text = content.output_transcription.text or ""
                if text:
                    self._model_buf.append(text)

            turn = getattr(content, "model_turn", None)
            if turn and turn.parts:
                for part in turn.parts:
                    # A single event can carry audio AND text — handle both,
                    # never elif, or you silently drop one of them.
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        self.audio.play(part.inline_data.data)
                    if getattr(part, "text", None):
                        self._model_buf.append(part.text)

            if getattr(content, "turn_complete", False):
                await self._on_turn_complete()

    async def _on_turn_complete(self) -> None:
        spoken = "".join(self._model_buf).strip()

        # Non-native backends get text from the model and speak it themselves.
        if spoken and not self.tts.native:
            await self.tts.speak(spoken)

        if spoken:
            self.log.assistant(spoken)
        self._flush_transcripts()

    def _flush_transcripts(self) -> None:
        user_text = "".join(self._user_buf).strip()
        model_text = "".join(self._model_buf).strip()
        if user_text:
            self.memory.add_turn("voice", "user", user_text)
        if model_text:
            self.memory.add_turn("voice", "assistant", model_text)
        self._user_buf.clear()
        self._model_buf.clear()

    async def _handle_tools(self, session, tool_call) -> None:
        responses = []
        for fc in tool_call.function_calls:
            args = dict(fc.args or {})
            self.log.tool(fc.name, args)

            result = await self.registry.invoke(fc.name, args, surface="voice")
            self.memory.add_turn(
                "voice", "tool", f"{fc.name}({args}) -> {str(result)[:500]}"
            )
            responses.append(
                types.FunctionResponse(id=fc.id, name=fc.name, response=result)
            )

        if not responses:
            return
        try:
            await session.send_tool_response(function_responses=responses)
            self._touch()
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"tool response failed: {exc}")
