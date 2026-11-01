from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_lab

from aptsim.core.artifacts import ArtifactJournal, sha256_file
from aptsim.core.session import SessionManager
from aptsim.modules.fileops import edit_files, iter_files, transform_files
from aptsim.recovery.cleanup import CleanupEngine


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.config, self.paths = make_lab(self.tmp_path)
        self.session = SessionManager(self.paths).create(metadata={"test": "cleanup"})
        self.journal = ArtifactJournal(self.session.artifacts_path, self.paths, self.session.session_id)

    def _mutate(self) -> tuple[dict[str, bytes], list[dict], list[dict]]:
        files = iter_files(self.paths.victim)
        originals = {path: path.read_bytes() for path in files}
        baseline = {
            str(path.relative_to(self.paths.root)): sha256_file(path) for path in files
        }
        edits = edit_files(
            self.paths,
            session_id=self.session.session_id,
            relatives=["documents/notes.txt"],
            marker="<!-- cleanup-test -->",
        )
        self.session.write_json("edits-manifest.json", edits)
        manifest = transform_files(
            self.paths,
            files,
            session_id=self.session.session_id,
            seed=3,
            max_files=50,
            max_bytes=1 << 20,
            skip_extensions={".bin"},
            baseline=baseline,
        )
        self.session.write_json("transform-manifest.json", manifest)
        for entry in manifest["files"]:
            self.journal.record(
                self.paths.resolve_relative(entry["backup"]),
                "transform-backup",
                sha256=entry["orig_sha256"],
            )
        return originals, edits, manifest

    def _persistence_artifact(self) -> Path:
        target = self.paths.guard_persistence("persistence/cron.d/aptsim-demo")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# simulated\n", encoding="utf-8")
        self.journal.record(target, "persistence")
        return target

    def test_cleanup_restores_and_removes(self) -> None:
        originals, _, manifest = self._mutate()
        artifact = self._persistence_artifact()
        events = []
        engine = CleanupEngine(
            self.paths,
            self.journal,
            self.session.directory,
            on_event=lambda category, action, data: events.append((category, action, data)),
        )
        report = engine.run()
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["transform"]["restored"], len(manifest["files"]))
        self.assertEqual(report["edits"]["restored"], 1)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content, f"not restored: {path}")
        self.assertFalse(artifact.exists())
        self.assertTrue((self.session.directory / "cleanup-report.json").exists())
        self.assertTrue(any(action == "completed" for _, action, _ in events))

    def test_cleanup_is_idempotent(self) -> None:
        self._mutate()
        engine = CleanupEngine(self.paths, self.journal, self.session.directory)
        first = engine.run()
        second = engine.run()
        self.assertTrue(first["ok"], first["errors"])
        self.assertTrue(second["ok"], second["errors"])
        self.assertEqual(second["transform"]["restored"], 0)

    def test_dry_run_changes_nothing(self) -> None:
        files = iter_files(self.paths.victim)[:2]
        manifest = transform_files(
            self.paths,
            files,
            session_id=self.session.session_id,
            seed=5,
            max_files=2,
            max_bytes=1 << 20,
            skip_extensions=set(),
        )
        artifact = self._persistence_artifact()
        before = {entry["path"]: sha256_file(self.paths.resolve_relative(entry["path"])) for entry in manifest["files"]}
        report = CleanupEngine(
            self.paths, self.journal, self.session.directory, dry_run=True
        ).run()
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["ok"])
        self.assertIn("plan", report)
        self.assertTrue(artifact.exists())
        for path, digest in before.items():
            self.assertEqual(sha256_file(self.paths.resolve_relative(path)), digest)

    def test_integrity_check_flags_missing_baseline_file(self) -> None:
        self._mutate()
        self.session.write_json(
            "file-baseline.json",
            {"files": [{"path": "victim/documents/notes.txt", "sha256": "0" * 64}]},
        )
        report = CleanupEngine(self.paths, self.journal, self.session.directory).run()
        self.assertFalse(report["integrity"]["ok"])
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
