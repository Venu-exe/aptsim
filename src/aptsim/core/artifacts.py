"""Artifact journal.

Every file the framework creates or mutates is recorded here so the cleanup
engine can reverse it without guessing. Records are lab-relative and appended
with a lock so worker threads can journal safely.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from .paths import LabPaths


def sha256_file(path: str | Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


class ArtifactJournal:
    def __init__(self, path: str | Path, paths: LabPaths, session_id: str) -> None:
        self.path = Path(path)
        self.paths = paths
        self.session_id = session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(
        self,
        target: str | Path,
        kind: str,
        *,
        sha256: str | None = None,
        **meta: Any,
    ) -> dict[str, Any]:
        resolved = self.paths.guard(target, what=f"artifact {kind}")
        if not resolved.is_relative_to(self.paths.root):
            raise ValueError("artifact must live inside the lab")
        relative = str(resolved.relative_to(self.paths.root))
        record = {
            "ts": time.time(),
            "session_id": self.session_id,
            "kind": kind,
            "path": relative,
            "sha256": sha256,
            "meta": meta,
        }
        line = json.dumps(record, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        return record

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
        return records

    def resolve(self, record: dict[str, Any]) -> Path:
        return self.paths.resolve_relative(record["path"], what=f"artifact {record['kind']}")
