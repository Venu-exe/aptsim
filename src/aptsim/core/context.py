"""Shared execution context passed to every simulation module."""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import ArtifactJournal
from .config import Config
from .events import EventBus
from .paths import LabPaths
from .session import Session
from .state import StateStore


@dataclass
class ExecutionContext:
    config: Config
    paths: LabPaths
    events: EventBus
    session: Session
    state: StateStore
    journal: ArtifactJournal
    seed: int = 0
    services: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    # -- config helpers ----------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.config.section(name)

    def get(self, dotted: str, default: Any = None) -> Any:
        return self.config.get(dotted, default)

    # -- event helpers -----------------------------------------------------
    async def emit(
        self,
        category: str,
        action: str,
        module: str,
        *,
        severity: str = "info",
        data: dict[str, Any] | None = None,
        correlation: str | None = None,
    ) -> None:
        await self.events.emit(
            category, action, module, severity=severity, data=data, correlation=correlation
        )

    # -- timing ------------------------------------------------------------
    async def sim_sleep(self, seconds: float) -> None:
        """Sleep scaled by ``runtime.time_scale``; zero disables waiting."""
        scaled = max(0.0, float(seconds)) * float(self.get("runtime.time_scale", 1.0))
        if scaled:
            await asyncio.sleep(scaled)
        else:
            await asyncio.sleep(0)

    # -- session output ----------------------------------------------------
    def write_json(self, name: str, payload: Any) -> Path:
        return self.session.write_json(name, payload)

    def read_json(self, name: str) -> Any:
        path = self.session.directory / name
        return json.loads(path.read_text(encoding="utf-8"))

    def exists(self, name: str) -> bool:
        return (self.session.directory / name).exists()

    def relative_to_lab(self, path: str | Path) -> str:
        return str(self.paths.guard(path).relative_to(self.paths.root))
