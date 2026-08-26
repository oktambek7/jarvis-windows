"""Automatic fact extraction — how Jarvis comes to actually know you.

The `remember` tool exists, but relying on the model to volunteer it doesn't
work: after a full day of heavy use exactly one fact had been stored. Durable
memory can't depend on the assistant remembering to remember.

So after each conversation a cheap text model reads what was SAID (never tool
output) and pulls out anything durable — where projects live, who people are,
stable preferences, ongoing work. Those get upserted into the facts table and
are injected into every future session on every surface.

Runs in the background after the session closes, so it never adds latency to a
conversation. Failure is silent by design: losing a fact is a small regression,
crashing the wake loop is not.
"""

from __future__ import annotations

import asyncio
import json
import time

from google.genai import types

from .genai_util import generate

# Don't bother reflecting on "hey jarvis" / "what time is it".
MIN_TURNS = 4
# Debounce for chat surfaces, where turns arrive one at a time.
MIN_INTERVAL_SECONDS = 300

_FACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "facts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "key": {"type": "STRING"},
                    "value": {"type": "STRING"},
                },
                "required": ["key", "value"],
            },
        }
    },
    "required": ["facts"],
}

_PROMPT = """\
Quyida foydalanuvchi va uning ovozli yordamchisi o'rtasidagi suhbat.

Vazifang: suhbatdan FAQAT uzoq muddat foydali bo'ladigan faktlarni ajratib ol.

OLINADI:
- Shaxsiy ma'lumot: ism, tug'ilgan kun, shahar, oila, do'stlar
- Ish va loyihalar: loyiha nomi, papka yo'li, texnologiyalar, muddatlar
- Barqaror odatlar va afzalliklar: qaysi muharrir, qaysi til, ish vaqti
- Davom etayotgan rejalar: nima qilmoqchi, nimani kutayapti

OLINMAYDI:
- Bir martalik so'rovlar ("batareya necha foiz", "faylni och")
- Texnik xatolar, tool natijalari, vaqtinchalik holatlar
- Yordamchining o'z gaplari

Kalit (key) — qisqa, ingliz tilida, snake_case. Qiymat (value) — bir qator,
o'zbekcha, o'zi tushunarli bo'lsin.

Agar hech qanday barqaror fakt bo'lmasa, bo'sh ro'yxat qaytar.

ALLAQACHON ESLAB QOLINGANLARI (bularni takrorlama, faqat aniqroq bo'lsa yangila):
{existing}

SUHBAT:
{transcript}
"""


async def extract_facts(client, cfg, memory, log, since_ts: float) -> int:
    """Pull durable facts out of everything said since `since_ts`. Returns count."""
    turns = memory.session_transcript(since_ts)
    if len(turns) < MIN_TURNS:
        return 0

    transcript = "\n".join(
        f"{'FOYDALANUVCHI' if t['role'] == 'user' else 'YORDAMCHI'}: {t['text'][:500]}"
        for t in turns
    )[:12000]

    existing = memory.all_facts()
    existing_text = (
        "\n".join(f"- {k}: {v}" for k, v in existing.items()) if existing else "(hozircha yo'q)"
    )

    try:
        response = await generate(
            client,
            cfg,
            contents=_PROMPT.format(existing=existing_text, transcript=transcript),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_FACT_SCHEMA,
                temperature=0.1,
            ),
        )
        payload = json.loads(response.text or "{}")
    except Exception as exc:  # noqa: BLE001 - never let reflection break the loop
        if log:
            log.info(f"(xotira tahlili o'tkazib yuborildi: {type(exc).__name__})")
        return 0

    saved = 0
    for fact in payload.get("facts", []):
        key = str(fact.get("key", "")).strip()
        value = str(fact.get("value", "")).strip()
        if not key or not value:
            continue
        # Skip no-op rewrites so the log stays meaningful.
        if existing.get(key) == value:
            continue
        memory.remember(key, value)
        saved += 1

    if saved and log:
        log.info(f"(xotiraga {saved} ta yangi fakt qo'shildi)")
    return saved


class Reflector:
    """Owns the debounce so callers can fire freely without thinking about it."""

    def __init__(self, client, cfg, memory, log) -> None:
        self._client = client
        self._cfg = cfg
        self._memory = memory
        self._log = log
        self._last_run = 0.0
        self._running = False

    def schedule(self, since_ts: float, force: bool = False) -> None:
        """Fire-and-forget. Skips if one is already in flight or too recent."""
        if self._running:
            return
        if not force and (time.time() - self._last_run) < MIN_INTERVAL_SECONDS:
            return
        if not self._cfg.get("memory.auto_facts", True):
            return
        asyncio.create_task(self._run(since_ts))

    async def _run(self, since_ts: float) -> None:
        self._running = True
        self._last_run = time.time()
        try:
            await extract_facts(self._client, self._cfg, self._memory, self._log, since_ts)
        finally:
            self._running = False
