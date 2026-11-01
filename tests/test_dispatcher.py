from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_context, make_lab

from aptsim.modules.dispatcher import Dispatcher


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config, paths = make_lab(Path(self.tmp.name))
        self.config = config
        self.paths = paths
        self.ctx, self.bus, self.memory = await make_context(config, paths)
        self.dispatcher = Dispatcher.from_config(self.ctx)

    async def asyncTearDown(self) -> None:
        await self.bus.close()

    async def test_noop_and_status(self) -> None:
        result = await self.dispatcher.handle({"task_id": "t1", "type": "noop", "args": {}})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["result"]["inert"])

        status = await self.dispatcher.handle({"task_id": "t2", "type": "report_status", "args": {}})
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["result"]["mode"], "simulation-only")

    async def test_list_and_hash_files(self) -> None:
        listing = await self.dispatcher.handle(
            {"task_id": "t3", "type": "list_files", "args": {"path": "victim"}}
        )
        self.assertEqual(listing["status"], "ok")
        self.assertGreater(listing["result"]["count"], 0)

        hashed = await self.dispatcher.handle(
            {"task_id": "t4", "type": "hash_files", "args": {"path": "victim", "limit": 2}}
        )
        self.assertEqual(hashed["status"], "ok")
        self.assertEqual(len(hashed["result"]["files"]), 2)
        self.assertEqual(len(hashed["result"]["files"][0]["sha256"]), 64)

    async def test_unknown_and_dangerous_types_rejected(self) -> None:
        for task_type in ("shell", "exec", "eval", "write_file"):
            with self.subTest(task_type=task_type):
                result = await self.dispatcher.handle(
                    {"task_id": "bad", "type": task_type, "args": {}}
                )
                self.assertEqual(result["status"], "rejected")
                self.assertIn("allowlist", result["error"])

    async def test_traversal_and_absolute_paths_rejected(self) -> None:
        for path in ("../../etc", "/etc/passwd", "victim/../../etc", "documents/notes.txt"):
            with self.subTest(path=path):
                result = await self.dispatcher.handle(
                    {"task_id": "t", "type": "list_files", "args": {"path": path}}
                )
                self.assertEqual(result["status"], "rejected", result)

    async def test_limit_validation(self) -> None:
        for limit in (0, 51, "5", True):
            with self.subTest(limit=limit):
                result = await self.dispatcher.handle(
                    {"task_id": "t", "type": "hash_files", "args": {"path": "victim", "limit": limit}}
                )
                self.assertEqual(result["status"], "rejected")

    async def test_timeout_and_error_handling(self) -> None:
        self.ctx.config.data["runtime"]["time_scale"] = 1.0
        fast = Dispatcher(
            ctx=self.ctx,
            allowed_types=["sleep", "noop"],
            allowed_roots=["victim"],
            timeout=0.05,
        )
        result = await fast.handle({"task_id": "slow", "type": "sleep", "args": {"seconds": 2}})
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(fast.errors, 1)

    async def test_malformed_tasks_rejected(self) -> None:
        for task in (None, {}, {"task_id": ""}, {"task_id": "x", "type": "noop", "args": []}):
            with self.subTest(task=task):
                result = await self.dispatcher.handle(task)  # type: ignore[arg-type]
                self.assertEqual(result["status"], "rejected")

    async def test_counters(self) -> None:
        await self.dispatcher.handle({"task_id": "a", "type": "noop", "args": {}})
        await self.dispatcher.handle({"task_id": "b", "type": "shell", "args": {}})
        self.assertEqual(self.dispatcher.handled, 1)
        self.assertEqual(self.dispatcher.rejected, 1)


if __name__ == "__main__":
    unittest.main()
