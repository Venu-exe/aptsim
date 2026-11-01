"""Command line interface for the aptsim lab framework."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .c2.client import C2Client
from .c2.server import C2Server
from .core.artifacts import ArtifactJournal
from .core.config import Config, ConfigManager
from .core.context import ExecutionContext
from .core.engine import Engine
from .core.errors import AptSimError, SimulatedCrash
from .core.events import EventBus, JSONLFileSink, PrettySink
from .core.paths import LabPaths
from .core.registry import ModuleRegistry
from .core.session import Heartbeat, Session, SessionManager
from .core.state import ExecutionState, StateStore
from .core.timeline import load_events, render, write_timeline
from .modules.dispatcher import Dispatcher
from .modules.network import play_session
from .recovery.cleanup import CleanupEngine
from .tools.labgen import populate

DEFAULT_PROFILE = "lab-full"


# ---------------------------------------------------------------------------
# configuration and context helpers
# ---------------------------------------------------------------------------
def config_dir() -> Path:
    candidates: list[Path] = []
    if os.environ.get("APTSIM_CONFIG_DIR"):
        candidates.append(Path(os.environ["APTSIM_CONFIG_DIR"]))
    package_root = Path(__file__).resolve().parents[2]
    candidates.extend([package_root / "config", Path.cwd() / "config"])
    for candidate in candidates:
        if (candidate / "default.json").exists():
            return candidate.resolve()
    return candidates[0]


def load_config(args: argparse.Namespace) -> Config:
    manager = ConfigManager(config_dir())
    overrides: list[str] = list(getattr(args, "set", []) or [])
    if getattr(args, "seed", None) is not None:
        overrides.append(f"runtime.seed={int(args.seed)}")
    if getattr(args, "stop_after", None):
        overrides.append(f"pipeline.stop_after={json.dumps(args.stop_after)}")
    profile = getattr(args, "profile", None) or DEFAULT_PROFILE

    extra: dict[str, Any] = {}
    if getattr(args, "lab", None):
        extra["lab"] = {"root": str(Path(args.lab).expanduser().resolve())}
    elif os.environ.get("APTSIM_LAB"):
        extra["lab"] = {"root": str(Path(os.environ["APTSIM_LAB"]).expanduser().resolve())}

    config = manager.load(profile, overrides, extra=extra)
    return config


def lab_root(config: Config) -> Path:
    raw = str(config.require("lab.root"))
    return Path(raw).expanduser().resolve()


def build_paths(config: Config) -> LabPaths:
    layout = config.section("lab")
    return LabPaths.build(
        lab_root(config),
        victim_subdir=str(layout.get("victim_subdir", "victim")),
        backup_subdir=str(layout.get("backup_subdir", ".backup")),
        sessions_subdir=str(layout.get("sessions_subdir", "sessions")),
        plugins_subdir=str(layout.get("plugins_subdir", "plugins")),
        persistence_subdir=str(layout.get("persistence_subdir", "persistence")),
        c2_subdir=str(layout.get("c2_subdir", "c2")),
    )


def make_context(
    config: Config,
    paths: LabPaths,
    session: Session,
    *,
    resume: bool = False,
    quiet: bool = False,
    verbose: bool = False,
) -> tuple[ExecutionContext, EventBus]:
    state = (
        StateStore.load(session.state_path)
        if resume
        else StateStore(session.state_path, ExecutionState(session.session_id))
    )
    if not resume:
        state.save()
    sinks: list[Any] = [JSONLFileSink(session.events_path)]
    if not quiet:
        sinks.append(PrettySink(verbose=verbose))
    bus = EventBus(session.session_id, sinks)
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
    session.write_json("config.snapshot.json", json.loads(config.snapshot()))
    return ctx, bus


def open_session(manager: SessionManager, session_id: str | None) -> Session:
    if session_id:
        return manager.open(session_id)
    latest = manager.latest()
    if latest is None:
        raise AptSimError("no sessions found; run 'aptsim run' first or pass --session")
    return latest


def ensure_lab(paths: LabPaths, *, seed: int) -> None:
    paths.ensure_layout()
    if not paths.victim.exists() or not any(paths.victim.iterdir()):
        populate(paths, seed=seed)


async def run_cleanup(ctx: ExecutionContext, *, dry_run: bool = False) -> dict[str, Any]:
    collected: list[tuple[str, str, dict[str, Any]]] = []
    engine = CleanupEngine(
        ctx.paths,
        ctx.journal,
        ctx.session.directory,
        dry_run=dry_run,
        on_event=lambda category, action, data: collected.append((category, action, data)),
    )
    report = engine.run()
    for category, action, data in collected:
        await ctx.emit(category, action, "cleanup", data=data)
    return report


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
async def command_init(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    seed = int(config.get("runtime.seed", 0))
    result = populate(paths, seed=seed, overwrite=bool(args.force))
    print(f"lab root : {result['lab_root']}")
    print(f"victim   : {result['victim']}")
    print(f"files    : {result['files']}")
    for item in result["created"]:
        print(f"  + {item}")
    if not paths.plugins.joinpath("sample_stage.py").exists():
        print("warning: sample plugin was not copied; plugin stage will fail", file=sys.stderr)
    return 0


async def command_run(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    seed = int(config.get("runtime.seed", 0))
    ensure_lab(paths, seed=seed)
    manager = SessionManager(paths)
    session = manager.create(metadata={"command": "run", "profile": args.profile or DEFAULT_PROFILE})
    ctx, bus = make_context(config, paths, session, quiet=args.quiet, verbose=args.verbose)
    await bus.start()
    heartbeat = Heartbeat(session, float(config.get("runtime.heartbeat_interval", 1.0)))
    await heartbeat.start()
    code = 0
    summary: dict[str, Any] = {}
    try:
        engine = Engine(ctx)
        summary = await engine.run()
        if config.get("recovery.auto_cleanup", False):
            report = await run_cleanup(ctx)
            summary["cleanup"] = {"ok": report.get("ok"), "removed": len(report.get("removed", []))}
        session.status = str(summary.get("status", "completed"))
    except SimulatedCrash as exc:
        session.status = "interrupted"
        code = 3
        print(f"\ninterrupted: {exc}", file=sys.stderr)
        print(f"resume with: aptsim resume --session {session.session_id}", file=sys.stderr)
    except AptSimError as exc:
        session.status = "failed"
        code = 1
        print(f"error: {exc}", file=sys.stderr)
    finally:
        session.save()
        await heartbeat.stop()
        await bus.close()
        timeline = write_timeline(session.directory)
        print(f"\nsession  : {session.session_id}")
        print(f"events   : {session.events_path}")
        print(f"timeline : {timeline}")
        print(f"state    : {session.state_path}")
        if summary:
            print(
                "result   : status={status} duration={duration_s}s stages={stages} artifacts={artifacts}".format(
                    status=summary.get("status"),
                    duration_s=summary.get("duration_s"),
                    stages=len(summary.get("stages", [])),
                    artifacts=summary.get("artifacts"),
                )
            )
    return code


async def command_resume(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    manager = SessionManager(paths)
    session = open_session(manager, args.session)
    if not session.state_path.exists():
        print(f"error: session {session.session_id} has no state checkpoint", file=sys.stderr)
        return 2
    state = StateStore.load(session.state_path)
    if state.state.status.value == "completed":
        print(f"session {session.session_id} already completed; nothing to resume")
        return 0

    session.status = "resuming"
    session.save()
    ctx, bus = make_context(config, paths, session, resume=True, quiet=args.quiet, verbose=args.verbose)
    await bus.start()
    heartbeat = Heartbeat(session, float(config.get("runtime.heartbeat_interval", 1.0)))
    await heartbeat.start()
    code = 0
    summary: dict[str, Any] = {}
    try:
        summary = await Engine(ctx).run(resume=True)
        if args.cleanup:
            report = await run_cleanup(ctx)
            summary["cleanup"] = {"ok": report.get("ok"), "removed": len(report.get("removed", []))}
        session.status = str(summary.get("status", "completed"))
    except SimulatedCrash as exc:
        session.status = "interrupted"
        code = 3
        print(f"\ninterrupted again: {exc}", file=sys.stderr)
    except AptSimError as exc:
        session.status = "failed"
        code = 1
        print(f"error: {exc}", file=sys.stderr)
    finally:
        session.save()
        await heartbeat.stop()
        await bus.close()
        write_timeline(session.directory)
        if summary:
            print(
                f"resumed  : session={session.session_id} status={summary.get('status')} "
                f"restarts={ctx.state.state.restarts}"
            )
    return code


async def command_timeline(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    session = open_session(SessionManager(paths), args.session)
    events = load_events(session.events_path)
    if args.json:
        print(json.dumps(events, indent=2))
        return 0
    text = render(events, title=f"session {session.session_id}", limit=args.limit)
    print(text)
    if args.write:
        target = write_timeline(session.directory, limit=args.limit)
        print(f"written: {target}")
    return 0


async def command_verify(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    session = open_session(SessionManager(paths), args.session)
    from .core.artifacts import sha256_file

    baseline_path = session.directory / "file-baseline.json"
    baseline: dict[str, str] = {}
    if baseline_path.exists():
        for entry in json.loads(baseline_path.read_text(encoding="utf-8")).get("files", []):
            if entry.get("sha256"):
                baseline[entry["path"]] = entry["sha256"]

    def classify(path: str, current: str, stored: str, original: str) -> str:
        if path in baseline and current == baseline[path]:
            return "restored"
        if current == original:
            return "restored"
        if current == stored:
            return "transformed"
        return "tampered"

    report: dict[str, Any] = {"session_id": session.session_id, "transform": [], "edits": [], "ok": True}
    transform_stored: dict[str, str] = {}
    manifest_path = session.directory / "transform-manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest.get("files", []):
            target = paths.resolve_relative(entry["path"])
            if not target.exists():
                state = "missing"
            else:
                digest = sha256_file(target)
                state = classify(entry["path"], digest, entry["sha256"], entry["orig_sha256"])
            transform_stored[entry["path"]] = entry["sha256"]
            report["transform"].append({"path": entry["path"], "state": state})
            if state in {"tampered", "missing"}:
                report["ok"] = False
    edits_path = session.directory / "edits-manifest.json"
    if edits_path.exists():
        manifest = json.loads(edits_path.read_text(encoding="utf-8"))
        for entry in manifest.get("files", []):
            target = paths.resolve_relative(entry["path"])
            if not target.exists():
                state = "missing"
            else:
                digest = sha256_file(target)
                state = classify(entry["path"], digest, entry["sha256"], entry["orig_sha256"])
                if state == "tampered" and transform_stored.get(entry["path"]) == digest:
                    state = "transformed"
            report["edits"].append({"path": entry["path"], "state": state})
            if state in {"tampered", "missing"}:
                report["ok"] = False
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] or args.lenient else 1


async def command_cleanup(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    session = open_session(SessionManager(paths), args.session)
    journal = ArtifactJournal(session.artifacts_path, paths, session.session_id)
    collected: list[tuple[str, str, dict[str, Any]]] = []
    engine = CleanupEngine(
        paths,
        journal,
        session.directory,
        dry_run=args.dry_run,
        on_event=lambda category, action, data: collected.append((category, action, data)),
    )
    report = engine.run()
    for category, action, data in collected:
        print(f"{category}.{action}: {json.dumps(data, sort_keys=True)}")
    print(json.dumps({key: report[key] for key in ("session_id", "ok", "dry_run", "removed", "errors")}, indent=2))
    return 0 if report["ok"] else 1


async def command_c2_demo(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    seed = int(config.get("runtime.seed", 0))
    ensure_lab(paths, seed=seed)
    session = SessionManager(paths).create(metadata={"command": "c2-demo"})
    ctx, bus = make_context(config, paths, session, quiet=args.quiet, verbose=args.verbose)
    await bus.start()
    tasks = _scenario_tasks(args, config)
    dispatcher = Dispatcher.from_config(ctx)
    secret = str(config.get("network.auth.shared_secret", "lab-only-shared-secret"))
    server = C2Server(
        host="127.0.0.1",
        port=int(config.get("network.port", 0)),
        secret=secret,
        tasks=tasks,
        tasks_per_poll=int(config.get("network.tasks_per_poll", 2)),
        on_event=lambda action, data: ctx.emit("network", action, "c2.server", data=data),
    )
    port = await server.start()
    client = C2Client(host="127.0.0.1", port=port, secret=secret, session_id=session.session_id)
    await client.connect()
    try:
        await client.register(["file-inventory", "beacon"])
        processed = await play_session(
            ctx,
            client,
            dispatcher,
            max_polls=int(config.get("network.max_polls", 12)),
            heartbeat_count=int(config.get("network.heartbeats", 3)),
            heartbeat_interval=float(config.get("runtime.heartbeat_interval", 1.0)),
        )
        print(f"loopback port : {port}")
        print(f"tasks handled : {processed}")
        print(f"rejected      : {dispatcher.rejected}  errors: {dispatcher.errors}")
    finally:
        await client.close()
        await server.stop()
        await bus.close()
        write_timeline(session.directory)
        print(f"session       : {session.session_id}")
        print(f"events        : {session.events_path}")
    return 0


async def command_c2_server(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    paths.ensure_layout()
    session_id = f"c2-server-{os.getpid()}"
    sink = JSONLFileSink(paths.c2 / "server-events.jsonl")
    bus = EventBus(session_id, [sink])
    await bus.start()

    async def on_event(action: str, data: dict[str, Any]) -> None:
        await bus.emit("network", action, "c2.server", data=data)

    server = C2Server(
        host="127.0.0.1",
        port=int(args.port or config.get("network.port", 8765)),
        secret=str(config.get("network.auth.shared_secret", "lab-only-shared-secret")),
        tasks=_scenario_tasks(args, config),
        tasks_per_poll=int(config.get("network.tasks_per_poll", 2)),
        on_event=on_event,
    )
    port = await server.start()
    print(f"simulated C2 listening on 127.0.0.1:{port} (scenario tasks={len(server.scenario.tasks)})")
    print(f"events: {paths.c2 / 'server-events.jsonl'}")
    print("press Ctrl-C to stop")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
        await bus.close()
    return 0


async def command_c2_client(args: argparse.Namespace) -> int:
    config = load_config(args)
    port = int(args.port or config.get("network.port", 8765))
    session_id = args.session_id or f"observe-{os.getpid()}"
    secret = str(config.get("network.auth.shared_secret", "lab-only-shared-secret"))
    client = C2Client(host="127.0.0.1", port=port, secret=secret, session_id=session_id)
    await client.connect()
    try:
        registered = await client.register(["observe-only"])
        print(json.dumps({"registered": registered}, indent=2, default=str))
        for seq in range(1, int(config.get("network.heartbeats", 2)) + 1):
            ack = await client.heartbeat(seq)
            print(f"heartbeat {seq} -> beats={ack.get('beats')}")
        for _ in range(int(config.get("network.max_polls", 4))):
            response = await client.poll()
            for task in response.get("tasks", []):
                print(f"task {task.get('task_id')} type={task.get('type')} args={task.get('args')}")
                await client.send_result(
                    task["task_id"],
                    "skipped",
                    None,
                    "standalone observer does not execute tasks",
                )
            if response.get("exhausted"):
                break
    finally:
        await client.close()
    return 0


async def command_doctor(args: argparse.Namespace) -> int:
    config = load_config(args)
    paths = build_paths(config)
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    checks.append(("config dir", config_dir().exists(), str(config_dir())))
    checks.append(("lab root", paths.root.exists(), str(paths.root)))
    checks.append(("lab writable", os.access(paths.root, os.W_OK) if paths.root.exists() else False, ""))
    checks.append(("loopback-only C2", config.get("network.host") in {"127.0.0.1", "::1"}, str(config.get("network.host"))))
    checks.append(("transform locked to lab", config.get("safety.allow_outside_lab") is False, "allow_outside_lab=false"))
    try:
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        checks.append(("loopback bind", True, f"127.0.0.1:{port}"))
    except OSError as exc:
        checks.append(("loopback bind", False, str(exc)))
    checks.append(("sample plugin", (paths.plugins / "sample_stage.py").exists(), str(paths.plugins / "sample_stage.py")))
    failed = 0
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'fail'}] {name:<26} {detail}")
        failed += 0 if ok else 1
    print(f"\nprofile: {args.profile or DEFAULT_PROFILE}  digest: {config.digest()[:16]}")
    return 0 if failed == 0 else 1


async def command_list_modules(args: argparse.Namespace) -> int:
    registry = ModuleRegistry.default()
    print("built-in modules:")
    for name in registry.names():
        cls = registry.get(name)
        print(f"  {name:<16} {getattr(cls, 'description', '')}")
    print("\nplugin specification: plugin:<filename>  (file must live in <lab>/plugins)")
    print("plugin contract: PLUGIN_API = 1 and MODULE = <SimModule subclass>")
    return 0


def _scenario_tasks(args: argparse.Namespace, config: Config) -> list[dict[str, Any]]:
    scenario_path = getattr(args, "scenario", None)
    if scenario_path:
        data = json.loads(Path(scenario_path).expanduser().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return list(data.get("tasks", []))
        return list(data)
    return list(config.get("network.scenario", []))


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config-dir", help="directory containing default.json and profiles/")
    common.add_argument("--lab", help="lab root (default: APTSIM_LAB or config lab.root)")
    common.add_argument("--profile", help="configuration profile name or path")
    common.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="config override")
    common.add_argument("--seed", type=int, help="override runtime.seed")
    common.add_argument("-q", "--quiet", action="store_true", help="suppress console event stream")
    common.add_argument("-v", "--verbose", action="store_true", help="include debug events on console")

    parser = argparse.ArgumentParser(
        prog="aptsim",
        description="Inert APT behaviour simulation framework (lab use only)",
    )
    parser.add_argument("--version", action="version", version=f"aptsim {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", parents=[common], help="create the disposable lab and synthetic files")
    init.add_argument("--force", action="store_true", help="overwrite synthetic files")
    init.set_defaults(func=command_init)

    run = sub.add_parser("run", parents=[common], help="execute the full simulation pipeline")
    run.add_argument("--stop-after", metavar="STAGE_ID", help="simulate a crash after this stage")
    run.set_defaults(func=command_run)

    resume = sub.add_parser("resume", parents=[common], help="resume an interrupted session")
    resume.add_argument("--session", help="session id (default: latest)")
    resume.add_argument("--cleanup", action="store_true", help="run cleanup after successful resume")
    resume.set_defaults(func=command_resume)

    timeline = sub.add_parser("timeline", parents=[common], help="render the execution timeline")
    timeline.add_argument("--session", help="session id (default: latest)")
    timeline.add_argument("--limit", type=int, default=5000)
    timeline.add_argument("--json", action="store_true", help="print raw events as JSON")
    timeline.add_argument("--write", action="store_true", help="write timeline.txt into the session")
    timeline.set_defaults(func=command_timeline)

    verify = sub.add_parser("verify", parents=[common], help="verify transform/edit integrity")
    verify.add_argument("--session", help="session id (default: latest)")
    verify.add_argument("--lenient", action="store_true", help="exit 0 even on mismatches")
    verify.set_defaults(func=command_verify)

    cleanup = sub.add_parser("cleanup", parents=[common], help="restore files and remove simulated artifacts")
    cleanup.add_argument("--session", help="session id (default: latest)")
    cleanup.add_argument("--dry-run", action="store_true", help="show the cleanup plan only")
    cleanup.set_defaults(func=command_cleanup)

    c2_demo = sub.add_parser("c2-demo", parents=[common], help="run a loopback client/server demo")
    c2_demo.add_argument("--scenario", help="scenario JSON file (default: config network.scenario)")
    c2_demo.set_defaults(func=command_c2_demo)

    c2_server = sub.add_parser("c2-server", parents=[common], help="run the standalone loopback server")
    c2_server.add_argument("--port", type=int, help="port (default: config or 8765)")
    c2_server.add_argument("--scenario", help="scenario JSON file")
    c2_server.set_defaults(func=command_c2_server)

    c2_client = sub.add_parser("c2-client", parents=[common], help="observe a standalone server (no task execution)")
    c2_client.add_argument("--port", type=int, help="port (default: config or 8765)")
    c2_client.add_argument("--session-id", help="client session id")
    c2_client.set_defaults(func=command_c2_client)

    doctor = sub.add_parser("doctor", parents=[common], help="check environment and safety locks")
    doctor.set_defaults(func=command_doctor)

    modules = sub.add_parser("list-modules", parents=[common], help="list built-in modules and plugin contract")
    modules.set_defaults(func=command_list_modules)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted by user", file=sys.stderr)
        return 130
    except AptSimError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
