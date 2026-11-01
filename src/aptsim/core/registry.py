"""Module registry and dynamic plugin loading.

Built-in modules are ordinary Python classes. Plugins are local files loaded at
runtime from ``<lab>/plugins``; the loader enforces the directory boundary, a
size limit, the plugin API version and the module contract, and records the
SHA-256 of the plugin file before importing it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import PluginError
from .paths import LabPaths

if TYPE_CHECKING:  # pragma: no cover
    from aptsim.modules.base import SimModule

PLUGIN_API = 1
MAX_PLUGIN_BYTES = 256 * 1024


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, type["SimModule"]] = {}

    def register(self, name: str, module: type["SimModule"]) -> None:
        self._modules[name] = module

    def names(self) -> list[str]:
        return sorted(self._modules)

    def get(self, specification: str, *, paths: LabPaths | None = None) -> type["SimModule"]:
        if specification.startswith("plugin:"):
            if paths is None:
                raise PluginError("plugin resolution requires lab paths")
            filename = specification.split(":", 1)[1]
            return self.load_plugin(paths.plugins / filename, paths)
        if specification not in self._modules:
            raise PluginError(
                f"unknown module {specification!r}; available: {', '.join(self.names())}"
            )
        return self._modules[specification]

    def load_plugin(self, path: str | Path, paths: LabPaths) -> type["SimModule"]:
        from aptsim.modules.base import SimModule

        resolved = paths.guard(path, what="plugin")
        if not resolved.is_relative_to(paths.plugins):
            raise PluginError(f"plugins must live under {paths.plugins}: {resolved}")
        if resolved.suffix != ".py":
            raise PluginError(f"plugin must be a .py file: {resolved}")
        if not resolved.is_file():
            raise PluginError(f"plugin not found: {resolved}")
        size = resolved.stat().st_size
        if size > MAX_PLUGIN_BYTES:
            raise PluginError(f"plugin too large ({size} bytes > {MAX_PLUGIN_BYTES})")

        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        module_name = f"aptsim_plugin_{digest[:12]}"
        if module_name in sys.modules:
            module = sys.modules[module_name]
        else:
            spec = importlib.util.spec_from_file_location(module_name, resolved)
            if spec is None or spec.loader is None:
                raise PluginError(f"cannot load plugin: {resolved}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as exc:  # pragma: no cover - defensive
                sys.modules.pop(module_name, None)
                raise PluginError(f"plugin import failed: {exc}") from exc

        api = getattr(module, "PLUGIN_API", None)
        if api != PLUGIN_API:
            raise PluginError(f"plugin API mismatch: {api!r} != {PLUGIN_API}")
        candidate = getattr(module, "MODULE", None)
        if not inspect.isclass(candidate) or not issubclass(candidate, SimModule):
            raise PluginError(f"plugin {resolved.name} must expose MODULE as a SimModule subclass")
        candidate.plugin_sha256 = digest
        candidate.plugin_path = str(resolved)
        return candidate

    @classmethod
    def default(cls) -> "ModuleRegistry":
        from aptsim.modules import (
            anti_analysis,
            fileops,
            network,
            payload,
            persistence,
            process,
        )

        registry = cls()
        registry.register("payload", payload.PayloadModule)
        registry.register("process", process.ProcessModule)
        registry.register("fileops", fileops.FileOpsModule)
        registry.register("persistence", persistence.PersistenceModule)
        registry.register("anti_analysis", anti_analysis.AntiAnalysisModule)
        registry.register("network", network.NetworkModule)
        return registry
