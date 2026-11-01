"""Process behaviour simulation.

A fake process table models a loader -> worker -> beacon chain. Nothing is
executed: ``spawn`` allocates a synthetic PID, records parent/child links and
emits events. This is the data a detection engineer would normally get from
process telemetry (Sysmon EventID 1 / auditd ``execve``), produced without
ever calling ``fork`` or ``exec``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .base import SimModule


@dataclass
class SimProcess:
    pid: int
    ppid: int
    name: str
    args: list[str]
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    state: str = "running"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "name": self.name,
            "args": self.args,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "state": self.state,
        }


class SimProcessTable:
    def __init__(self, pid_base: int = 4100) -> None:
        self._next_pid = int(pid_base)
        self._processes: dict[int, SimProcess] = {}

    def spawn(self, name: str, args: list[str], *, ppid: int = 1) -> SimProcess:
        pid = self._next_pid
        self._next_pid += 4
        process = SimProcess(pid=pid, ppid=ppid, name=name, args=list(args))
        self._processes[pid] = process
        return process

    def exit(self, pid: int) -> SimProcess | None:
        process = self._processes.get(pid)
        if process is not None and process.state == "running":
            process.state = "exited"
            process.ended_at = time.time()
        return process

    def get(self, pid: int) -> SimProcess | None:
        return self._processes.get(pid)

    def snapshot(self) -> list[dict[str, Any]]:
        return [process.to_dict() for process in self._processes.values()]

    def running(self) -> list[SimProcess]:
        return [process for process in self._processes.values() if process.state == "running"]


class ProcessModule(SimModule):
    name = "process"
    description = "Synthetic process chain telemetry (no real process creation)"

    async def run(self, ctx) -> None:  # type: ignore[no-untyped-def]
        config = ctx.section("process")
        chain: list[dict[str, Any]] = list(config.get("chain", []))
        table = SimProcessTable(pid_base=int(config.get("pid_base", 4100)))
        ctx.services["process_table"] = table

        await ctx.emit(
            "process",
            "table.initialised",
            self.name,
            data={"pid_base": int(config.get("pid_base", 4100)), "real_execution": False},
        )

        root_pid = 1
        spawned: list[int] = []
        for entry in chain:
            process = table.spawn(
                str(entry.get("name", "aptsim-demo")),
                [str(arg) for arg in entry.get("args", [])],
                ppid=root_pid,
            )
            spawned.append(process.pid)
            root_pid = process.pid
            await ctx.emit(
                "process",
                "spawn.simulated",
                self.name,
                data={
                    "pid": process.pid,
                    "ppid": process.ppid,
                    "image": process.name,
                    "args": process.args,
                    "synthetic": True,
                },
            )
            await ctx.sim_sleep(float(entry.get("ttl", 0.2)) * 0.25)

        if config.get("simulate_injection", True) and spawned:
            target = table.get(spawned[-1])
            await ctx.emit(
                "process",
                "injection.simulated",
                self.name,
                severity="warning",
                data={
                    "pid": target.pid if target else None,
                    "technique": "virtual-alloc/write-thread",
                    "effect": "telemetry-only",
                    "note": "no code is written to any process; event is synthetic",
                },
            )

        await ctx.sim_sleep(0.1)
        snapshot = table.snapshot()
        ctx.write_json("processes.json", {"processes": snapshot, "synthetic": True})

        for pid in reversed(spawned):
            process = table.exit(pid)
            if process is None:
                continue
            await ctx.emit(
                "process",
                "exit.simulated",
                self.name,
                data={"pid": process.pid, "image": process.name, "lifetime_s": round(
                    (process.ended_at or time.time()) - process.started_at, 4
                )},
            )

        await ctx.emit(
            "process",
            "chain.completed",
            self.name,
            data={"count": len(snapshot), "alive": len(table.running())},
        )
