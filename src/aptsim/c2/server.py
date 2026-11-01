"""Loopback-only simulated C2 server.

The server is a deterministic scenario player: it authenticates clients, tracks
sessions, serves heartbeat acknowledgements, hands out scripted task
descriptors and records results. It has no operator console and no ability to
accept new task definitions at runtime.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aptsim.core.errors import ProtocolError

from .protocol import MAX_FRAME_BYTES, ProtocolCodec, ReplayGuard
from .scenario import ScenarioPlayer

EventHook = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class SessionRecord:
    session_id: str
    peer: str
    capabilities: list[str] = field(default_factory=list)
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    heartbeats: int = 0
    polls: int = 0
    tasks_sent: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "peer": self.peer,
            "capabilities": self.capabilities,
            "registered_at": self.registered_at,
            "last_seen": self.last_seen,
            "heartbeats": self.heartbeats,
            "polls": self.polls,
            "tasks_sent": self.tasks_sent,
            "results": self.results[-20:],
        }


class C2Server:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        secret: str = "lab-only-shared-secret",
        tasks: list[dict[str, Any]] | None = None,
        tasks_per_poll: int = 2,
        on_event: EventHook | None = None,
        heartbeat_interval: float = 1.0,
        idle_timeout: float = 60.0,
    ) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("C2 server may only bind to loopback")
        self.host = host
        self.requested_port = int(port)
        self.codec = ProtocolCodec(secret, replay_guard=ReplayGuard())
        self.scenario = ScenarioPlayer(list(tasks or []), tasks_per_poll=tasks_per_poll)
        self.on_event = on_event
        self.heartbeat_interval = heartbeat_interval
        self.idle_timeout = idle_timeout
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._sessions: dict[str, SessionRecord] = {}
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.host,
            port=self.requested_port,
            limit=MAX_FRAME_BYTES + 1,
        )
        sockets = self._server.sockets or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        await self._emit(
            "server.started",
            {"host": self.host, "port": self.port, "tasks": len(self.scenario.tasks), "loopback": True},
        )
        return self.port

    async def stop(self) -> None:
        for writer in list(self._writers):
            writer.close()
        for writer in list(self._writers):
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self._emit("server.stopped", {"port": self.port})

    def active_sessions(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self._sessions.values()]

    async def _emit(self, action: str, data: dict[str, Any]) -> None:
        if self.on_event is not None:
            await self.on_event(action, data)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername") or ("unknown", 0)
        peer = f"{peername[0]}:{peername[1]}"
        self._writers.add(writer)
        await self._emit("server.connection", {"peer": peer, "action": "accepted"})
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=self.idle_timeout)
                except asyncio.TimeoutError:
                    await self._send(writer, self._error("idle_timeout", "no frame within timeout"))
                    break
                if not line:
                    break
                try:
                    body = self.codec.decode(line)
                except ProtocolError as exc:
                    await self._emit(
                        "server.frame_rejected",
                        {"peer": peer, "reason": str(exc)},
                    )
                    await self._send(writer, self._error("bad_frame", str(exc)))
                    break
                response = await self._dispatch(body, peer)
                await self._send(writer, response)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass
            await self._emit("server.connection", {"peer": peer, "action": "closed"})

    async def _send(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        message_type = payload.pop("type")
        writer.write(self.codec.encode(message_type, **payload))
        await writer.drain()

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"type": "error", "code": code, "message": message}

    async def _dispatch(self, body: dict[str, Any], peer: str) -> dict[str, Any]:
        message_type = body.get("type")
        session_id = str(body.get("session_id", ""))
        if message_type == "register":
            return await self._register(body, session_id, peer)
        if not session_id or session_id not in self._sessions:
            return self._error("unknown_session", "register before sending other frames")
        record = self._sessions[session_id]
        record.last_seen = time.time()

        if message_type == "heartbeat":
            record.heartbeats += 1
            await self._emit(
                "server.heartbeat",
                {"session_id": session_id, "seq": body.get("seq"), "beats": record.heartbeats},
            )
            return {
                "type": "heartbeat_ack",
                "session_id": session_id,
                "server_time": time.time(),
                "beats": record.heartbeats,
            }

        if message_type == "poll":
            record.polls += 1
            tasks = self.scenario.next_tasks(session_id)
            record.tasks_sent += len(tasks)
            state = self.scenario.for_session(session_id)
            await self._emit(
                "server.poll",
                {
                    "session_id": session_id,
                    "tasks": [task["task_id"] for task in tasks],
                    "remaining": state["remaining"],
                },
            )
            return {
                "type": "tasks",
                "session_id": session_id,
                "tasks": tasks,
                "remaining": state["remaining"],
                "exhausted": state["remaining"] == 0,
            }

        if message_type == "result":
            entry = {
                "task_id": body.get("task_id"),
                "status": body.get("status"),
                "error": body.get("error"),
                "received_at": time.time(),
            }
            record.results.append(entry)
            await self._emit("server.result", {"session_id": session_id, **entry})
            return {"type": "result_ack", "session_id": session_id, "task_id": body.get("task_id")}

        return self._error("unsupported_type", f"unsupported frame type: {message_type!r}")

    async def _register(self, body: dict[str, Any], session_id: str, peer: str) -> dict[str, Any]:
        if not session_id or len(session_id) > 64:
            return self._error("bad_session", "session_id must be 1..64 characters")
        capabilities = body.get("capabilities", [])
        if not isinstance(capabilities, list):
            return self._error("bad_session", "capabilities must be a list")
        record = SessionRecord(
            session_id=session_id,
            peer=peer,
            capabilities=[str(item) for item in capabilities][:32],
        )
        self._sessions[session_id] = record
        await self._emit(
            "server.registered",
            {
                "session_id": session_id,
                "peer": peer,
                "capabilities": record.capabilities,
                "active_sessions": len(self._sessions),
            },
        )
        return {
            "type": "registered",
            "session_id": session_id,
            "server_time": time.time(),
            "heartbeat_interval": self.heartbeat_interval,
            "policy": {"tasks": len(self.scenario.tasks), "mode": "scripted-lab"},
        }
