"""Network module: loopback C2 session, heartbeats and scripted task handling.

Flow
----
1. start a loopback server (ephemeral port by default)
2. register a session with the capabilities negotiated by the payload module
3. run heartbeats concurrently with polling
4. validate and execute each task through the inert dispatcher
5. post results, stop cleanly and write a session report

No external address is reachable: the host is validated as loopback in config
validation, in the server constructor and here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aptsim.c2.client import C2Client
from aptsim.c2.server import C2Server
from aptsim.modules.dispatcher import Dispatcher

from .base import SimModule


async def drive_heartbeats(ctx, client: C2Client, count: int, interval: float) -> None:  # type: ignore[no-untyped-def]
    for seq in range(1, max(1, count) + 1):
        await ctx.sim_sleep(interval)
        response = await client.heartbeat(seq)
        await ctx.emit(
            "network",
            "heartbeat.sent",
            "network",
            data={"seq": seq, "beats_server": response.get("beats"), "ack": True},
        )


async def drive_polls(ctx, client: C2Client, dispatcher: Dispatcher, max_polls: int) -> int:  # type: ignore[no-untyped-def]
    started_tasks = 0
    for poll_index in range(1, max(1, max_polls) + 1):
        response = await client.poll()
        tasks = list(response.get("tasks", []))
        if not tasks:
            await ctx.emit(
                "network",
                "poll.empty",
                "network",
                severity="debug",
                data={"poll": poll_index, "exhausted": bool(response.get("exhausted"))},
            )
            if response.get("exhausted"):
                break
            await ctx.sim_sleep(0.02)
            continue

        await ctx.emit(
            "network",
            "poll.tasks_received",
            "network",
            data={
                "poll": poll_index,
                "task_ids": [task.get("task_id") for task in tasks],
                "remaining": response.get("remaining"),
            },
        )
        for task in tasks:
            if started_tasks >= dispatcher.max_tasks:
                await ctx.emit(
                    "network",
                    "task.budget_exhausted",
                    "network",
                    severity="warning",
                    data={"max_tasks": dispatcher.max_tasks},
                )
                return started_tasks
            started_tasks += 1
            result = await dispatcher.handle(task)
            await client.send_result(
                result["task_id"],
                result["status"],
                result.get("result"),
                result.get("error"),
            )
            await ctx.emit(
                "command",
                "task.completed",
                "network",
                severity="info" if result["status"] == "ok" else "warning",
                data={
                    "task_id": result["task_id"],
                    "type": task.get("type"),
                    "status": result["status"],
                    "duration_ms": result.get("duration_ms"),
                    "error": result.get("error"),
                    "error_handled": result["status"] != "ok",
                },
            )
    return started_tasks


async def play_session(
    ctx,  # type: ignore[no-untyped-def]
    client: C2Client,
    dispatcher: Dispatcher,
    *,
    max_polls: int,
    heartbeat_count: int,
    heartbeat_interval: float,
) -> int:
    """Run heartbeats concurrently with task polling on one connection."""
    heartbeat_task = asyncio.create_task(
        drive_heartbeats(ctx, client, heartbeat_count, heartbeat_interval),
        name="c2-heartbeats",
    )
    try:
        processed = await drive_polls(ctx, client, dispatcher, max_polls)
    finally:
        try:
            await asyncio.wait_for(
                heartbeat_task, timeout=heartbeat_interval * (heartbeat_count + 2) + 1
            )
        except asyncio.TimeoutError:  # pragma: no cover - safety valve
            heartbeat_task.cancel()
    return processed


class NetworkModule(SimModule):
    name = "network"
    description = "Loopback-only C2 simulation with heartbeat, polling and task results"

    async def run(self, ctx) -> None:  # type: ignore[no-untyped-def]
        config = ctx.section("network")
        host = str(config.get("host", "127.0.0.1"))
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError(f"refusing non-loopback C2 host: {host}")
        port = int(config.get("port", 0))
        secret = str(config.get("auth", {}).get("shared_secret", "lab-only-shared-secret"))
        tasks = list(config.get("scenario", []))
        heartbeat_interval = float(ctx.get("runtime.heartbeat_interval", 1.0))
        heartbeat_count = int(config.get("heartbeats", 3))
        max_polls = int(config.get("max_polls", 12))

        dispatcher = Dispatcher.from_config(ctx)
        ctx.services["dispatcher"] = dispatcher

        async def on_server_event(action: str, data: dict[str, Any]) -> None:
            await ctx.emit("network", action, "c2.server", data=data)

        server = C2Server(
            host=host,
            port=port,
            secret=secret,
            tasks=tasks,
            tasks_per_poll=int(config.get("tasks_per_poll", 2)),
            on_event=on_server_event,
            heartbeat_interval=heartbeat_interval,
        )
        bound_port = await server.start()
        ctx.services["c2_server"] = server
        ctx.write_json(
            "c2-server.json",
            {
                "host": host,
                "port": bound_port,
                "transport": config.get("transport", "tcp-jsonl"),
                "loopback": True,
                "scenario_tasks": len(tasks),
                "auth": config.get("auth", {}).get("scheme", "hmac-sha256"),
            },
        )

        capabilities = list(ctx.state.namespace("payload").get("capabilities", []))
        client = C2Client(
            host=host,
            port=bound_port,
            secret=secret,
            session_id=ctx.session.session_id,
            timeout=15.0,
        )
        await client.connect()
        try:
            registered = await client.register(capabilities)
            await ctx.emit(
                "network",
                "session.registered",
                self.name,
                data={
                    "session_id": ctx.session.session_id,
                    "server_session_id": registered.get("session_id"),
                    "capabilities": capabilities,
                    "heartbeat_interval_s": registered.get("heartbeat_interval"),
                    "active_sessions": len(server.active_sessions()),
                },
            )

            processed = await play_session(
                ctx,
                client,
                dispatcher,
                max_polls=max_polls,
                heartbeat_count=heartbeat_count,
                heartbeat_interval=heartbeat_interval,
            )
            ctx.services["tasks_processed"] = processed
        finally:
            await client.close()
            await server.stop()

        report = {
            "session_id": ctx.session.session_id,
            "port": bound_port,
            "request_count": client.request_count,
            "dispatcher": {
                "handled": dispatcher.handled,
                "rejected": dispatcher.rejected,
                "errors": dispatcher.errors,
            },
            "server_sessions": server.active_sessions(),
            "loopback_only": True,
        }
        ctx.write_json("c2-session.json", report)
        await ctx.emit(
            "network",
            "session.closed",
            self.name,
            data={
                "request_count": client.request_count,
                "handled": dispatcher.handled,
                "rejected": dispatcher.rejected,
                "errors": dispatcher.errors,
                "server_sessions": len(server.active_sessions()),
            },
        )
