"""Shared fixtures for unit and integration tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aptsim.core.artifacts import ArtifactJournal  # noqa: E402
from aptsim.core.config import Config, ConfigManager  # noqa: E402
from aptsim.core.context import ExecutionContext  # noqa: E402
from aptsim.core.events import EventBus, MemorySink  # noqa: E402
from aptsim.core.paths import LabPaths  # noqa: E402
from aptsim.core.session import SessionManager  # noqa: E402
from aptsim.core.state import ExecutionState, StateStore  # noqa: E402
from aptsim.tools.labgen import populate  # noqa: E402

CONFIG_DIR = REPO / "config"


def make_config(tmp_path: Path, profile: str = "ci", overrides: list[str] | None = None) -> Config:
    lab = tmp_path / "lab"
    base = [f"lab.root={json.dumps(str(lab))}"]
    return ConfigManager(CONFIG_DIR).load(profile, base + list(overrides or []))


def make_paths(config: Config) -> LabPaths:
    return LabPaths.build(config.require("lab.root"))


def make_lab(tmp_path: Path, profile: str = "ci") -> tuple[Config, LabPaths]:
    config = make_config(tmp_path, profile)
    paths = make_paths(config)
    populate(paths)
    return config, paths


async def make_context(config: Config, paths: LabPaths, *, sink: MemorySink | None = None):
    manager = SessionManager(paths)
    session = manager.create(metadata={"test": True})
    state = StateStore(session.state_path, ExecutionState(session.session_id))
    state.save()
    memory = sink or MemorySink()
    bus = EventBus(session.session_id, [memory])
    await bus.start()
    journal = ArtifactJournal(session.artifacts_path, paths, session.session_id)
    ctx = ExecutionContext(
        config=config,
        paths=paths,
        events=bus,
        session=session,
        state=state,
        journal=journal,
        seed=int(config.get("runtime.seed", 0)),
    )
    return ctx, bus, memory
