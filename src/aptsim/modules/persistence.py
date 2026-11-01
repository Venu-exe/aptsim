"""Simulated persistence.

Nothing outside ``<lab>/persistence`` is written. The module renders realistic
startup artifacts (XDG autostart entry, cron fragment, systemd user unit) into
that directory, verifies them and records what a real implementation *would*
have touched. The restart counter demonstrates state that survives a process
restart and is consumed by the resume workflow.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .base import SimModule

TEMPLATES = {
    "xdg-autostart": (
        "# aptsim simulation artifact - not installed on the host\n"
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=aptsim-demo\n"
        "Comment=Inert lab simulation entry\n"
        "Exec={exec}\n"
        "Terminal=false\n"
        "X-APTSIM-Simulation=true\n"
    ),
    "cron": (
        "# aptsim simulation artifact - not installed on the host\n"
        "SHELL=/bin/sh\n"
        "PATH=/usr/local/bin:/usr/bin\n"
        "{schedule} root {command}\n"
        "# X-APTSIM-Simulation=true\n"
    ),
    "systemd-user": (
        "# aptsim simulation artifact - not installed on the host\n"
        "[Unit]\n"
        "Description=aptsim demo unit (simulated)\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart={exec}\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
        "# X-APTSIM-Simulation=true\n"
    ),
    "launch-agent-sim": (
        "# aptsim simulation artifact - not installed on the host\n"
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<plist><dict><key>Label</key><string>aptsim.demo</string>"
        "<key>ProgramArguments</key><array><string>{exec}</string></array></dict></plist>\n"
    ),
}


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    tmp = path.with_name(path.name + ".aptsim-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, mode)


def render_mechanism(mechanism: dict[str, Any]) -> str:
    kind = mechanism["kind"]
    template = TEMPLATES.get(kind)
    if template is None:
        raise ValueError(f"unsupported persistence kind: {kind}")
    payload = dict(mechanism.get("payload", {}))
    payload.setdefault("exec", "/opt/aptsim-demo/aptsim-demo --silent")
    payload.setdefault("schedule", "*/7 * * * *")
    payload.setdefault("command", "/opt/aptsim-demo/aptsim-demo --beacon")
    return template.format(**payload)


class PersistenceModule(SimModule):
    name = "persistence"
    description = "Renders startup artifacts into the lab persistence directory only"

    async def run(self, ctx) -> None:  # type: ignore[no-untyped-def]
        config = ctx.section("persistence")
        if not config.get("enabled", True):
            await ctx.emit(
                "persistence", "simulation.skipped", self.name, data={"reason": "disabled"}
            )
            return

        restart_state = self._bump_restart_counter(ctx, str(config.get("restart_state", "persistence/restart_state.json")))
        await ctx.emit(
            "persistence",
            "restart.counter",
            self.name,
            data={
                "runs": restart_state["runs"],
                "last_session": restart_state.get("last_session"),
                "state_path": restart_state["state_path"],
            },
        )

        mechanisms: list[dict[str, Any]] = list(config.get("mechanisms", []))
        created: list[dict[str, Any]] = []
        for mechanism in mechanisms:
            relative = str(mechanism.get("path", ""))
            target = ctx.paths.guard_persistence(relative)
            ctx.paths.assert_not_host_target(target)
            content = render_mechanism(mechanism)
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, content)
            created.append(
                {
                    "id": mechanism.get("id"),
                    "kind": mechanism.get("kind"),
                    "path": str(target.relative_to(ctx.paths.root)),
                    "bytes": len(content.encode("utf-8")),
                    "host_target": False,
                }
            )
            ctx.journal.record(target, "persistence", meta={"mechanism": mechanism.get("id")})
            await ctx.emit(
                "persistence",
                "registration.simulated",
                self.name,
                severity="warning",
                data={
                    "mechanism": mechanism.get("id"),
                    "kind": mechanism.get("kind"),
                    "path": str(target.relative_to(ctx.paths.root)),
                    "would_target_host": "not performed",
                    "simulation": True,
                },
            )

        if config.get("verify", True):
            for entry in created:
                target = ctx.paths.resolve_relative(entry["path"])
                exists = target.is_file()
                await ctx.emit(
                    "persistence",
                    "artifact.verified",
                    self.name,
                    severity="info" if exists else "warning",
                    data={"path": entry["path"], "exists": exists, "lab_only": True},
                )

        ctx.write_json(
            "persistence-report.json",
            {
                "session_id": ctx.session.session_id,
                "mechanisms": created,
                "restart_state": restart_state,
                "host_modified": False,
                "synthetic": True,
            },
        )
        await ctx.emit(
            "persistence",
            "simulation.completed",
            self.name,
            data={"mechanisms": len(created), "host_modified": False},
        )

    def _bump_restart_counter(self, ctx, relative: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        target = ctx.paths.guard_persistence(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {"runs": 0, "sessions": []}
        if target.exists():
            try:
                state = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {"runs": 0, "sessions": []}
        state["runs"] = int(state.get("runs", 0)) + 1
        state["last_session"] = ctx.session.session_id
        state["updated_at"] = time.time()
        state["state_path"] = str(target.relative_to(ctx.paths.root))
        sessions = list(state.get("sessions", []))
        sessions.append(ctx.session.session_id)
        state["sessions"] = sessions[-20:]
        _atomic_write(target, json.dumps(state, indent=2, sort_keys=True))
        ctx.journal.record(target, "persistence-state")
        return state
