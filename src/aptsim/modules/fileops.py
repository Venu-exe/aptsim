"""File operation simulation: discovery, metadata, reversible edits and
reversible "ransomware-shaped" transforms.

Everything here is intentionally reversible and confined to the disposable
victim tree:

* discovery walks the tree, hashes files and computes Shannon entropy;
* edits append a visible marker to selected files after backing them up;
* transforms XOR each file with a SHA-256 counter-mode keystream. Per-file keys
  are stored in a manifest, originals are backed up, and restore verifies both
  the encrypted and the original SHA-256 before writing anything back.

A real ransomware sample would delete keys and backups. This module does the
opposite on purpose: the manifest is the recovery contract and cleanup fails
closed if integrity does not match.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from aptsim.core.artifacts import sha256_file
from aptsim.core.errors import IntegrityError, PathViolation
from aptsim.core.paths import LabPaths

from .base import SimModule


@dataclass
class FileMeta:
    path: str
    size: int
    mode: int
    mtime: float
    sha256: str | None = None
    entropy: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    length = len(data)
    entropy = 0.0
    for count in counts.values():
        probability = count / length
        entropy -= probability * math.log2(probability)
    return round(entropy, 4)


def derive_key(seed: int, relative: str) -> bytes:
    return hashlib.sha256(f"{seed}:{relative}".encode("utf-8")).digest()


def keystream(key: bytes, length: int) -> bytes:
    blocks = bytearray()
    counter = 0
    while len(blocks) < length:
        blocks.extend(hashlib.sha256(key + counter.to_bytes(8, "big")).digest())
        counter += 1
    return bytes(blocks[:length])


def xor_bytes(data: bytes, key: bytes) -> bytes:
    stream = keystream(key, len(data))
    return bytes(left ^ right for left, right in zip(data, stream))


def iter_files(root: Path, *, max_files: int = 10_000) -> list[Path]:
    """Deterministic recursive discovery; symlinks are never followed."""
    found: list[Path] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if not (Path(current) / name).is_symlink())
        for name in sorted(filenames):
            candidate = Path(current) / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            found.append(candidate)
            if len(found) >= max_files:
                return found
    return found


def collect_metadata(path: Path) -> FileMeta:
    stat = path.stat()
    return FileMeta(
        path=str(path),
        size=stat.st_size,
        mode=stat.st_mode & 0o777,
        mtime=stat.st_mtime,
    )


def write_atomic(path: Path, data: bytes, mode: int | None = None) -> None:
    tmp = path.with_name(path.name + ".aptsim-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    if mode is not None:
        os.chmod(path, mode)


def transform_files(
    paths: LabPaths,
    files: list[Path],
    *,
    session_id: str,
    seed: int,
    max_files: int,
    max_bytes: int,
    skip_extensions: set[str],
    baseline: dict[str, str] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Apply the reversible transform. Returns the manifest."""

    def notify(action: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(action, payload)

    backup_root = paths.backup_root / session_id
    backup_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    total_bytes = 0

    for source in files:
        if len(entries) >= max_files:
            skipped.append({"path": str(source), "reason": "max_files"})
            continue
        try:
            target = paths.guard_victim(source)
        except PathViolation as exc:
            skipped.append({"path": str(source), "reason": str(exc)})
            continue
        if target.is_symlink() or not target.is_file():
            skipped.append({"path": str(target), "reason": "not-regular-file"})
            continue
        if target.suffix.lower() in skip_extensions:
            skipped.append({"path": str(target), "reason": "skipped-extension"})
            continue
        size = target.stat().st_size
        if size > max_bytes:
            skipped.append({"path": str(target), "reason": "max_bytes", "size": size})
            continue

        relative = str(target.relative_to(paths.root))
        original = target.read_bytes()
        original_sha = hashlib.sha256(original).hexdigest()
        mode = target.stat().st_mode & 0o777

        backup = backup_root / target.relative_to(paths.victim)
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)
        if sha256_file(backup) != original_sha:
            raise IntegrityError(f"backup verification failed for {relative}")

        key = derive_key(seed, relative)
        stored = xor_bytes(original, key)
        stored_sha = hashlib.sha256(stored).hexdigest()
        write_atomic(target, stored, mode)

        entries.append(
            {
                "path": relative,
                "backup": str(backup.relative_to(paths.root)),
                "size": size,
                "mode": mode,
                "orig_sha256": original_sha,
                "sha256": stored_sha,
                "baseline_sha256": (baseline or {}).get(relative),
                "key_hex": key.hex(),
            }
        )
        total_bytes += size
        notify(
            "transformed",
            {
                "path": relative,
                "size": size,
                "orig_sha256": original_sha,
                "sha256": stored_sha,
                "reversible": True,
            },
        )

    manifest = {
        "algorithm": "xor-sha256-ctr",
        "session_id": session_id,
        "seed": seed,
        "root": str(paths.victim.relative_to(paths.root)),
        "created_at": time.time(),
        "files": entries,
        "skipped": skipped,
        "total_bytes": total_bytes,
        "note": "keys and backups are retained solely to make the demo reversible",
    }
    notify(
        "transform.summary",
        {
            "files": len(entries),
            "skipped": len(skipped),
            "bytes": total_bytes,
            "algorithm": "xor-sha256-ctr",
        },
    )
    return manifest


