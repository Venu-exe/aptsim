"""Multi-stage payload simulation.

The payload is a declarative list of stages. Each stage may reference a local
plugin that is loaded at runtime from ``<lab>/plugins`` and asked to execute a
stage hook. No stage performs host changes; stages emit events, update the
checkpoint and simulate work.

Resume support: the completed stage index is stored in the state namespace so
an interrupted run continues where it stopped instead of replaying stages.
"""

from __future__ import annotations

from typing import Any

from aptsim.core.registry import ModuleRegistry

from .base import SimModule


class PayloadModule(SimModule):
    name = "payload"
    description = "Staged execution flow with dynamic local plugin loading"

    async def run(self, ctx) -> None:  # type: ignore[no-untyped-def]
        config = ctx.section("payload")
        stages: list[dict[str, Any]] = list(config.get("stages", []))
        if not stages:
            raise ValueError("payload.stages is empty")

        namespace = ctx.state.namespace("payload")
        start_index = int(namespace.get("stage_index", 0))
        completed: list[str] = list(namespace.get("completed", []))
        capabilities: list[str] = list(namespace.get("capabilities", []))
        registry = ModuleRegistry()

        await ctx.emit(
            "execution",
            "payload.begin",
            self.name,
            data={
                "stages": [stage.get("id") for stage in stages],
                "resume_from": start_index,
                "mode": "simulation",
            },
        )

        for index, spec in enumerate(stages):
            stage_id = str(spec.get("id"))
            if index < start_index:
                await ctx.emit(
                    "execution",
                    "payload.stage.already_completed",
                    self.name,
                    data={"stage_id": stage_id, "index": index},
                )
                continue

            missing = [req for req in spec.get("requires", []) if req not in completed]
            if missing:
                raise ValueError(f"stage {stage_id} is missing prerequisites: {missing}")

            await ctx.emit(
                "execution",
                "payload.stage.start",
                self.name,
                data={
                    "stage_id": stage_id,
                    "index": index,
                    "name": spec.get("name", stage_id),
                    "capabilities": spec.get("capabilities", []),
                },
            )

            result: dict[str, Any] = {"stage_id": stage_id, "status": "simulated"}
            plugin_name = spec.get("plugin")
            if plugin_name:
                plugin_cls = registry.load_plugin(ctx.paths.plugins / str(plugin_name), ctx.paths)
                plugin = plugin_cls()
                await ctx.emit(
                    "execution",
                    "payload.plugin.loaded",
                    self.name,
                    data={
                        "plugin": plugin_name,
                        "sha256": plugin_cls.plugin_sha256,
                        "path": plugin_cls.plugin_path,
                        "trust": "lab-local",
                    },
                )
                result = await plugin.run_stage(ctx, spec)

            await ctx.sim_sleep(float(spec.get("delay", config.get("stage_delay", 0.1))))
            completed.append(stage_id)
            for capability in spec.get("capabilities", []):
                if capability not in capabilities:
                    capabilities.append(capability)

            namespace["stage_index"] = index + 1
            namespace["completed"] = completed
            namespace["capabilities"] = capabilities
            ctx.state.save()

            await ctx.emit(
                "execution",
                "payload.stage.completed",
                self.name,
                data={
                    "stage_id": stage_id,
                    "index": index,
                    "result": result.get("status", "simulated"),
                },
            )

        await ctx.emit(
            "execution",
            "payload.ready",
            self.name,
            data={
                "stages_completed": len(completed),
                "capabilities": capabilities,
                "inert": True,
            },
        )
