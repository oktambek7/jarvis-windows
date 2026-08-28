"""Process-wide state pub/sub, decoupling the voice loop from the HUD.

app.py and live.py already know exactly when Jarvis falls asleep, wakes up,
calls a tool or starts speaking — that instrumentation is what console.py's
Log class prints to the terminal. This module lets the same call sites also
publish that state for the overlay, without either side importing the other:
the voice loop never has to know whether a HUD is attached, and the overlay
never has to know how the voice loop works. No asyncio here on purpose —
publish() must be safely callable from plain sync code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class State(Enum):
    SLEEPING = "sleeping"    # waiting for the wake word
    LISTENING = "listening"  # a conversation is open, Jarvis is or may be heard
    TOOL = "tool"            # a tool call is in flight
    SPEAKING = "speaking"    # audio is playing back
    ERROR = "error"          # something just went wrong


@dataclass
class Event:
    state: State
    detail: str = ""


class StateBus:
    """One or more subscribers (normally: one overlay widget). Callbacks run
    synchronously on the publisher's thread/loop iteration — with qasync, the
    Qt event loop and the asyncio loop are the same thread, so this is safe to
    call directly from the voice loop with no marshalling.
    """

    def __init__(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []
        self.current = Event(State.SLEEPING)

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        self._subscribers.append(callback)
        callback(self.current)  # sync the new subscriber to current state

    def publish(self, state: State, detail: str = "") -> None:
        self.current = Event(state, detail)
        for callback in list(self._subscribers):
            try:
                callback(self.current)
            except Exception:  # noqa: BLE001 - a HUD glitch must never break the voice loop
                pass


bus = StateBus()
