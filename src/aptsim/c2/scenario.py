"""Scripted task source for the simulated C2 server.

The server does not accept tasks from an operator. It replays a fixed scenario
loaded from configuration and hands a bounded number of task descriptors to
each session on poll. This keeps the "C2" side deterministic and inert: even if
someone runs the standalone server, it can only deliver allowlisted lab tasks.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScenarioPlayer:
    tasks: list[dict[str, Any]]
    tasks_per_poll: int = 2
    _cursors: dict[str, int] = field(default_factory=dict)
    delivered: int = 0

    def __post_init__(self) -> None:
        cleaned: list[dict[str, Any]] = []
        for index, task in enumerate(self.tasks):
            task_id = str(task.get("task_id") or f"t-{index + 1:03d}")
            cleaned.append(
                {
                    "task_id": task_id,
                    "type": str(task.get("type", "noop")),
                    "args": copy.deepcopy(dict(task.get("args", {}))),
                }
            )
        self.tasks = cleaned

    def next_tasks(self, session_id: str) -> list[dict[str, Any]]:
        cursor = self._cursors.get(session_id, 0)
        batch = self.tasks[cursor : cursor + max(1, self.tasks_per_poll)]
        self._cursors[session_id] = cursor + len(batch)
        self.delivered += len(batch)
        return copy.deepcopy(batch)

    def remaining(self, session_id: str) -> int:
        return max(0, len(self.tasks) - self._cursors.get(session_id, 0))

    def for_session(self, session_id: str) -> dict[str, Any]:
        return {
            "total": len(self.tasks),
            "delivered": self._cursors.get(session_id, 0),
            "remaining": self.remaining(session_id),
        }
