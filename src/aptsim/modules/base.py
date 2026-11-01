"""Simulation module contract."""

from __future__ import annotations

from abc import ABC
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from aptsim.core.context import ExecutionContext


class SimModule(ABC):
    """Base class for every simulation module.

    Modules are intentionally inert: they read and write only inside the lab
    tree, emit structured events, and never touch host state.
    """

    name: str = "unnamed"
    description: str = ""
    plugin_sha256: str | None = None
    plugin_path: str | None = None

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def option(self, ctx: "ExecutionContext", key: str, default: Any = None) -> Any:
        """Stage-level option wins over the module's global config section."""
        if key in self.options:
            return self.options[key]
        return ctx.config.get(f"{self.name}.{key}", default)

    async def setup(self, ctx: "ExecutionContext") -> None:
        """Prepare resources. Must not mutate host state."""

    async def run(self, ctx: "ExecutionContext") -> None:
        """Perform the simulated behaviour."""

    async def teardown(self, ctx: "ExecutionContext") -> None:
        """Release resources; always called after run, including on failure."""

    async def run_stage(self, ctx: "ExecutionContext", spec: dict[str, Any]) -> dict[str, Any]:
        """Optional hook used by the payload loader for dynamic stage modules."""
        await ctx.emit(
            "execution",
            "plugin.stage.noop",
            self.name,
            data={"stage": spec.get("id")},
        )
        return {"stage": spec.get("id"), "status": "noop"}
