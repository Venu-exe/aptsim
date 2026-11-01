"""Checkpointed execution state.

The state file is the durable contract between a process that was interrupted
and the ``resume`` path. It records completed stages, the pipeline cursor and
per-module cursors (for example the payload stage index).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ExecutionState:
    session_id: str
    status: RunStatus = RunStatus.PENDING
    cursor: dict[str, Any] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)
    failed_stage: str | None = None
    error: str | None = None
    namespaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    restarts: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "session_id": self.session_id,
            "status": self.status.value,
            "cursor": self.cursor,
            "completed": list(self.completed),
            "failed_stage": self.failed_stage,
            "error": self.error,
            "restarts": self.restarts,
            "updated_at": self.updated_at,
        }
        data.update(self.namespaces)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionState":
        reserved = {
            "session_id",
            "status",
            "cursor",
            "completed",
            "failed_stage",
            "error",
            "restarts",
            "updated_at",
        }
        namespaces = {key: value for key, value in data.items() if key not in reserved}
        return cls(
            session_id=str(data["session_id"]),
            status=RunStatus(data.get("status", "pending")),
            cursor=dict(data.get("cursor", {})),
            completed=list(data.get("completed", [])),
            failed_stage=data.get("failed_stage"),
            error=data.get("error"),
            namespaces=namespaces,
            restarts=int(data.get("restarts", 0)),
            updated_at=float(data.get("updated_at", time.time())),
        )


class StateStore:
    """Atomic JSON checkpoint store."""

    def __init__(self, path: str | Path, state: ExecutionState | None = None) -> None:
        self.path = Path(path)
        self.state = state or ExecutionState(session_id=self.path.parent.name)
        self._lock = threading.Lock()

    @classmethod
    def load(cls, path: str | Path) -> "StateStore":
        target = Path(path)
        data = json.loads(target.read_text(encoding="utf-8"))
        return cls(target, ExecutionState.from_dict(data))

    def save(self) -> None:
        with self._lock:
            self.state.updated_at = time.time()
            payload = json.dumps(self.state.to_dict(), indent=2, sort_keys=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)

    # -- convenience -------------------------------------------------------
    def mark_running(self, stage_id: str, index: int) -> None:
        self.state.status = RunStatus.RUNNING
        self.state.cursor = {"stage_id": stage_id, "stage_index": index}
        self.state.error = None
        self.save()

    def mark_completed(self, stage_id: str) -> None:
        if stage_id not in self.state.completed:
            self.state.completed.append(stage_id)
        self.state.cursor = {}
        self.save()

    def mark_interrupted(self, stage_id: str, reason: str) -> None:
        self.state.status = RunStatus.INTERRUPTED
        self.state.failed_stage = stage_id
        self.state.error = reason
        self.state.restarts += 1
        self.save()

    def mark_failed(self, stage_id: str, reason: str) -> None:
        self.state.status = RunStatus.FAILED
        self.state.failed_stage = stage_id
        self.state.error = reason
        self.save()

    def mark_completed_run(self) -> None:
        self.state.status = RunStatus.COMPLETED
        self.state.failed_stage = None
        self.state.error = None
        self.save()

    def is_completed(self, stage_id: str) -> bool:
        return stage_id in self.state.completed

    def namespace(self, name: str) -> dict[str, Any]:
        return self.state.namespaces.setdefault(name, {})
