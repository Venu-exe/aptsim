"""Session management: identity, layout, heartbeat, metadata and summary."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import LabPaths


def new_session_id(prefix: str = "s") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{secrets.token_hex(3)}"


@dataclass
class Session:
    session_id: str
    directory: Path
    started_at: float = field(default_factory=time.time)
    status: str = "created"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def events_path(self) -> Path:
        return self.directory / "events.jsonl"

    @property
    def state_path(self) -> Path:
        return self.directory / "state.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.directory / "heartbeat.json"

    @property
    def summary_path(self) -> Path:
        return self.directory / "summary.json"

    @property
    def artifacts_path(self) -> Path:
        return self.directory / "artifacts.jsonl"

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.directory / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "directory": str(self.directory),
            "started_at": self.started_at,
            "started_at_iso": datetime.fromtimestamp(self.started_at, timezone.utc).isoformat(),
            "status": self.status,
            "pid": os.getpid(),
            "metadata": self.metadata,
        }

    def save(self) -> None:
        self.write_json("session.json", self.to_dict())


class SessionManager:
    def __init__(self, paths: LabPaths) -> None:
        self.paths = paths

    def create(self, *, metadata: dict[str, Any] | None = None) -> Session:
        session_id = new_session_id()
        directory = self.paths.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=False)
        session = Session(session_id=session_id, directory=directory, metadata=dict(metadata or {}))
        session.save()
        return session

    def open(self, session_id: str) -> Session:
        directory = self.paths.session_dir(session_id)
        meta_path = directory / "session.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"unknown session: {session_id}")
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        return Session(
            session_id=session_id,
            directory=directory,
            started_at=float(payload.get("started_at", time.time())),
            status=str(payload.get("status", "unknown")),
            metadata=dict(payload.get("metadata", {})),
        )

    def latest(self) -> Session | None:
        if not self.paths.sessions.exists():
            return None
        candidates = [p for p in self.paths.sessions.iterdir() if (p / "session.json").exists()]
        if not candidates:
            return None
        newest = max(candidates, key=lambda p: (p / "session.json").stat().st_mtime)
        return self.open(newest.name)


class Heartbeat:
    """Background heartbeat task; proves liveness and drives session tracking."""

    def __init__(self, session: Session, interval: float = 1.0) -> None:
        self.session = session
        self.interval = max(0.01, interval)
        self._task: asyncio.Task[None] | None = None
        self.beats = 0

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="session-heartbeat")

    async def _run(self) -> None:
        while True:
            self.beats += 1
            self.session.write_json(
                "heartbeat.json",
                {
                    "session_id": self.session.session_id,
                    "beat": self.beats,
                    "ts": time.time(),
                    "pid": os.getpid(),
                },
            )
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
