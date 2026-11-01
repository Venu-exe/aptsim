"""Newline-delimited JSON protocol with HMAC-SHA256 authentication.

Every frame is a JSON object signed over its canonical form. Frames carry a
protocol version, a timestamp and a nonce. The receiver rejects unsigned or
tampered frames, stale frames, replayed nonces and oversized frames.

The transport is loopback TCP only; the codec itself has no notion of the
network and is trivially unit-testable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any, Callable

from aptsim.core.errors import ProtocolError

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
MAX_CLOCK_SKEW = 30.0


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ReplayGuard:
    def __init__(self, window: float = MAX_CLOCK_SKEW * 2) -> None:
        self.window = window
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, nonce: str, ts: float) -> None:
        now = time.time()
        with self._lock:
            cutoff = now - self.window
            self._seen = {key: value for key, value in self._seen.items() if value >= cutoff}
            if nonce in self._seen:
                raise ProtocolError(f"replayed nonce: {nonce}")
            self._seen[nonce] = ts

    def __len__(self) -> int:
        return len(self._seen)


class ProtocolCodec:
    def __init__(
        self,
        key: bytes | str,
        *,
        max_frame: int = MAX_FRAME_BYTES,
        max_age: float = MAX_CLOCK_SKEW,
        clock: Callable[[], float] = time.time,
        replay_guard: ReplayGuard | None = None,
    ) -> None:
        self.key = key.encode("utf-8") if isinstance(key, str) else key
        if len(self.key) < 8:
            raise ValueError("shared secret must be at least 8 bytes")
        self.max_frame = max_frame
        self.max_age = max_age
        self.clock = clock
        self.replay_guard = replay_guard or ReplayGuard()

    def sign(self, body: dict[str, Any]) -> str:
        return hmac.new(self.key, canonical(body), hashlib.sha256).hexdigest()

    def encode(self, message_type: str, **fields: Any) -> bytes:
        body: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": message_type,
            "ts": self.clock(),
            "nonce": secrets.token_hex(8),
        }
        for key, value in fields.items():
            if key in body:
                raise ProtocolError(f"reserved protocol field: {key}")
            body[key] = value
        envelope = dict(body)
        envelope["auth"] = self.sign(body)
        data = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) > self.max_frame:
            raise ProtocolError(f"frame too large: {len(data)} > {self.max_frame}")
        return data + b"\n"

    def decode(self, line: bytes | str) -> dict[str, Any]:
        raw = line.encode("utf-8") if isinstance(line, str) else line
        if len(raw) > self.max_frame:
            raise ProtocolError(f"frame too large: {len(raw)} > {self.max_frame}")
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"malformed JSON frame: {exc}") from exc
        if not isinstance(envelope, dict):
            raise ProtocolError("frame must be a JSON object")
        auth = envelope.pop("auth", None)
        if not isinstance(auth, str):
            raise ProtocolError("missing frame signature")
        expected = self.sign(envelope)
        if not hmac.compare_digest(auth, expected):
            raise ProtocolError("invalid frame signature")
        if envelope.get("v") != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {envelope.get('v')!r}")
        message_type = envelope.get("type")
        if not isinstance(message_type, str) or not message_type:
            raise ProtocolError("missing message type")
        try:
            ts = float(envelope.get("ts"))
        except (TypeError, ValueError) as exc:
            raise ProtocolError("missing or invalid timestamp") from exc
        now = self.clock()
        if now - ts > self.max_age:
            raise ProtocolError(f"stale frame ({now - ts:.1f}s old)")
        if ts - now > self.max_age:
            raise ProtocolError(f"frame timestamp too far in the future ({ts - now:.1f}s)")
        nonce = envelope.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise ProtocolError("missing nonce")
        self.replay_guard.check(nonce, ts)
        return envelope


def make_codec(secret: str | bytes, *, max_age: float = MAX_CLOCK_SKEW) -> ProtocolCodec:
    return ProtocolCodec(secret, max_age=max_age)
