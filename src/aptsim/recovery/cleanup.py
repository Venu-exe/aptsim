"""Cleanup and recovery engine.

Restores every reversible mutation recorded by a session, removes the simulated
artifacts that session created, and verifies the result against the recorded
SHA-256 baseline.

Order matters: the transform is reversed first (it was applied last), then the
append-marker edits are reversed. Backups are consumed by the restore path and
never deleted before verification succeeds -- the engine fails closed if a hash
does not match.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from aptsim.core.artifacts import ArtifactJournal, sha256_file
from aptsim.core.paths import LabPaths
from aptsim.modules.fileops import restore_edits, restore_transform

EventHook = Callable[[str, str, dict[str, Any]], None]


class CleanupEngine:
    REMOVABLE_KINDS = {"persistence", "persistence-state", "plugin-copy"}

    def __init__(
        self,
        paths: LabPaths,
        journal: ArtifactJournal,
        session_dir: str | Path,
        *,
        dry_run: bool = False,
        on_event: EventHook | None = None,
    ) -> None:
        self.paths = paths
        self.journal = journal
        self.session_dir = Path(session_dir)
        self.dry_run = dry_run
        self.on_event = on_event

    def _emit(self, category: str, action: str, data: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(category, action, data)

    def run(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "session_id": self.session_dir.name,
            "started_at": time.time(),
            "dry_run": self.dry_run,
            "transform": None,
            "edits": None,
            "removed": [],
            "errors": [],
            "ok": False,
        }

        self._emit("cleanup", "begin", {"session": report["session_id"], "dry_run": self.dry_run})

        if self.dry_run:
            report["plan"] = self._plan()
            report["ok"] = True
            self._emit("cleanup", "dry_run", {"artifacts": len(report["plan"]["artifacts"])})
            return report

        transform_path = self.session_dir / "transform-manifest.json"
        if transform_path.exists():
            manifest = json.loads(transform_path.read_text(encoding="utf-8"))
            result = restore_transform(
                self.paths,
                manifest,
                verify=bool(manifest.get("files")),
                on_event=lambda action, payload: self._emit("cleanup", action, payload),
            )
            report["transform"] = result
            if result["errors"]:
                report["errors"].extend(result["errors"])

        edits_path = self.session_dir / "edits-manifest.json"
        if edits_path.exists():
            manifest = json.loads(edits_path.read_text(encoding="utf-8"))
            result = restore_edits(
                self.paths,
                manifest,
                on_event=lambda action, payload: self._emit("cleanup", action, payload),
            )
            report["edits"] = result
            if result["errors"]:
                report["errors"].extend(result["errors"])

        for record in self.journal.load():
            kind = record.get("kind")
            if kind not in self.REMOVABLE_KINDS:
                continue
            target = self.paths.resolve_relative(record["path"])
            if target.exists():
                try:
                    target.unlink()
                    report["removed"].append(record["path"])
                    self._emit("cleanup", "artifact.removed", {"path": record["path"], "kind": kind})
                except OSError as exc:
                    report["errors"].append({"path": record["path"], "error": str(exc)})

        self._prune_empty_directories(report)
        report["integrity"] = self._integrity_check()
        if report["integrity"]["checked"] and not report["integrity"]["ok"]:
            report["errors"].append({"path": "*", "error": "post-cleanup integrity check failed"})

        report["ok"] = not report["errors"]
        report["finished_at"] = time.time()
        report["duration_s"] = round(report["finished_at"] - report["started_at"], 4)
        self.paths.cleanup_reports.mkdir(parents=True, exist_ok=True)
        report_path = self.paths.cleanup_reports / f"{report['session_id']}.json"
        tmp = report_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, report_path)
        self.session_dir.joinpath("cleanup-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        self._emit(
            "cleanup",
            "completed",
            {
                "ok": report["ok"],
                "removed": len(report["removed"]),
                "errors": len(report["errors"]),
                "report": str(report_path),
            },
        )
        return report

    def _integrity_check(self) -> dict[str, Any]:
        baseline_path = self.session_dir / "file-baseline.json"
        if not baseline_path.exists():
            return {"checked": False, "ok": True, "files": 0, "mismatches": []}
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        mismatches: list[dict[str, Any]] = []
        checked = 0
        for entry in baseline.get("files", []):
            if not entry.get("sha256"):
                continue
            target = self.paths.resolve_relative(entry["path"])
            checked += 1
            if not target.exists():
                mismatches.append({"path": entry["path"], "error": "missing"})
                continue
            if sha256_file(target) != entry["sha256"]:
                mismatches.append({"path": entry["path"], "error": "sha256-mismatch"})
        return {"checked": True, "ok": not mismatches, "files": checked, "mismatches": mismatches}

    def _prune_empty_directories(self, report: dict[str, Any]) -> None:
        candidates = [self.paths.backup_root]
        for root in candidates:
            if not root.exists():
                continue
            for current, directories, files in os.walk(root, topdown=False):
                directory = Path(current)
                if not files and not directories:
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
        if self.paths.persistence.exists():
            for current, directories, files in os.walk(self.paths.persistence, topdown=False):
                directory = Path(current)
                if not files and not directories:
                    try:
                        directory.rmdir()
                    except OSError:
                        pass

    def _plan(self) -> dict[str, Any]:
        artifacts = []
        for record in self.journal.load():
            target = self.paths.resolve_relative(record["path"])
            artifacts.append(
                {
                    "path": record["path"],
                    "kind": record["kind"],
                    "exists": target.exists(),
                    "removable": record.get("kind") in self.REMOVABLE_KINDS,
                }
            )
        return {
            "artifacts": artifacts,
            "transform_manifest": (self.session_dir / "transform-manifest.json").exists(),
            "edits_manifest": (self.session_dir / "edits-manifest.json").exists(),
        }
