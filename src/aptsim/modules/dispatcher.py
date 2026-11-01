"""Modular command dispatcher.

Remote tasks are validated against a closed allowlist of task types and a
closed set of readable lab roots. Handlers are read-only and operate exclusively
on lab files. There is no shell, no ``eval``/``exec``, no subprocess and no
writable handler by design -- the dispatcher exists to demonstrate request
validation, result correlation, timeouts and error handling.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aptsim.core.artifacts import sha256_file
from aptsim.core.errors import CommandRejected, PathViolation
from aptsim.core.paths import LabPaths

from .fileops import iter_files

Handler = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class TaskResult:
    task_id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 3),
        }


@dataclass
class Dispatcher:
    ctx: Any
    allowed_types: list[str]
    allowed_roots: list[str]
    timeout: float = 10.0
    max_concurrent: int = 3
    max_tasks: int = 100
    handled: int = 0
    rejected: int = 0
    errors: int = 0
    _handlers: dict[str, Handler] = field(default_factory=dict)
    _semaphore: asyncio.Semaphore | None = None

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(max(1, self.max_concurrent))
        self._handlers = {
            "noop": self._noop,
            "report_status": self._report_status,
            "list_files": self._list_files,
            "hash_files": self._hash_files,
            "sleep": self._sleep,
        }

    @classmethod
    def from_config(cls, ctx: Any) -> "Dispatcher":
        config = ctx.section("dispatcher")
        return cls(
            ctx=ctx,
            allowed_types=[str(item) for item in config.get("allowed_task_types", [])],
            allowed_roots=[str(item) for item in config.get("allowed_roots", ["victim"])],
            timeout=float(config.get("task_timeout", 10.0)),
            max_concurrent=int(config.get("max_concurrent", 3)),
            max_tasks=int(config.get("max_tasks", 100)),
        )

    async def handle(self, task: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        if not isinstance(task, dict):
            self.rejected += 1
            return TaskResult(
                "?",
                "rejected",
                error="task must be an object",
                duration_ms=(time.perf_counter() - started) * 1000,
            ).to_dict()
        task_id = str(task.get("task_id", "?"))
        try:
            task_type, args = self._validate(task)
            handler = self._handlers[task_type]
            async with self._semaphore:  # type: ignore[union-attr]
                result = await asyncio.wait_for(handler(self.ctx, args), timeout=self.timeout)
            self.handled += 1
            return TaskResult(task_id, "ok", result=result, duration_ms=(time.perf_counter() - started) * 1000).to_dict()
        except CommandRejected as exc:
            self.rejected += 1
            return TaskResult(task_id, "rejected", error=str(exc), duration_ms=(time.perf_counter() - started) * 1000).to_dict()
        except asyncio.TimeoutError:
            self.errors += 1
            return TaskResult(task_id, "timeout", error=f"task exceeded {self.timeout}s", duration_ms=(time.perf_counter() - started) * 1000).to_dict()
        except Exception as exc:
            self.errors += 1
            return TaskResult(task_id, "error", error=f"{type(exc).__name__}: {exc}", duration_ms=(time.perf_counter() - started) * 1000).to_dict()

    # -- validation --------------------------------------------------------
    def _validate(self, task: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if not isinstance(task, dict):
            raise CommandRejected("task must be an object")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 64:
            raise CommandRejected("task_id must be a non-empty string <= 64 chars")
        task_type = task.get("type")
        if task_type not in self.allowed_types:
            raise CommandRejected(f"task type {task_type!r} is not in the allowlist")
        if task_type not in self._handlers:
            raise CommandRejected(f"task type {task_type!r} has no registered handler")
        args = task.get("args", {})
        if not isinstance(args, dict):
            raise CommandRejected("args must be an object")
        if task_type in {"list_files", "hash_files"}:
            self._resolve_lab_path(str(args.get("path", "victim")))
            if "limit" in args:
                limit = args["limit"]
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
                    raise CommandRejected("limit must be an integer between 1 and 50")
        if task_type == "sleep":
            seconds = args.get("seconds", 0)
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= float(seconds) <= 2.0:
                raise CommandRejected("sleep seconds must be between 0 and 2")
        return str(task_type), args

    def _resolve_lab_path(self, relative: str) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise CommandRejected(f"path not allowed: {relative!r}")
        try:
            resolved = self.ctx.paths.resolve_relative(relative, what="task path")
        except PathViolation as exc:
            raise CommandRejected(str(exc)) from exc
        for root in self.allowed_roots:
            allowed = self.ctx.paths.resolve_relative(root, what="allowed root")
            if resolved == allowed or resolved.is_relative_to(allowed):
                return resolved
        raise CommandRejected(f"path outside allowed roots: {relative!r}")

    # -- handlers (read-only) ---------------------------------------------
    async def _noop(self, ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        return {"acknowledged": True, "inert": True}

    async def _report_status(self, ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": ctx.session.session_id,
            "status": ctx.state.state.status.value,
            "completed_stages": list(ctx.state.state.completed),
            "restarts": ctx.state.state.restarts,
            "mode": "simulation-only",
        }

    async def _list_files(self, ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_lab_path(str(args.get("path", "victim")))
        entries: list[dict[str, Any]] = []
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_symlink():
                    kind = "symlink"
                elif child.is_dir():
                    kind = "dir"
                elif child.is_file():
                    kind = "file"
                else:
                    kind = "other"
                entries.append(
                    {
                        "name": child.name,
                        "type": kind,
                        "size": child.stat().st_size if kind == "file" else None,
                    }
                )
                if len(entries) >= 200:
                    break
        return {
            "path": str(root.relative_to(ctx.paths.root)),
            "entries": entries,
            "count": len(entries),
        }

    async def _hash_files(self, ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_lab_path(str(args.get("path", "victim")))
        limit = int(args.get("limit", 10))
        files = iter_files(root, max_files=limit)
        results = []
        for path in files:
            results.append(
                {
                    "path": str(path.relative_to(ctx.paths.root)),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        return {"path": str(root.relative_to(ctx.paths.root)), "files": results, "count": len(results)}

    async def _sleep(self, ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        await ctx.sim_sleep(float(args.get("seconds", 0)))
        return {"slept": float(args.get("seconds", 0)), "real_process_created": False}
