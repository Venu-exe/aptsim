"""Loopback C2 client used by the network module and the standalone demo."""

from __future__ import annotations

import asyncio
from typing import Any

from aptsim.core.errors import ProtocolError

from .protocol import MAX_FRAME_BYTES, ProtocolCodec, ReplayGuard


class C2Client:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int,
        secret: str = "lab-only-shared-secret",
        session_id: str,
        timeout: float = 10.0,
    ) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("C2 client may only connect to loopback")
        self.host = host
        self.port = int(port)
        self.session_id = session_id
        self.timeout = timeout
        self.codec = ProtocolCodec(secret, replay_guard=ReplayGuard())
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self.request_count = 0

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=MAX_FRAME_BYTES + 1),
            timeout=self.timeout,
        )

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self._writer = None
            self._reader = None

    async def _request(self, message_type: str, **fields: Any) -> dict[str, Any]:
        if self._writer is None or self._reader is None:
            raise ProtocolError("client is not connected")
        async with self._lock:
            self.request_count += 1
            frame = self.codec.encode(message_type, session_id=self.session_id, **fields)
            self._writer.write(frame)
            await self._writer.drain()
            line = await asyncio.wait_for(self._reader.readline(), timeout=self.timeout)
            if not line:
                raise ProtocolError("server closed the connection")
            response = self.codec.decode(line)
        if response.get("type") == "error":
            raise ProtocolError(f"server error {response.get('code')}: {response.get('message')}")
        return response

    async def register(self, capabilities: list[str]) -> dict[str, Any]:
        return await self._request("register", capabilities=capabilities)

    async def heartbeat(self, seq: int) -> dict[str, Any]:
        return await self._request("heartbeat", seq=seq)

    async def poll(self) -> dict[str, Any]:
        return await self._request("poll")

    async def send_result(
        self,
        task_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "result", task_id=task_id, status=status, result=result, error=error
        )
