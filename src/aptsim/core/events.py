"""Thread-safe, asynchronous event bus with pluggable sinks.

Design notes
------------
* Events are immutable dataclasses with a monotonic sequence number and a
  wall-clock timestamp plus a session-relative offset used by the timeline
  renderer.
* The bus is drained by a single writer task, which guarantees that every sink
  observes the same global ordering. Producers may run in worker threads; they
  hand events to the loop with ``call_soon_threadsafe``.
* Sinks are simple synchronous objects guarded by their own locks, so a slow
  disk never corrupts an event stream.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    ts: float
    t_rel: float
    session_id: str
    category: str
    action: str
    module: str
    severity: str = "info"
    correlation: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventSink(Protocol):
    def write(self, event: Event) -> None: ...

    def close(self) -> None: ...


class JSONLFileSink:
    """Append-only JSON Lines sink. Flushes each event for crash resilience."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)

    def write(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()


class MemorySink:
    """In-memory sink used by unit tests and by the timeline renderer."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._lock = threading.Lock()

    def write(self, event: Event) -> None:
        with self._lock:
            self.events.append(event)

    def close(self) -> None:
        return None

    def find(self, *, category: str | None = None, action: str | None = None) -> list[Event]:
        with self._lock:
            return [
                event
                for event in self.events
                if (category is None or event.category == category)
                and (action is None or event.action == action)
            ]


class PrettySink:
    """Human readable console sink; ANSI colours only on a TTY."""

    COLOURS = {
        "debug": "\033[90m",
        "info": "\033[36m",
        "warning": "\033[33m",
        "error": "\033[31m",
        "critical": "\033[41;97m",
    }
    RESET = "\033[0m"

    def __init__(self, stream: Any = None, *, colour: bool | None = None, verbose: bool = False) -> None:
        import sys

        self.stream = stream or sys.stdout
        self.colour = self.stream.isatty() if colour is None else colour
        self.verbose = verbose

    def write(self, event: Event) -> None:
        if event.severity == "debug" and not self.verbose:
            return
        prefix = f"+{event.t_rel:8.3f}s"
        tag = f"{event.category}.{event.action}"
        line = f"[{prefix}] {tag:<34} {event.module:<14} {self._summary(event)}"
        if self.colour:
            colour = self.COLOURS.get(event.severity, "")
            line = f"{colour}{line}{self.RESET}"
        print(line, file=self.stream)

    @staticmethod
    def _summary(event: Event) -> str:
        if not event.data:
            return ""
        interesting = ("message", "path", "task_id", "status", "score", "count", "reason", "error")
        parts = [f"{key}={event.data[key]}" for key in interesting if key in event.data]
        if not parts:
            parts = [f"{key}={value}" for key, value in list(event.data.items())[:4]]
        text = " ".join(str(part) for part in parts)
        return text if len(text) <= 110 else text[:107] + "..."

    def close(self) -> None:
        return None


class EventBus:
    """Async fan-out bus with a single ordered writer task."""

    def __init__(
        self,
        session_id: str,
        sinks: list[EventSink] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session_id = session_id
        self._sinks: list[EventSink] = list(sinks or [])
        self._subscribers: list[Callable[[Event], Awaitable[None]]] = []
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._clock = clock
        self._start_ts = clock()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._buffer: deque[Event] = deque(maxlen=4096)
        self._writer: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self.dropped = 0

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self._writer is None:
            self._loop = asyncio.get_running_loop()
            self._writer = asyncio.create_task(self._drain(), name="eventbus-writer")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            await self._queue.put(None)
            await self._writer
        for sink in self._sinks:
            sink.close()

    # -- producers ---------------------------------------------------------
    def _next_event(
        self,
        category: str,
        action: str,
        module: str,
        severity: str,
        data: dict[str, Any] | None,
        correlation: str | None,
    ) -> Event:
        now = self._clock()
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        return Event(
            seq=seq,
            ts=now,
            t_rel=round(now - self._start_ts, 6),
            session_id=self.session_id,
            category=category,
            action=action,
            module=module,
            severity=severity,
            correlation=correlation,
            data=dict(data or {}),
        )

    async def emit(
        self,
        category: str,
        action: str,
        module: str,
        *,
        severity: str = "info",
        data: dict[str, Any] | None = None,
        correlation: str | None = None,
    ) -> Event:
        event = self._next_event(category, action, module, severity, data, correlation)
        if self._closed:
            self.dropped += 1
            return event
        self._buffer.append(event)
        await self._queue.put(event)
        return event

    def emit_threadsafe(
        self,
        category: str,
        action: str,
        module: str,
        *,
        severity: str = "info",
        data: dict[str, Any] | None = None,
        correlation: str | None = None,
    ) -> Event:
        """Emit from a worker thread. Requires the bus to have been started."""
        event = self._next_event(category, action, module, severity, data, correlation)
        if self._closed:
            self.dropped += 1
            return event
        self._buffer.append(event)
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            self.dropped += 1
            return event
        loop.call_soon_threadsafe(self._queue.put_nowait, event)
        return event

    # -- consumers ---------------------------------------------------------
    def subscribe(self, callback: Callable[[Event], Awaitable[None]]) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def replay(self) -> list[Event]:
        return list(self._buffer)

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            for sink in self._sinks:
                try:
                    sink.write(event)
                except Exception:  # pragma: no cover - sink failure must not kill the bus
                    self.dropped += 1
            for subscriber in list(self._subscribers):
                try:
                    await subscriber(event)
                except Exception:  # pragma: no cover
                    self.dropped += 1
        # Drain anything that raced with close().
        while not self._queue.empty():
            event = self._queue.get_nowait()
            if event is None:
                continue
            for sink in self._sinks:
                sink.write(event)

    def __enter__(self) -> "EventBus":  # pragma: no cover - convenience only
        self._loop = asyncio.get_event_loop()
        return self
