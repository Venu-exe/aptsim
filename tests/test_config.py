from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import CONFIG_DIR, make_config

from aptsim.core.config import ConfigManager
from aptsim.core.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "lab"

    def load(self, profile: str = "ci", overrides: list[str] | None = None):
        base = [f"lab.root={json.dumps(str(self.root))}"]
        return ConfigManager(CONFIG_DIR).load(profile, base + list(overrides or []))

    def test_defaults_and_profile_merge(self) -> None:
        config = self.load()
        self.assertEqual(config.get("network.host"), "127.0.0.1")
        self.assertEqual(config.get("runtime.time_scale"), 0.0)
        self.assertTrue(config.get("recovery.auto_cleanup"))
        self.assertEqual(config.get("safety.allow_outside_lab"), False)
        self.assertEqual(len(config.sources), 3)

    def test_env_expansion(self) -> None:
        os.environ["APTSIM_TEST_VALUE"] = "expanded"
        self.addCleanup(os.environ.pop, "APTSIM_TEST_VALUE", None)
        config = ConfigManager(CONFIG_DIR).load(
            "ci",
            [f"lab.root={json.dumps(str(self.root))}"],
            extra={"meta": {"env_probe": "${APTSIM_TEST_VALUE}", "fallback": "${APTSIM_MISSING:-fallback}"}},
        )
        self.assertEqual(config.get("meta.env_probe"), "expanded")
        self.assertEqual(config.get("meta.fallback"), "fallback")

    def test_cli_override_parsing(self) -> None:
        config = self.load(overrides=['runtime.seed=7', "pipeline.stop_after=\"s3-process\""])
        self.assertEqual(config.get("runtime.seed"), 7)
        self.assertEqual(config.get("pipeline.stop_after"), "s3-process")

    def test_digest_is_stable(self) -> None:
        first = self.load()
        second = self.load()
        self.assertEqual(first.digest(), second.digest())
        third = self.load(overrides=["runtime.seed=1"])
        self.assertNotEqual(first.digest(), third.digest())

    def test_safety_locks_rejected(self) -> None:
        for override in (
            "safety.allow_outside_lab=true",
            "safety.allow_subprocess=true",
            "safety.allow_network_non_loopback=true",
            "safety.allow_host_persistence=true",
            "safety.locked=false",
        ):
            with self.subTest(override=override):
                with self.assertRaises(ConfigError):
                    self.load(overrides=[override])

    def test_enforcement_must_be_log_only(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(overrides=['anti_analysis.enforcement="block"'])

    def test_non_loopback_host_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(overrides=['network.host="10.0.0.5"'])

    def test_unknown_transform_algorithm_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(overrides=['fileops.transform.algorithm="aes-gcm"'])

    def test_absolute_persistence_path_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(
                overrides=[
                    'persistence.mechanisms=[{"id":"x","kind":"cron","path":"/etc/cron.d/x","payload":{}}]'
                ]
            )

    def test_duplicate_stage_ids_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(
                overrides=[
                    'pipeline.stages=[{"id":"a","module":"payload"},{"id":"a","module":"process"}]'
                ]
            )

    def test_shell_task_type_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(overrides=['dispatcher.allowed_task_types=["noop","shell"]'])

    def test_unknown_profile(self) -> None:
        with self.assertRaises(ConfigError):
            ConfigManager(CONFIG_DIR).load("does-not-exist", [f"lab.root={json.dumps(str(self.root))}"])

    def test_invalid_json_override(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(overrides=["runtime.seed"])


if __name__ == "__main__":
    unittest.main()