def restore_transform(
    paths: LabPaths,
    manifest: dict[str, Any],
    *,
    verify: bool = True,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    def notify(action: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(action, payload)

    restored = 0
    already_restored = 0
    errors: list[dict[str, Any]] = []
    for entry in manifest.get("files", []):
        relative = entry["path"]
        try:
            target = paths.guard_victim(paths.resolve_relative(relative))
            backup = paths.resolve_relative(entry["backup"])
            current = target.read_bytes()
            current_sha = hashlib.sha256(current).hexdigest()
            if verify and current_sha in {entry["orig_sha256"], entry.get("baseline_sha256")}:
                already_restored += 1
                if backup.exists():
                    backup.unlink()
                notify("restore.already_restored", {"path": relative})
                continue
            if verify and current_sha != entry["sha256"]:
                raise IntegrityError(f"transformed file was modified after encryption: {relative}")
            original = xor_bytes(current, bytes.fromhex(entry["key_hex"]))
            if verify and hashlib.sha256(original).hexdigest() != entry["orig_sha256"]:
                raise IntegrityError(f"decrypted content does not match original: {relative}")

            if backup.exists() and verify:
                if sha256_file(backup) != entry["orig_sha256"]:
                    raise IntegrityError(f"backup does not match recorded hash: {relative}")

            write_atomic(target, original, int(entry.get("mode", 0o644)))
            if verify and sha256_file(target) != entry["orig_sha256"]:
                raise IntegrityError(f"post-restore verification failed: {relative}")
            if backup.exists():
                backup.unlink()
            restored += 1
            notify(
                "restored",
                {"path": relative, "sha256": entry["orig_sha256"], "verified": verify},
            )
        except (OSError, KeyError, ValueError, IntegrityError) as exc:
            errors.append({"path": relative, "error": str(exc)})
            notify(
                "restore_failed",
                {"path": relative, "error": str(exc)},
            )

    summary = {
        "restored": restored,
        "already_restored": already_restored,
        "errors": errors,
        "requested": len(manifest.get("files", [])),
    }
    notify(
        "restore.summary",
        {
            "restored": restored,
            "already_restored": already_restored,
            "errors": len(errors),
            "requested": len(manifest.get("files", [])),
        },
    )
    return summary


def edit_files(
    paths: LabPaths,
    *,
    session_id: str,
    relatives: list[str],
    marker: str,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Append a marker to selected victim files, with backups for restore."""

    def notify(action: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(action, payload)

    backup_root = paths.backup_root / session_id / "edits"
    backup_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for relative in relatives:
        target = paths.guard_victim(paths.victim / relative)
        if not target.is_file() or target.is_symlink():
            notify("edit_skipped", {"path": relative, "reason": "not-regular-file"})
            continue
        original = target.read_bytes()
        original_sha = hashlib.sha256(original).hexdigest()
        mode = target.stat().st_mode & 0o777
        backup = backup_root / target.relative_to(paths.victim)
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(original)
        mode_suffix = b"" if original.endswith(b"\n") else b"\n"
        updated = original + mode_suffix + marker.encode("utf-8") + b"\n"
        write_atomic(target, updated, mode)
        entry = {
            "path": str(target.relative_to(paths.root)),
            "backup": str(backup.relative_to(paths.root)),
            "orig_sha256": original_sha,
            "sha256": hashlib.sha256(updated).hexdigest(),
            "marker": marker,
        }
        entries.append(entry)
        notify("edited", {"path": entry["path"], "marker": marker, "reversible": True})

    return {
        "session_id": session_id,
        "created_at": time.time(),
        "files": entries,
        "note": "original bytes are backed up and restored by the cleanup engine",
    }


def restore_edits(
    paths: LabPaths,
    manifest: dict[str, Any],
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    def notify(action: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(action, payload)

    restored = 0
    already_restored = 0
    errors: list[dict[str, Any]] = []
    for entry in manifest.get("files", []):
        relative = entry["path"]
        try:
            backup = paths.resolve_relative(entry["backup"])
            target = paths.guard_victim(paths.resolve_relative(relative))
            if target.exists() and sha256_file(target) == entry["orig_sha256"]:
                already_restored += 1
                if backup.exists():
                    backup.unlink()
                notify("edit.already_restored", {"path": relative})
                continue
            if not backup.exists():
                raise IntegrityError(f"edit backup is missing: {relative}")
            if sha256_file(backup) != entry["orig_sha256"]:
                raise IntegrityError(f"edit backup hash mismatch: {relative}")
            write_atomic(target, backup.read_bytes(), target.stat().st_mode & 0o777)
            if sha256_file(target) != entry["orig_sha256"]:
                raise IntegrityError(f"post-restore verification failed: {relative}")
            backup.unlink()
            restored += 1
            notify("edit_restored", {"path": relative})
        except (OSError, KeyError, ValueError, IntegrityError) as exc:
            errors.append({"path": relative, "error": str(exc)})
            notify("edit_restore_failed", {"path": relative, "error": str(exc)})
    return {
        "restored": restored,
        "already_restored": already_restored,
        "errors": errors,
        "requested": len(manifest.get("files", [])),
    }


class FileOpsModule(SimModule):
    name = "fileops"
    description = "Recursive discovery, metadata, reversible edits and transforms"

    async def run(self, ctx) -> None:  # type: ignore[no-untyped-def]
        config = ctx.section("fileops")
        discovery_cfg = dict(config.get("discovery", {}))
        root = ctx.paths.victim
        if not root.exists():
            raise FileNotFoundError(f"victim tree is missing: {root}; run 'aptsim init' first")

        files = iter_files(root, max_files=int(discovery_cfg.get("max_files", 500)))
        await ctx.emit(
            "file",
            "discovery.begin",
            self.name,
            data={"root": str(root.relative_to(ctx.paths.root)), "candidates": len(files)},
        )

        metas = self._discover(ctx, files, discovery_cfg)
        baseline = {
            "session_id": ctx.session.session_id,
            "root": str(root.relative_to(ctx.paths.root)),
            "files": metas,
            "created_at": time.time(),
        }
        ctx.write_json("file-baseline.json", baseline)
        ctx.services["file_inventory"] = metas
        total_bytes = sum(meta["size"] for meta in metas)
        await ctx.emit(
            "file",
            "discovery.completed",
            self.name,
            data={"files": len(metas), "bytes": total_bytes},
        )

        if config.get("edit", {}).get("enabled"):
            edits = edit_files(
                ctx.paths,
                session_id=ctx.session.session_id,
                relatives=[str(item) for item in config["edit"].get("files", [])],
                marker=str(config["edit"].get("marker", "<!-- aptsim-simulated-tamper -->")),
                on_event=lambda action, payload: ctx.events.emit_threadsafe(
                    "file", action, self.name, data=payload
                ),
            )
            ctx.write_json("edits-manifest.json", edits)
            for entry in edits["files"]:
                ctx.journal.record(
                    ctx.paths.resolve_relative(entry["backup"]),
                    "edit-backup",
                    sha256=entry["orig_sha256"],
                )
            ctx.journal.record(
                ctx.session.directory / "edits-manifest.json", "manifest", meta={"kind": "edits"}
            )

        transform_cfg = dict(config.get("transform", {}))
        if transform_cfg.get("enabled"):
            if not ctx.config.get("safety.allow_transform", False):
                await ctx.emit(
                    "file",
                    "transform.blocked",
                    self.name,
                    severity="warning",
                    data={"reason": "safety.allow_transform is false"},
                )
            else:
                manifest = await asyncio.to_thread(
                    transform_files,
                    ctx.paths,
                    files,
                    session_id=ctx.session.session_id,
                    seed=int(ctx.get("runtime.seed", 0)),
                    max_files=int(transform_cfg.get("max_files", 50)),
                    max_bytes=int(transform_cfg.get("max_bytes", 524288)),
                    skip_extensions=set(transform_cfg.get("skip_extensions", [])),
                    baseline={meta["path"]: meta["sha256"] for meta in metas if meta.get("sha256")},
                    on_event=lambda action, payload: ctx.events.emit_threadsafe(
                        "file", action, self.name, data=payload
                    ),
                )
                ctx.write_json("transform-manifest.json", manifest)
                for entry in manifest["files"]:
                    ctx.journal.record(
                        ctx.paths.resolve_relative(entry["backup"]),
                        "transform-backup",
                        sha256=entry["orig_sha256"],
                    )
                ctx.journal.record(
                    ctx.session.directory / "transform-manifest.json",
                    "manifest",
                    meta={"kind": "transform"},
                )
                ctx.services["transform_manifest"] = manifest

        await ctx.emit(
            "file",
            "operations.completed",
            self.name,
            data={
                "discovered": len(metas),
                "edited": len(ctx.read_json("edits-manifest.json")["files"])
                if ctx.exists("edits-manifest.json")
                else 0,
                "transformed": len(ctx.services.get("transform_manifest", {}).get("files", [])),
            },
        )

    def _discover(self, ctx, files: list[Path], config: dict[str, Any]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
        compute_hash = bool(config.get("hash", True))
        compute_entropy = bool(config.get("entropy", True))
        threads = max(1, int(config.get("threads", 4)))
        results: list[dict[str, Any]] = []

        def worker(path: Path) -> dict[str, Any]:
            meta = collect_metadata(path)
            record = meta.to_dict()
            record["path"] = str(path.relative_to(ctx.paths.root))
            data = path.read_bytes() if (compute_hash or compute_entropy) else b""
            if compute_hash:
                record["sha256"] = hashlib.sha256(data).hexdigest()
            if compute_entropy:
                record["entropy"] = shannon_entropy(data)
            ctx.events.emit_threadsafe(
                "file",
                "metadata.collected",
                self.name,
                severity="debug",
                data={
                    "path": record["path"],
                    "size": record["size"],
                    "sha256": record["sha256"],
                    "entropy": record["entropy"],
                },
            )
            return record

        with ThreadPoolExecutor(max_workers=threads) as pool:
            for record in pool.map(worker, files):
                results.append(record)
        return results
