"""Layered JSON configuration manager with safety interlocks.

Load order (later wins):
    config/default.json  ->  config/profiles/<profile>.json  ->  CLI overrides

Values support ``${VAR}`` and ``${VAR:-default}`` expansion. The resulting
configuration is validated against the framework's safety contract before any
module is imported or any file is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ConfigError

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
ALLOWED_TRANSFORM_ALGORITHMS = {"xor-sha256-ctr"}
ALLOWED_PERSISTENCE_KINDS = {"xdg-autostart", "cron", "systemd-user", "launch-agent-sim"}
ALLOWED_ERROR_POLICIES = {"abort", "continue", "retry"}
ALLOWED_ENFORCEMENT = {"log_only"}


def _expand(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            return os.environ.get(name, default if default is not None else "")

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _parse_override(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise ConfigError(f"override must look like key.path=value, got: {raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise ConfigError(f"override has an empty key: {raw}")
    text = value.strip()
    for parser in (json.loads,):
        try:
            parsed = parser(text)
        except (ValueError, TypeError):
            continue
        else:
            return key, parsed
    return key, text


def _set_dotted(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    cursor = data
    for part in parts[:-1]:
        node = cursor.setdefault(part, {})
        if not isinstance(node, dict):
            raise ConfigError(f"cannot override {key!r}: {part!r} is not an object")
        cursor = node
    cursor[parts[-1]] = value


class Config:
    """Immutable-by-convention configuration snapshot."""

    def __init__(self, data: dict[str, Any], sources: Iterable[str] = ()) -> None:
        self._data = data
        self.sources = list(sources)

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigError(f"missing required configuration key: {dotted}")
        return value

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"configuration section {name!r} must be an object")
        return value

    def with_overrides(self, overrides: Iterable[str]) -> "Config":
        data = json.loads(json.dumps(self._data))
        for raw in overrides:
            key, value = _parse_override(raw)
            _set_dotted(data, key, value)
        validate(data)
        return Config(data, [*self.sources, "cli-overrides"])

    def snapshot(self) -> str:
        return json.dumps(self._data, indent=2, sort_keys=True)

    def digest(self) -> str:
        return hashlib.sha256(self.snapshot().encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Config(digest={self.digest()[:12]}, sources={self.sources})"


class ConfigManager:
    def __init__(self, config_dir: str | Path) -> None:
        self.config_dir = Path(config_dir).expanduser().resolve()

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(f"configuration file not found: {path}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"configuration root must be an object: {path}")
        return data

    def load(
        self,
        profile: str | Path | None = None,
        overrides: Iterable[str] = (),
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> Config:
        default_path = self.config_dir / "default.json"
        data = _expand(self._read(default_path))
        sources = [str(default_path)]

        if profile:
            profile_path = Path(profile)
            if not profile_path.exists():
                profile_path = self.config_dir / "profiles" / f"{profile}.json"
            if not profile_path.exists():
                raise ConfigError(f"unknown profile: {profile}")
            data = _deep_merge(data, _expand(self._read(profile_path)))
            sources.append(str(profile_path))

        if extra:
            data = _deep_merge(data, _expand(dict(extra)))
            sources.append("embedded")

        for raw in overrides:
            key, value = _parse_override(raw)
            _set_dotted(data, key, value)
        if overrides:
            sources.append("cli-overrides")

        validate(data)
        return Config(data, sources)


def validate(data: Mapping[str, Any]) -> None:
    """Validate the safety contract and structural invariants."""

    def fail(message: str) -> None:
        raise ConfigError(message)

    schema_version = data.get("meta", {}).get("schema_version")
    if schema_version != 1:
        fail(f"unsupported meta.schema_version: {schema_version!r} (expected 1)")

    safety = data.get("safety")
    if not isinstance(safety, dict):
        fail("missing safety section")
    if safety.get("locked") is not True:
        fail("safety.locked must be true; the lab framework refuses to run unlocked")
    if safety.get("allow_outside_lab") is not False:
        fail("safety.allow_outside_lab must be false; out-of-lab writes are unsupported")
    if safety.get("allow_subprocess") is not False:
        fail("safety.allow_subprocess must be false; the framework never spawns processes")
    if safety.get("allow_network_non_loopback") is not False:
        fail("safety.allow_network_non_loopback must be false; C2 is loopback only")
    if safety.get("allow_host_persistence") is not False:
        fail("safety.allow_host_persistence must be false; persistence is simulated only")

    enforcement = data.get("anti_analysis", {}).get("enforcement")
    if enforcement not in ALLOWED_ENFORCEMENT:
        fail("anti_analysis.enforcement must be 'log_only'; behaviour-changing evasion is unsupported")

    host = str(data.get("network", {}).get("host", ""))
    if host not in LOOPBACK_HOSTS:
        fail(f"network.host must be loopback ({sorted(LOOPBACK_HOSTS)}), got {host!r}")
    try:
        socket.inet_pton(socket.AF_INET, host)
    except OSError:
        if host not in {"localhost", "::1"}:
            fail(f"network.host is not a valid loopback address: {host!r}")

    algorithm = data.get("fileops", {}).get("transform", {}).get("algorithm")
    if algorithm not in ALLOWED_TRANSFORM_ALGORITHMS:
        fail(f"unsupported transform algorithm: {algorithm!r}")
    if data.get("fileops", {}).get("transform", {}).get("enabled") and not safety.get(
        "allow_transform", False
    ):
        fail("transform is enabled but safety.allow_transform is false")

    stages = data.get("pipeline", {}).get("stages")
    if not isinstance(stages, list) or not stages:
        fail("pipeline.stages must be a non-empty list")
    seen: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            fail(f"pipeline.stages[{index}] must be an object")
        stage_id = stage.get("id")
        module = stage.get("module")
        if not stage_id or not isinstance(stage_id, str):
            fail(f"pipeline.stages[{index}] is missing a string id")
        if stage_id in seen:
            fail(f"duplicate pipeline stage id: {stage_id}")
        seen.add(stage_id)
        if not module:
            fail(f"pipeline stage {stage_id} is missing a module")
        policy = stage.get("on_error", "abort")
        if policy not in ALLOWED_ERROR_POLICIES:
            fail(f"pipeline stage {stage_id} has invalid on_error={policy!r}")

    for mechanism in data.get("persistence", {}).get("mechanisms", []):
        kind = mechanism.get("kind")
        if kind not in ALLOWED_PERSISTENCE_KINDS:
            fail(f"unsupported simulated persistence kind: {kind!r}")
        path = str(mechanism.get("path", ""))
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            fail(f"persistence mechanism {mechanism.get('id')!r} path must be lab-relative under persistence/: {path!r}")

    dispatcher = data.get("dispatcher", {})
    allowed = dispatcher.get("allowed_task_types")
    if not isinstance(allowed, list) or not allowed:
        fail("dispatcher.allowed_task_types must be a non-empty list")
    for task_type in allowed:
        if task_type in {"shell", "exec", "system", "eval"}:
            fail(f"task type {task_type!r} is permanently unsupported; the dispatcher is inert")

    runtime = data.get("runtime", {})
    if float(runtime.get("time_scale", 1.0)) < 0:
        fail("runtime.time_scale must be >= 0")

    lab = data.get("lab", {})
    if not lab.get("root"):
        fail("lab.root must be set")
    if Path(str(lab.get("root"))).is_absolute():
        from .errors import PathViolation
        from .paths import validate_lab_root

        try:
            validate_lab_root(Path(str(lab["root"])))
        except PathViolation as exc:
            fail(str(exc))
