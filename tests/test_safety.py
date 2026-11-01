from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import REPO, SRC, make_config

from aptsim.core.errors import ConfigError, PluginError
from aptsim.core.registry import ModuleRegistry
from aptsim.modules.anti_analysis import AntiAnalysisModule, evaluate
from aptsim.modules.base import SimModule
from aptsim.modules.persistence import render_mechanism

BANNED_PATTERNS = {
    "subprocess import": re.compile(r"\b(import|from)\s+subprocess\b"),
    "subprocess call": re.compile(r"\bsubprocess\.(Popen|run|call|check_output|check_call)\b"),
    "os.system": re.compile(r"\bos\.system\s*\("),
    "os.popen": re.compile(r"\bos\.popen\s*\("),
    "eval": re.compile(r"\beval\s*\("),
    "exec": re.compile(r"\bexec\s*\("),
    "recursive delete": re.compile(r"\bshutil\.rmtree\b"),
    "raw socket": re.compile(r"\bsocket\.socket\b"),
    "ssh library": re.compile(r"\b(paramiko|fabric)\b"),
    "http client": re.compile(r"\b(requests|httpx|urllib\.request)\b"),
    "crypto library": re.compile(r"\b(Crypto|Cryptodome|cryptography)\b"),
    "keylogger": re.compile(r"\b(pynput|keyboard\.Listener)\b"),
}


class SafetyTests(unittest.TestCase):
    def test_no_offensive_primitives_in_source(self) -> None:
        violations: list[str] = []
        for path in sorted((SRC / "aptsim").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for name, pattern in BANNED_PATTERNS.items():
                if pattern.search(text):
                    violations.append(f"{path.relative_to(REPO)}: {name}")
        self.assertEqual(violations, [], "banned primitives found: " + ", ".join(violations))

    def test_safety_contract_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
        self.assertTrue(config.get("safety.locked"))
        self.assertFalse(config.get("safety.allow_outside_lab"))
        self.assertFalse(config.get("safety.allow_subprocess"))
        self.assertFalse(config.get("safety.allow_network_non_loopback"))
        self.assertFalse(config.get("safety.allow_host_persistence"))
        self.assertEqual(config.get("anti_analysis.enforcement"), "log_only")
        self.assertEqual(config.get("network.host"), "127.0.0.1")

    def test_anti_analysis_refuses_enforcement_change(self) -> None:
        module = AntiAnalysisModule()
        fake_ctx = SimpleNamespace(
            section=lambda name: {"enforcement": "evade", "rules": [], "scenarios": []}
        )
        import asyncio

        with self.assertRaises(ConfigError):
            asyncio.run(module.run(fake_ctx))

    def test_anti_analysis_predicates(self) -> None:
        self.assertTrue(evaluate(2, "lte", 2))
        self.assertFalse(evaluate(4, "lte", 2))
        self.assertTrue(evaluate(4, "gte", 2))
        self.assertTrue(evaluate("sandbox-win10", "contains_any", ["sandbox", "vm"]))
        self.assertTrue(evaluate(["procmon"], "non_empty"))
        self.assertTrue(evaluate([], "empty"))
        with self.assertRaises(ConfigError):
            evaluate(1, "sql", 1)

    def test_persistence_artifacts_are_marked_simulated(self) -> None:
        for kind in ("xdg-autostart", "cron", "systemd-user", "launch-agent-sim"):
            content = render_mechanism(
                {"kind": kind, "payload": {"exec": "/opt/demo", "schedule": "* * * * *", "command": "/opt/demo"}}
            )
            self.assertIn("simulation", content.lower())
            self.assertNotIn("/etc/", content)

    def test_registry_contract(self) -> None:
        registry = ModuleRegistry.default()
        self.assertEqual(
            set(registry.names()),
            {"payload", "process", "fileops", "persistence", "anti_analysis", "network"},
        )
        for name in registry.names():
            self.assertTrue(issubclass(registry.get(name), SimModule))

    def test_plugin_loader_confines_to_plugins_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = make_config(tmp_path)
            paths = __import__("aptsim.core.paths", fromlist=["LabPaths"]).LabPaths.build(
                config.require("lab.root")
            )
            paths.ensure_layout()
            with self.assertRaises(PluginError):
                registry = ModuleRegistry.default()
                registry.load_plugin(paths.victim / "evil.py", paths)

    def test_no_real_credentials_in_lab_template(self) -> None:
        labgen = (SRC / "aptsim" / "tools" / "labgen.py").read_text(encoding="utf-8")
        self.assertIn("NOT VALID ANYWHERE", labgen)
        self.assertNotRegex(labgen, r"AKIA[0-9A-Z]{16}")


if __name__ == "__main__":
    unittest.main()
