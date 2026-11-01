from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_lab, make_paths, make_config

from aptsim.core.errors import PathViolation
from aptsim.core.paths import LabPaths, validate_lab_root


class PathGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        _, self.paths = make_lab(self.tmp_path)

    def test_lab_root_validation(self) -> None:
        for bad in ("/", "/etc", "/usr", "/home", str(Path.home())):
            with self.subTest(root=bad):
                with self.assertRaises(PathViolation):
                    validate_lab_root(Path(bad))

    def test_traversal_rejected(self) -> None:
        for relative in ("../escape.txt", "victim/../../escape.txt", "/etc/passwd"):
            with self.subTest(relative=relative):
                with self.assertRaises(PathViolation):
                    self.paths.resolve_relative(relative)

    def test_guard_outside_lab_rejected(self) -> None:
        with self.assertRaises(PathViolation):
            self.paths.guard(self.tmp_path / "outside.txt")

    def test_symlink_escape_rejected(self) -> None:
        outside = self.tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.paths.victim / "escape-link"
        link.symlink_to(outside)
        with self.assertRaises(PathViolation):
            self.paths.guard(link)

    def test_persistence_target_confined(self) -> None:
        with self.assertRaises(PathViolation):
            self.paths.guard_persistence("victim/documents/notes.txt")
        with self.assertRaises(PathViolation):
            self.paths.guard_persistence("/etc/cron.d/aptsim")
        with self.assertRaises(PathViolation):
            self.paths.guard_persistence("persistence/../../etc/cron.d/aptsim")
        allowed = self.paths.guard_persistence("persistence/autostart/demo.desktop")
        self.assertTrue(allowed.is_relative_to(self.paths.persistence))

    def test_host_target_blocked(self) -> None:
        for target in ("/etc/cron.d/x", "/etc/systemd/system/x.service", "/var/spool/cron/root"):
            with self.subTest(target=target):
                with self.assertRaises(PathViolation):
                    self.paths.assert_not_host_target(target)

    def test_session_dir_confined(self) -> None:
        with self.assertRaises(PathViolation):
            self.paths.session_dir("../../etc")

    def test_build_rejects_bad_layout(self) -> None:
        config = make_config(self.tmp_path)
        paths = make_paths(config)
        self.assertEqual(paths.victim, paths.root / "victim")


if __name__ == "__main__":
    unittest.main()
