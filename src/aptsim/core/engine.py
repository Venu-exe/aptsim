"""Central execution engine.

Responsibilities
----------------
* parse the pipeline into ordered stages
* construct modules through the registry (built-ins or local plugins)
* enforce per-stage timeouts and retry policies
* checkpoint state after every stage so ``resume`` can continue
* emit lifecycle events and write a machine readable summary

The engine is deliberately fail-soft: a stage declared with ``on_error:
continue`` records its failure and lets the pipeline proceed, while ``abort``
stops the run and leaves a resumable checkpoint behind.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .context import ExecutionContext
from .errors import ModuleError, SimulatedCrash
from .registry import ModuleRegistry
from .state import RunStatus


@dataclass
class Stage:
    id: str
    module: str
    enabled: bool = True
    on_error: str = "abort"
    retries: int = 0
    timeout: float | None = None
    options: dict[str, Any] = field(default_factory=dict)


def parse_stages(config: Config) -> list[Stage]:
    stages: list[Stage] = []
    for raw in config.require("pipeline.stages"):
        stages.append(
            Stage(
                id=str(raw["id"]),
                module=str(raw["module"]),
                enabled=bool(raw.get("enabled", True)),
                on_error=str(raw.get("on_error", "abort")),
                retries=int(raw.get("retries", 0)),
                timeout=raw.get("timeout"),
                options=dict(raw.get("options", {})),
            )
        )
    return stages


class Engine:
    def __init__(self, ctx: ExecutionContext, registry: ModuleRegistry | None = None) -> None:
        self.ctx = ctx
        self.registry = registry or ModuleRegistry.default()
        self.stages = parse_stages(ctx.config)
        self.results: list[dict[str, Any]] = []
        self.stop_after = ctx.config.get("pipeline.stop_after")

    async def run(self, resume: bool = False) -> dict[str, Any]:
        ctx = self.ctx
        state = ctx.state
        started = time.time()
        first_pending = self._first_pending(resume)

        if resume:
            state.state.restarts += 1
            state.state.status = RunStatus.RUNNING
            state.save()
            await ctx.emit(
                "lifecycle",
                "run.resume",
                "engine",
                data={
                    "session_id": ctx.session.session_id,
                    "completed": list(state.state.completed),
                    "restarts": state.state.restarts,
                },
            )
        else:
            state.state.status = RunStatus.RUNNING
            state.state.completed.clear()
            state.save()
            await ctx.emit(
                "lifecycle",
                "run.start",
                "engine",
                data={
                    "session_id": ctx.session.session_id,
                    "stages": [stage.id for stage in self.stages],
                    "config_digest": ctx.config.digest()[:16],
                    "mode": "simulation",
                },
            )

        try:
            for index, stage in enumerate(self.stages):
                if not stage.enabled:
                    await ctx.emit(
                        "lifecycle", "stage.skipped", "engine", data={"stage_id": stage.id, "reason": "disabled"}
                    )
                    continue
                if resume and index < first_pending:
                    await ctx.emit(
                        "lifecycle", "stage.already_completed", "engine", data={"stage_id": stage.id}
                    )
                    continue

                await self._run_stage(stage, index)

                if self.stop_after and stage.id == self.stop_after:
                    reason = f"simulated crash requested after stage {stage.id}"
                    state.mark_interrupted(stage.id, reason)
                    await ctx.emit(
                        "lifecycle",
                        "run.crash_simulated",
                        "engine",
                        severity="warning",
                        data={"stage_id": stage.id, "reason": reason},
                    )
                    raise SimulatedCrash(reason)

            state.mark_completed_run()
            summary = self._summary(started, status="completed")
            ctx.write_json("summary.json", summary)
            await ctx.emit(
                "lifecycle",
                "run.completed",
                "engine",
                data={
                    "duration_s": summary["duration_s"],
                    "stages": len(summary["stages"]),
                    "artifacts": summary["artifacts"],
                },
            )
            return summary

        except SimulatedCrash:
            ctx.write_json("summary.json", self._summary(started, status="interrupted"))
            raise
        except asyncio.CancelledError:
            state.mark_interrupted(state.state.cursor.get("stage_id", "unknown"), "cancelled")
            await ctx.emit(
                "lifecycle", "run.cancelled", "engine", severity="warning", data={"reason": "signal"}
            )
            raise
        except Exception as exc:
            stage_id = state.state.cursor.get("stage_id", "unknown")
            state.mark_failed(stage_id, str(exc))
            ctx.write_json("summary.json", self._summary(started, status="failed", error=str(exc)))
            await ctx.emit(
                "lifecycle",
                "run.failed",
                "engine",
                severity="error",
                data={"stage_id": stage_id, "error": str(exc)},
            )
            raise

    def _first_pending(self, resume: bool) -> int:
        if not resume:
            return 0
        for index, stage in enumerate(self.stages):
            if stage.id not in self.ctx.state.state.completed:
                return index
        return len(self.stages)

    async def _run_stage(self, stage: Stage, index: int) -> None:
        ctx = self.ctx
        state = ctx.state
        timeout = float(stage.timeout or ctx.config.get("runtime.stage_timeout", 60.0))
        attempts = 1 + max(0, stage.retries)
        module_cls = self.registry.get(stage.module, paths=ctx.paths)

        state.mark_running(stage.id, index)
        await ctx.emit(
            "lifecycle",
            "stage.start",
            "engine",
            data={"stage_id": stage.id, "module": stage.module, "index": index, "timeout_s": timeout},
        )
        started = time.perf_counter()

        for attempt in range(1, attempts + 1):
            module = module_cls(stage.options)
            try:
                await module.setup(ctx)
                await asyncio.wait_for(module.run(ctx), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._safe_teardown(module)
                if attempt < attempts:
                    backoff = float(ctx.config.get("runtime.retry.backoff", 0.1)) * attempt
                    await ctx.emit(
                        "lifecycle",
                        "stage.retry",
                        "engine",
                        severity="warning",
                        data={
                            "stage_id": stage.id,
                            "attempt": attempt,
                            "error": f"{type(exc).__name__}: {exc}",
                            "backoff_s": backoff,
                        },
                    )
                    await ctx.sim_sleep(backoff)
                    continue

                duration = time.perf_counter() - started
                await ctx.emit(
                    "lifecycle",
                    "stage.failed",
                    "engine",
                    severity="error",
                    data={
                        "stage_id": stage.id,
                        "module": stage.module,
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_s": round(duration, 4),
                    },
                )
                state.namespace("failures")[stage.id] = str(exc)
                if stage.on_error == "continue":
                    state.mark_completed(stage.id)
                    self.results.append(
                        {
                            "id": stage.id,
                            "module": stage.module,
                            "status": "failed",
                            "error": str(exc),
                            "duration_s": round(duration, 4),
                        }
                    )
                    return
                raise ModuleError(f"stage {stage.id} failed: {exc}") from exc
            else:
                await self._safe_teardown(module)
                duration = time.perf_counter() - started
                state.mark_completed(stage.id)
                self.results.append(
                    {
                        "id": stage.id,
                        "module": stage.module,
                        "status": "ok",
                        "duration_s": round(duration, 4),
                    }
                )
                await ctx.emit(
                    "lifecycle",
                    "stage.completed",
                    "engine",
                    data={
                        "stage_id": stage.id,
                        "module": stage.module,
                        "duration_s": round(duration, 4),
                    },
                )
                return

    async def _safe_teardown(self, module: Any) -> None:
        try:
            await module.teardown(self.ctx)
        except Exception as exc:  # pragma: no cover - teardown must not mask failures
            await self.ctx.emit(
                "lifecycle",
                "stage.teardown_error",
                "engine",
                severity="warning",
                data={"module": module.name, "error": str(exc)},
            )

    def _summary(self, started: float, *, status: str, error: str | None = None) -> dict[str, Any]:
        ctx = self.ctx
        try:
            artifacts = len(ctx.journal.load())
        except (OSError, ValueError):  # pragma: no cover
            artifacts = -1
        summary: dict[str, Any] = {
            "session_id": ctx.session.session_id,
            "status": status,
            "error": error,
            "started_at": started,
            "finished_at": time.time(),
            "duration_s": round(time.time() - started, 4),
            "stages": self.results,
            "events": len(ctx.events.replay()),
            "artifacts": artifacts,
            "config_digest": ctx.config.digest(),
            "mode": "simulation-only",
        }
        return summary
