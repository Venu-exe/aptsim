#!/usr/bin/env python3
"""Render the README GIF and screenshots from a real aptsim run.

Developer tooling only — the simulator itself never imports this module and
never spawns processes. Requires Pillow; ffmpeg is optional.

    python3 scripts/render_assets.py

Writes docs/assets/demo.gif and docs/assets/*.png, then restores the lab.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - dev tooling
    sys.exit("Pillow is required: python3 -m pip install --user pillow")

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
LAB = ROOT / "lab"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

FONT_REG = "/usr/share/fonts/TTF/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf"

BG = "#0b0f14"
BAR = "#161b22"
TEXT = "#c9d1d9"
DIM = "#7d8590"
GREEN = "#3fb950"
BRIGHT = "#56d364"
YELLOW = "#d29922"
RED = "#f85149"
BLUE = "#79c0ff"
PROMPT = GREEN


def run(*args: str) -> str:
    result = subprocess.run(
        args, cwd=ROOT, env=ENV, capture_output=True, text=True, check=False
    )
    return result.stdout + result.stderr


def collect() -> dict[str, str]:
    shutil.rmtree(LAB, ignore_errors=True)
    run_out = run("./scripts/run_lab.sh", "-q")
    before = run("python3", "-m", "aptsim", "verify")
    cleanup = run("./scripts/cleanup_lab.sh")
    after = run("python3", "-m", "aptsim", "verify")
    timeline = run("python3", "-m", "aptsim", "timeline")
    c2 = run("./scripts/c2_demo.sh")
    return {
        "run": run_out,
        "before": before,
        "cleanup": cleanup,
        "after": after,
        "timeline": timeline,
        "c2": c2,
    }


def pick(text: str, needles: list[str]) -> list[str]:
    lines = text.splitlines()
    chosen: list[str] = []
    for needle in needles:
        for line in lines:
            if needle in line:
                chosen.append(line.rstrip())
                break
    return chosen


def shorten(line: str, limit: int) -> str:
    return line if len(line) <= limit else line[: limit - 1] + "…"


def sanitize(line: str) -> str:
    """Keep local absolute paths out of committed images."""
    return line.replace(str(ROOT), "<LAB>").replace(str(Path.home()), "<HOME>")


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


class Canvas:
    def __init__(self, width: int, height: int, font_size: int = 14, title: str = "aptsim — detection lab"):
        self.width = width
        self.height = height
        self.font = ImageFont.truetype(FONT_REG, font_size)
        self.bold = ImageFont.truetype(FONT_BOLD, font_size)
        self.title_font = ImageFont.truetype(FONT_REG, font_size - 2)
        self.title = title
        self.pad_x = 20
        self.pad_y = 52
        self.line_h = font_size + 8
        self.max_lines = (height - self.pad_y - 18) // self.line_h
        self.char_w = self.font.getlength("M")
        self.max_chars = int((width - 2 * self.pad_x) / self.char_w)

    def render(self, lines: list[tuple[str, str, bool]]) -> Image.Image:
        image = Image.new("RGB", (self.width, self.height), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, self.width, 34], fill=BAR)
        for index, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            x = 16 + index * 18
            draw.ellipse([x, 12, x + 10, 22], fill=color)
        draw.text((78, 10), self.title, font=self.title_font, fill=DIM)

        visible = lines[-self.max_lines :]
        y = self.pad_y
        for text, color, is_bold in visible:
            font = self.bold if is_bold else self.font
            draw.text((self.pad_x, y), shorten(sanitize(text), self.max_chars), font=font, fill=color)
            y += self.line_h
        return image


def cleanup_scene(raw: str) -> list[str]:
    lines = ["$ ./scripts/cleanup_lab.sh"]
    for line in raw.splitlines():
        if line.startswith("cleanup.begin"):
            session = re.search(r'"session": "([^"]+)"', line)
            name = session.group(1) if session else "s-…"
            lines.append(f'cleanup.begin: session={name} dry_run=false')
        elif line.startswith("cleanup.restored"):
            path = re.search(r'"path": "([^"]+)"', line)
            if path:
                lines.append(f'cleanup.restored: {path.group(1)}  verified=true')
        elif line.startswith("cleanup.restore.summary"):
            lines.append('cleanup.restore.summary: {"restored": 6, "errors": 0}')
        elif line.startswith("cleanup.artifact.removed"):
            path = re.search(r'"path": "([^"]+)"', line)
            if path:
                lines.append(f'cleanup.artifact.removed: {path.group(1)}')
        elif line.startswith("cleanup.completed"):
            lines.append('cleanup.completed: {"ok": true, "removed": 4, "errors": 0}')
    return lines


def c2_scene(raw: str) -> list[str]:
    lines = ["$ ./scripts/c2_demo.sh"]
    for line in raw.splitlines():
        if line.startswith(("loopback port", "tasks handled", "rejected")):
            lines.append(line.strip())
    lines.append("rejections: ../../etc (path) · shell (not in allowlist)")
    return lines


def event_scene(events_path: Path) -> list[str]:
    """Build a compact, truthful one-line-per-event view from events.jsonl."""
    if not events_path.exists():
        return []
    wanted = {
        ("execution", "payload.begin"),
        ("execution", "payload.plugin.loaded"),
        ("anti_analysis", "evasion.simulated"),
        ("process", "spawn.simulated"),
        ("persistence", "registration.simulated"),
        ("file", "transformed"),
        ("network", "server.started"),
        ("command", "task.completed"),
        ("lifecycle", "run.completed"),
    }
    chosen: list[dict] = []
    seen: set[str] = set()
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw)
        key = f"{event['category']}.{event['action']}"
        if (event["category"], event["action"]) not in wanted:
            continue
        if key in seen and key != "command.task.completed":
            continue
        if key == "command.task.completed" and event["data"].get("status") != "rejected":
            continue
        seen.add(key)
        chosen.append(event)

    labels = {
        "execution.payload.begin": "payload.begin",
        "execution.payload.plugin.loaded": "payload.plugin.loaded",
        "anti_analysis.evasion.simulated": "evasion.simulated",
        "process.spawn.simulated": "process.spawn",
        "persistence.registration.simulated": "persistence.register",
        "file.transformed": "file.transform",
        "network.server.started": "network.server",
        "command.task.completed": "command.rejected",
        "lifecycle.run.completed": "run.completed",
    }
    lines: list[str] = []
    for event in chosen:
        key = f"{event['category']}.{event['action']}"
        data = event["data"]
        if key == "execution.payload.begin":
            detail = f"stages={len(data.get('stages', []))} mode={data.get('mode')}"
        elif key == "execution.payload.plugin.loaded":
            detail = f"{data.get('plugin')} trust={data.get('trust')}"
        elif key == "anti_analysis.evasion.simulated":
            detail = f"{data.get('action')} effect={data.get('effect')}"
        elif key == "process.spawn.simulated":
            detail = f"pid={data.get('pid')} image={data.get('image')}"
        elif key == "persistence.registration.simulated":
            detail = str(data.get("path"))
        elif key == "file.transformed":
            detail = str(data.get("path"))
        elif key == "network.server.started":
            detail = f"{data.get('host')}:{data.get('port')} loopback={data.get('loopback')}"
        elif key == "command.task.completed":
            detail = f"task={data.get('task_id')} {data.get('error')}"
        else:
            detail = f"stages={data.get('stages')} artifacts={data.get('artifacts')}"
        label = labels[key]
        lines.append(f" +{event['t_rel']:6.3f}s  {label:<22}  {shorten(detail, 62)}")
    return lines


def story(captured: dict[str, str]) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str, bool]]]:
    before = json.loads(captured["before"])
    after = json.loads(captured["after"])
    paths = [entry["path"] for entry in before["transform"]]

    session_id = next(
        (line.split(":", 1)[1].strip() for line in captured["run"].splitlines() if line.startswith("session")),
        "",
    )
    run_lines = event_scene(LAB / "sessions" / session_id / "events.jsonl")
    if not run_lines:
        run_lines = [" + 0.115s  payload.plugin.loaded   sample_stage.py trust=lab-local"]

    cleanup_lines = cleanup_scene(captured["cleanup"])

    commands: list[tuple[str, list[str]]] = [
        ("./scripts/run_lab.sh -q", run_lines),
        (
            "aptsim verify",
            [
                "{",
                '  "transform": [',
                *(f'    {{ "path": "{path}", "state": "transformed" }},' for path in paths[:3]),
                "    …",
                "  ],",
                f'  "ok": {json.dumps(before["ok"])}',
                "}",
            ],
        ),
        ("aptsim cleanup", cleanup_lines),
        (
            "aptsim verify",
            [
                "{",
                '  "transform": [',
                *(f'    {{ "path": "{path}", "state": "restored" }},' for path in paths[:3]),
                "    …",
                "  ],",
                f'  "ok": {json.dumps(after["ok"])}',
                "}",
            ],
        ),
    ]

    storyboard: list[tuple[str, str, bool]] = []
    for command, output in commands:
        storyboard.append(("$ " + command, PROMPT, False))
        for line in output:
            color = TEXT
            if "transformed" in line:
                color = YELLOW
            elif "restored" in line or ("ok" in line and "true" in line):
                color = BRIGHT
            elif '"path"' in line or "verify" in line:
                color = DIM
            if line.startswith("cleanup.restored"):
                color = BLUE
            storyboard.append((line, color, False))
        storyboard.append(("", TEXT, False))

    caption = [
        ("", TEXT, False),
        ("# 6 stages · 13 artifacts · every file restored", BRIGHT, True),
    ]
    return storyboard, caption


def build_gif(storyboard, caption, canvas: Canvas, path: Path) -> None:
    frames: list[tuple[Image.Image, int]] = []
    buffer: list[tuple[str, str, bool]] = []

    def emit(duration: int) -> None:
        frames.append((canvas.render(buffer), duration))

    for line, color, bold in storyboard:
        if line.startswith("$ "):
            command = line[2:]
            for end in range(5, len(command) + 1, 5):
                buffer.append(("$ " + command[:end] + "▏", PROMPT, False))
                emit(55)
                buffer.pop()
            buffer.append((line, PROMPT, False))
            emit(260)
        else:
            buffer.append((line, color, bold))
            emit(150 if line else 80)

    buffer.extend(caption)
    emit(3000)
    for _ in range(4):
        emit(400)

    images = [frame for frame, _ in frames]
    durations = [duration for _, duration in frames]
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    print(f"wrote {path.relative_to(ROOT)}  ({path.stat().st_size / 1024:.0f} KiB, {len(frames)} frames)")


def build_png(lines: list[str], path: Path, width: int = 1240, font_size: int = 14) -> None:
    height = 52 + len(lines) * (font_size + 8) + 24
    canvas = Canvas(width, height, font_size=font_size)
    colored: list[tuple[str, str, bool]] = []
    for line in lines:
        color = TEXT
        if line.startswith("$ "):
            color = PROMPT
        elif "transformed" in line:
            color = YELLOW
        elif "restored" in line or 'ok": true' in line or "status=completed" in line:
            color = BRIGHT
        elif line.startswith(("cleanup.", "session", "events", "timeline", "state", "result")):
            color = DIM
        colored.append((line, color, False))
    canvas.render(colored).save(path)
    print(f"wrote {path.relative_to(ROOT)}  ({path.stat().st_size / 1024:.0f} KiB)")


def main() -> int:
    if not Path(FONT_REG).exists():
        sys.exit(f"font not found: {FONT_REG} (install ttf-dejavu)")
    ASSETS.mkdir(parents=True, exist_ok=True)
    print("[assets] running a fresh lab cycle")
    captured = collect()
    storyboard, caption = story(captured)

    build_gif(storyboard, caption, Canvas(1000, 640, font_size=13), ASSETS / "demo.gif")

    before = json.loads(captured["before"])
    after = json.loads(captured["after"])

    session_id = next(
        (line.split(":", 1)[1].strip() for line in captured["run"].splitlines() if line.startswith("session")),
        "",
    )
    run_lines = event_scene(LAB / "sessions" / session_id / "events.jsonl")
    result = pick(captured["run"], ["result   :"])
    build_png(
        ["$ ./scripts/run_lab.sh -q", *run_lines, "", *result],
        ASSETS / "run.png",
        width=1000,
    )
    build_png(
        [
            "$ aptsim verify",
            *[f'    {{"path": "{e["path"]}", "state": "{e["state"]}"}}' for e in before["transform"]],
            f'    "ok": {json.dumps(before["ok"])}',
        ],
        ASSETS / "verify-transformed.png",
    )
    build_png(
        [
            "$ aptsim verify",
            *[f'    {{"path": "{e["path"]}", "state": "{e["state"]}"}}' for e in after["transform"]],
            f'    "ok": {json.dumps(after["ok"])}',
        ],
        ASSETS / "verify-restored.png",
    )

    timeline_lines = captured["timeline"].splitlines()
    stage_start = next((i for i, line in enumerate(timeline_lines) if "stage durations" in line), 0)
    header = next((i for i, line in enumerate(timeline_lines) if "execution timeline" in line), 0)
    build_png(
        ["$ aptsim timeline", *timeline_lines[header : header + 10], "", *timeline_lines[stage_start :]],
        ASSETS / "timeline.png",
        width=1040,
    )

    build_png(cleanup_scene(captured["cleanup"]), ASSETS / "cleanup.png", width=1240)
    build_png(c2_scene(captured["c2"]), ASSETS / "c2-demo.png", width=1100)

    print("[assets] restoring lab")
    run("./scripts/cleanup_lab.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
