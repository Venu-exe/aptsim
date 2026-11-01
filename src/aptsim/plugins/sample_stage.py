"""Sample local stage plugin.

Contract
--------
* ``PLUGIN_API`` must equal the loader's supported version.
* ``MODULE`` must be a ``SimModule`` subclass.

The payload module loads this file from ``<lab>/plugins`` at runtime to
demonstrate dynamic module loading from a trusted local directory. The plugin
only emits telemetry and returns a result; it has no host-side effects.
"""

from __future__ import annotations

from typing import Any

from aptsim.modules.base import SimModule

PLUGIN_API = 1


class SampleStage(SimModule):
    name = "sample_stage"
    description = "Example capability stage executed through the plugin loader"

    async def run(self, ctx) -> None:  # type: ignore[no-untyped-def]
        await ctx.emit(
            "execution",
            "plugin.standalone",
            self.name,
            data={"note": "plugin executed as a pipeline module"},
        )

    async def run_stage(self, ctx, spec: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        capabilities = list(spec.get("capabilities", []))
        await ctx.emit(
            "execution",
            "plugin.stage.hook",
            self.name,
            data={
                "stage_id": spec.get("id"),
                "capabilities": capabilities,
                "plugin_api": PLUGIN_API,
                "dynamic_load": True,
            },
        )
        # A real capability module would do work here. This one only simulates
        # a short delay so timing appears in the timeline.
        await ctx.sim_sleep(0.05)
        return {
            "stage": spec.get("id"),
            "status": "simulated",
            "plugin_api": PLUGIN_API,
            "capabilities": capabilities,
        }


MODULE = SampleStage
