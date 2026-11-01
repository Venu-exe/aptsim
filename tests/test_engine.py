from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_config, make_context, make_lab

from aptsim.core.artifacts import ArtifactJournal
from aptsim.core.context import ExecutionContext
from aptsim.core.engine import Engine
from aptsim.core.errors import SimulatedCrash
from aptsim.core.events import EventBus, MemorySink
from aptsim.core.registry import ModuleRegistry
from aptsim.core.session import SessionManager
from aptsim.core.state import StateStore
from aptsim.modules.base import SimModule
from aptsim.recovery.cleanup import CleanupEngine


class BoomModule(SimModule):
    name = "boom"

    async def run(self, ctx) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic module failure")


class PipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.config, self.paths = make_lab(self.tmp_path)
        self.ctx, self.bus, self.memory = await make_context(self.config, self.paths)

    async def asyncTearDown(self) -> None:
        if not self.bus._closed:
            await self.bus.close()

    async def test_full_pipeline(self) -> None:
        summary = await Engine(self.ctx).run()
        import asyncio

        await asyncio.sleep(0.1)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(len(summary["stages"]), 6)
        self.assertTrue(all(stage["status"] == "ok" for stage in summary["stages"]))

        actions = {event.action for event in self.memory.events}
        for expected in (
            "payload.ready",
            "payload.plugin.loaded",
            "assessment.completed",
            "spawn.simulated",
            "registration.simulated",
            "transform.summary",
            "session.registered",
            "task.completed",
            "run.completed",
        ):
            self.assertIn(expected, actions)

        self.assertTrue((self.ctx.session.directory / "transform-manifest.json").exists())
        self.assertTrue((self.ctx.session.directory / "c2-session.json").exists())
        self.assertTrue((self.ctx.session.directory / "summary.json").exists())
        self.assertTrue((self.ctx.session.directory / "processes.json").exists())

        task_events = [event for event in self.memory.events if event.action == "task.completed"]
        self.assertEqual(len(task_events), 6)
        statuses = [event.data["status"] for event in task_events]
        self.assertEqual(statuses.count("ok"), 4)
        self.assertEqual(statuses.count("rejected"), 2)

        report = CleanupEngine(self.paths, self.ctx.journal, self.ctx.session.directory).run()
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["transform"]["restored"], 6)

    async def test_retry_and_continue_policy(self) -> None:
        config = make_config(
            self.tmp_path,
            overrides=[
                'pipeline.stages=[{"id":"s1-boom","module":"boom","on_error":"continue","retries":1}]'
            ],
        )
        paths = self.paths
        ctx, bus, memory = await make_context(config, paths)
        registry = ModuleRegistry.default()
        registry.register("boom", BoomModule)
        try:
            summary = await Engine(ctx, registry).run()
        finally:
            await bus.close()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["stages"][0]["status"], "failed")
        actions = [event.action for event in memory.events]
        self.assertIn("stage.retry", actions)
        self.assertIn("stage.failed", actions)
        self.assertIn("run.completed", actions)

    async def test_abort_policy_marks_failed(self) -> None:
        config = make_config(
            self.tmp_path,
            overrides=['pipeline.stages=[{"id":"s1-boom","module":"boom","on_error":"abort"}]'],
        )
        ctx, bus, memory = await make_context(config, self.paths)
        registry = ModuleRegistry.default()
        registry.register("boom", BoomModule)
        try:
            with self.assertRaises(Exception):
                await Engine(ctx, registry).run()
        finally:
            await bus.close()
        self.assertEqual(ctx.state.state.status.value, "failed")


class ResumeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config, paths = make_lab(tmp_path)
            crash_config = make_config(
                tmp_path, overrides=['pipeline.stop_after="s5-fileops"']
            )
            ctx, bus, memory = await make_context(crash_config, paths)
            try:
                with self.assertRaises(SimulatedCrash):
                    await Engine(ctx).run()
            finally:
                await bus.close()
            session_id = ctx.session.session_id
            state = StateStore.load(ctx.session.state_path)
            self.assertEqual(state.state.status.value, "interrupted")
            self.assertIn("s5-fileops", state.state.completed)

            session = SessionManager(paths).open(session_id)
            resumed_state = StateStore.load(session.state_path)
            resumed_bus = EventBus(session_id, [MemorySink()])
            await resumed_bus.start()
            resumed = ExecutionContext(
                config=config,
                paths=paths,
                events=resumed_bus,
                session=session,
                state=resumed_state,
                journal=ArtifactJournal(session.artifacts_path, paths, session_id),
                seed=1,
            )
            try:
                summary = await Engine(resumed).run(resume=True)
            finally:
                await resumed_bus.close()
            self.assertEqual(summary["status"], "completed")
            self.assertGreaterEqual(resumed.state.state.restarts, 2)
            skipped = [event for event in resumed_bus.replay() if event.action == "stage.already_completed"]
            self.assertGreaterEqual(len(skipped), 5)
            report = CleanupEngine(paths, resumed.journal, session.directory).run()
            self.assertTrue(report["ok"], report["errors"])


if __name__ == "__main__":
    unittest.main()
