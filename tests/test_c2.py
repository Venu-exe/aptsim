from __future__ import annotations

import json
import unittest

from aptsim.c2.client import C2Client
from aptsim.c2.protocol import ProtocolCodec, ReplayGuard
from aptsim.c2.scenario import ScenarioPlayer
from aptsim.c2.server import C2Server
from aptsim.core.errors import ProtocolError

SECRET = "unit-test-secret"


class CodecTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        codec = ProtocolCodec(SECRET)
        frame = codec.encode("register", session_id="s1", capabilities=["beacon"])
        body = codec.decode(frame)
        self.assertEqual(body["type"], "register")
        self.assertEqual(body["session_id"], "s1")
        self.assertNotIn("auth", body)

    def test_tampered_frame_rejected(self) -> None:
        codec = ProtocolCodec(SECRET)
        frame = codec.encode("poll", session_id="s1")
        envelope = json.loads(frame)
        envelope["session_id"] = "s2"
        with self.assertRaises(ProtocolError):
            codec.decode(json.dumps(envelope))

    def test_wrong_key_rejected(self) -> None:
        sender = ProtocolCodec(SECRET)
        receiver = ProtocolCodec("another-secret")
        with self.assertRaises(ProtocolError):
            receiver.decode(sender.encode("poll", session_id="s1"))

    def test_replay_rejected(self) -> None:
        codec = ProtocolCodec(SECRET)
        frame = codec.encode("heartbeat", session_id="s1", seq=1)
        codec.decode(frame)
        with self.assertRaises(ProtocolError):
            codec.decode(frame)

    def test_stale_frame_rejected(self) -> None:
        sender = ProtocolCodec(SECRET, clock=lambda: 1000.0)
        receiver = ProtocolCodec(SECRET, clock=lambda: 2000.0)
        with self.assertRaises(ProtocolError):
            receiver.decode(sender.encode("poll", session_id="s1"))

    def test_oversize_frame_rejected(self) -> None:
        codec = ProtocolCodec(SECRET, max_frame=200)
        with self.assertRaises(ProtocolError):
            codec.encode("poll", session_id="s1", blob="x" * 500)
        with self.assertRaises(ProtocolError):
            codec.decode(b"{" + b"a" * 500 + b"}")

    def test_malformed_frame_rejected(self) -> None:
        codec = ProtocolCodec(SECRET)
        for payload in (b"", b"not-json", b"[]", b'{"type":"poll"}', b'{"v":1,"type":"poll","ts":1,"nonce":"x"}'):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError):
                    codec.decode(payload)

    def test_short_secret_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ProtocolCodec("short")

    def test_replay_guard_prunes(self) -> None:
        guard = ReplayGuard(window=0.01)
        guard.check("a", 1.0)
        self.assertEqual(len(guard), 1)
        import time

        time.sleep(0.02)
        guard.check("b", 1.0)
        self.assertEqual(len(guard), 1)


class ScenarioTests(unittest.TestCase):
    def test_batches_and_remaining(self) -> None:
        player = ScenarioPlayer(
            [{"task_id": f"t{i}", "type": "noop", "args": {}} for i in range(5)],
            tasks_per_poll=2,
        )
        self.assertEqual(len(player.next_tasks("s")), 2)
        self.assertEqual(player.remaining("s"), 3)
        self.assertEqual(len(player.next_tasks("s")), 2)
        self.assertEqual(len(player.next_tasks("s")), 1)
        self.assertEqual(player.remaining("s"), 0)
        self.assertEqual(player.next_tasks("s"), [])


class ServerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_heartbeat_and_task_flow(self) -> None:
        events = []

        async def on_event(action, data):
            events.append((action, data))

        tasks = [
            {"task_id": "t-1", "type": "noop", "args": {}},
            {"task_id": "t-2", "type": "shell", "args": {"cmd": "id"}},
            {"task_id": "t-3", "type": "report_status", "args": {}},
        ]
        server = C2Server(secret=SECRET, tasks=tasks, tasks_per_poll=2, on_event=on_event)
        port = await server.start()
        self.assertGreater(port, 0)
        client = C2Client(host="127.0.0.1", port=port, secret=SECRET, session_id="unit-client")
        await client.connect()
        try:
            registered = await client.register(["beacon"])
            self.assertEqual(registered["type"], "registered")
            ack = await client.heartbeat(1)
            self.assertEqual(ack["beats"], 1)
            batch = await client.poll()
            self.assertEqual([task["task_id"] for task in batch["tasks"]], ["t-1", "t-2"])
            await client.send_result("t-1", "ok", {"inert": True})
            second = await client.poll()
            self.assertEqual([task["task_id"] for task in second["tasks"]], ["t-3"])
            self.assertTrue(second["exhausted"])
            third = await client.poll()
            self.assertEqual(third["tasks"], [])
            sessions = server.active_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_id"], "unit-client")
            self.assertEqual(sessions[0]["heartbeats"], 1)
            self.assertEqual(sessions[0]["results"][0]["task_id"], "t-1")
        finally:
            await client.close()
            await server.stop()

        actions = [action for action, _ in events]
        self.assertIn("server.registered", actions)
        self.assertIn("server.poll", actions)
        self.assertIn("server.result", actions)

    async def test_unknown_session_rejected(self) -> None:
        server = C2Server(secret=SECRET, tasks=[])
        port = await server.start()
        client = C2Client(host="127.0.0.1", port=port, secret=SECRET, session_id="ghost")
        await client.connect()
        try:
            with self.assertRaises(ProtocolError):
                await client.poll()
        finally:
            await client.close()
            await server.stop()

    async def test_non_loopback_bind_refused(self) -> None:
        with self.assertRaises(ValueError):
            C2Server(host="0.0.0.0")

    async def test_bad_signature_closes_connection(self) -> None:
        server = C2Server(secret=SECRET, tasks=[])
        port = await server.start()
        try:
            reader, writer = await __import__("asyncio").open_connection("127.0.0.1", port)
            writer.write(b'{"v":1,"type":"poll","ts":0,"nonce":"x","auth":"deadbeef"}\n')
            await writer.drain()
            line = await __import__("asyncio").wait_for(reader.readline(), timeout=2)
            self.assertIn(b"bad_frame", line)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()
