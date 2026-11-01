from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from aptsim.core.events import EventBus, JSONLFileSink, MemorySink


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordering_and_sinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            memory = MemorySink()
            bus = EventBus("s-test", [memory, JSONLFileSink(path)])
            await bus.start()
            for index in range(25):
                await bus.emit("test", "tick", "unit", data={"index": index})
            await bus.close()

            self.assertEqual(len(memory.events), 25)
            self.assertEqual([event.seq for event in memory.events], list(range(1, 26)))
            self.assertTrue(all(event.session_id == "s-test" for event in memory.events))
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 25)
            self.assertEqual(json.loads(lines[0])["data"]["index"], 0)

    async def test_threadsafe_emission(self) -> None:
        memory = MemorySink()
        bus = EventBus("s-thread", [memory])
        await bus.start()
        barrier = threading.Barrier(4)

        def worker(worker_id: int) -> None:
            barrier.wait()
            for index in range(10):
                bus.emit_threadsafe(
                    "test", "thread.event", "worker", data={"worker": worker_id, "index": index}
                )

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        await asyncio.sleep(0.1)
        await bus.close()
        self.assertEqual(len(memory.events), 40)
        self.assertEqual(len({event.seq for event in memory.events}), 40)

    async def test_subscriber_and_replay(self) -> None:
        bus = EventBus("s-sub", [])
        await bus.start()
        seen = []

        async def subscriber(event):
            seen.append(event.action)

        unsubscribe = bus.subscribe(subscriber)
        await bus.emit("test", "one", "unit")
        await asyncio.sleep(0.02)
        unsubscribe()
        await bus.emit("test", "two", "unit")
        await bus.close()
        self.assertEqual(seen, ["one"])
        self.assertEqual(len(bus.replay()), 2)
        self.assertEqual(bus.dropped, 0)

    async def test_emit_after_close_is_counted(self) -> None:
        bus = EventBus("s-closed", [])
        await bus.start()
        await bus.close()
        await bus.emit("test", "late", "unit")
        self.assertEqual(bus.dropped, 1)


if __name__ == "__main__":
    unittest.main()
