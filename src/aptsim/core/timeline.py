"""Execution timeline rendering.

Reads ``events.jsonl`` and produces a compact, deterministic text timeline plus
a per-stage duration table. Used by ``aptsim timeline`` and by the launch
script so every run leaves a human readable trace next to the JSONL log.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_events(path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def render(events: list[dict[str, Any]], *, title: str = "", limit: int = 5000) -> str:
    lines: list[str] = []
    if not events:
        return "no events recorded\n"
    first = events[0]
    last = events[-1]
    duration = max(0.0, float(last.get("t_rel", 0.0)))
    header = title or f"session {first.get('session_id', '?')}"
    lines.append("=" * 96)
    lines.append(f" aptsim execution timeline | {header}")
    lines.append(
        f" events={len(events)} duration={duration:.3f}s first={first.get('category')}.{first.get('action')}"
    )
    lines.append("=" * 96)
    lines.append(f" {'offset':>9}  {'category.action':<34} {'module':<14} detail")
    lines.append("-" * 96)

    for event in events[:limit]:
        offset = f"+{float(event.get('t_rel', 0.0)):8.3f}s"
        tag = f"{event.get('category')}.{event.get('action')}"
        module = str(event.get("module", ""))[:14]
        detail = _detail(event.get("data", {}))
        lines.append(f" {offset}  {tag:<34} {module:<14} {detail}")

    if len(events) > limit:
        lines.append(f" ... {len(events) - limit} events omitted (limit={limit})")

    durations = _stage_durations(events)
    if durations:
        lines.append("")
        lines.append(" stage durations")
        lines.append("-" * 96)
        widest = max(duration for _, duration in durations) or 1.0
        for stage, duration in durations:
            bar = "#" * max(1, int(round(28 * duration / widest)))
            lines.append(f" {stage:<28} {duration:8.4f}s {bar}")
    return "\n".join(lines) + "\n"


def _detail(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return str(data)
    priority = (
        "stage_id",
        "path",
        "task_id",
        "status",
        "score",
        "verdict",
        "manifest",
        "session_id",
        "error",
        "reason",
        "files",
        "count",
    )
    parts: list[str] = []
    for key in priority:
        if key in data and data[key] is not None:
            value = data[key]
            text = str(value)
            if len(text) > 56:
                text = text[:53] + "..."
            parts.append(f"{key}={text}")
        if len(parts) >= 4:
            break
    if not parts:
        for key, value in list(data.items())[:3]:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _stage_durations(events: list[dict[str, Any]]) -> list[tuple[str, float]]:
    starts: dict[str, float] = {}
    durations: list[tuple[str, float]] = []
    for event in events:
        if event.get("category") != "lifecycle":
            continue
        data = event.get("data", {})
        stage = data.get("stage_id")
        if not stage:
            continue
        if event.get("action") == "stage.start":
            starts[stage] = float(event.get("t_rel", 0.0))
        elif event.get("action") in {"stage.completed", "stage.failed"} and stage in starts:
            durations.append((stage, max(0.0, float(event.get("t_rel", 0.0)) - starts[stage])))
    return durations


def write_timeline(session_dir: str | Path, *, limit: int = 5000) -> Path:
    session_dir = Path(session_dir)
    events = load_events(session_dir / "events.jsonl")
    output = session_dir / "timeline.txt"
    output.write_text(render(events, limit=limit), encoding="utf-8")
    return output
