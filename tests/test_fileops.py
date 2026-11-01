from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_lab

from aptsim.core.errors import IntegrityError, PathViolation
from aptsim.modules.fileops import (
    derive_key,
    edit_files,
    iter_files,
    keystream,
    restore_edits,
    restore_transform,
    shannon_entropy,
    transform_files,
    xor_bytes,
)


class FileOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        _, self.paths = make_lab(self.tmp_path)
        self.files = iter_files(self.paths.victim)

    def test_discovery_is_recursive_and_deterministic(self) -> None:
        self.assertEqual(len(self.files), 7)
        self.assertEqual(self.files, iter_files(self.paths.victim))
        self.assertTrue(any(path.suffix == ".csv" for path in self.files))
        self.assertTrue(any(path.name == "data.bin" for path in self.files))

    def test_entropy(self) -> None:
        self.assertEqual(shannon_entropy(b""), 0.0)
        self.assertEqual(shannon_entropy(b"aaaa"), 0.0)
        self.assertAlmostEqual(shannon_entropy(bytes(range(256))), 8.0, places=3)

    def test_keystream_and_xor_roundtrip(self) -> None:
        key = derive_key(42, "victim/a.txt")
        stream = keystream(key, 1000)
        self.assertEqual(len(stream), 1000)
        self.assertNotEqual(stream, keystream(derive_key(43, "victim/a.txt"), 1000))
        data = b"detection engineering"
        self.assertEqual(xor_bytes(xor_bytes(data, key), key), data)

    def test_transform_and_restore_roundtrip(self) -> None:
        originals = {path: path.read_bytes() for path in self.files}
        manifest = transform_files(
            self.paths,
            self.files,
            session_id="s-unit",
            seed=7,
            max_files=50,
            max_bytes=1 << 20,
            skip_extensions={".bin"},
        )
        self.assertEqual(len(manifest["files"]), 6)
        self.assertEqual(manifest["algorithm"], "xor-sha256-ctr")
        for entry in manifest["files"]:
            target = self.paths.resolve_relative(entry["path"])
            self.assertNotEqual(target.read_bytes(), originals[target])
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), entry["sha256"])
            backup = self.paths.resolve_relative(entry["backup"])
            self.assertTrue(backup.exists())

        result = restore_transform(self.paths, manifest, verify=True)
        self.assertEqual(result["restored"], 6)
        self.assertEqual(result["errors"], [])
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_restore_detects_tampering_and_fails_closed(self) -> None:
        files = [path for path in self.files if path.suffix == ".txt"][:1]
        manifest = transform_files(
            self.paths,
            files,
            session_id="s-tamper",
            seed=1,
            max_files=10,
            max_bytes=1 << 20,
            skip_extensions=set(),
        )
        entry = manifest["files"][0]
        target = self.paths.resolve_relative(entry["path"])
        tampered = target.read_bytes() + b"tamper"
        target.write_bytes(tampered)
        result = restore_transform(self.paths, manifest, verify=True)
        self.assertEqual(result["restored"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(target.read_bytes(), tampered)

    def test_restore_without_backup_still_verifies_plaintext(self) -> None:
        files = self.files[:1]
        manifest = transform_files(
            self.paths, files, session_id="s-missing", seed=1, max_files=1, max_bytes=1 << 20, skip_extensions=set()
        )
        entry = manifest["files"][0]
        backup = self.paths.resolve_relative(entry["backup"])
        backup.unlink()
        result = restore_transform(self.paths, manifest, verify=True)
        self.assertEqual(result["restored"], 1)
        target = self.paths.resolve_relative(entry["path"])
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), entry["orig_sha256"])

    def test_edit_and_restore(self) -> None:
        target = self.paths.victim / "documents" / "notes.txt"
        original = target.read_bytes()
        manifest = edit_files(
            self.paths,
            session_id="s-edit",
            relatives=["documents/notes.txt"],
            marker="<!-- test-marker -->",
        )
        self.assertEqual(len(manifest["files"]), 1)
        self.assertIn(b"test-marker", target.read_bytes())
        result = restore_edits(self.paths, manifest)
        self.assertEqual(result["restored"], 1)
        self.assertEqual(target.read_bytes(), original)

    def test_edit_rejects_traversal(self) -> None:
        with self.assertRaises(PathViolation):
            edit_files(
                self.paths,
                session_id="s-edit",
                relatives=["../../etc/passwd"],
                marker="x",
            )

    def test_transform_skips_bin_and_respects_limits(self) -> None:
        manifest = transform_files(
            self.paths,
            self.files,
            session_id="s-limit",
            seed=1,
            max_files=2,
            max_bytes=1000,
            skip_extensions={".bin"},
        )
        self.assertEqual(len(manifest["files"]), 2)
        reasons = {item["reason"] for item in manifest["skipped"]}
        self.assertIn("max_files", reasons)
        self.assertIn("skipped-extension", reasons)

    def test_integrity_error_type(self) -> None:
        with self.assertRaises(IntegrityError):
            raise IntegrityError("unit")


if __name__ == "__main__":
    unittest.main()
