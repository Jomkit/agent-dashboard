import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

# Ensure local test server is not intercepted by environment HTTP proxy
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

from tracker.adapters.http_server import DashboardServer
from tracker.adapters.sqlite_store import SqliteSessionStore
from tracker.adapters.sse_broadcaster import StdlibSSEBroadcaster
from tracker.domain.models import (
    AgentSession,
    NormalizedEvent,
    SessionStatus,
    TokenMetrics,
)


class TestDashboardServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test.db"
        cls.store = SqliteSessionStore(db_path=str(cls.db_path), in_memory_test=True)
        cls.broadcaster = StdlibSSEBroadcaster()

        # Seed initial session
        cls.test_session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/home/user",
            started_at="2026-11-17T22:00:00Z",
            status=SessionStatus.RUNNING,
            tokens=TokenMetrics(input_tokens=100, output_tokens=50),
        )
        cls.store.upsert_session(cls.test_session)

        # Seed events
        events = [
            NormalizedEvent(
                event_id="e1",
                session_id="s1",
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:00:01Z",
                payload={"text": "hello"},
                sequence_num=1,
            ),
            NormalizedEvent(
                event_id="e2",
                session_id="s1",
                event_type="THINKING",
                timestamp="2026-11-17T22:00:02Z",
                payload={"thinking": "work"},
                sequence_num=2,
            ),
        ]
        from tracker.domain.models import IngestCursor
        cursor = IngestCursor(session_id="s1", dev=1, ino=1, byte_offset=100, last_sequence_num=2)
        cls.store.commit_ingest_batch(cls.test_session, events, cursor)

        web_dir = Path(__file__).resolve().parent.parent / "tracker" / "interfaces" / "web"
        cls.server = DashboardServer(
            host="127.0.0.1",
            port=0,
            store=cls.store,
            broadcaster=cls.broadcaster,
            web_dir=web_dir,
            ping_interval=0.05,
        )
        cls.server_thread = threading.Thread(target=cls.server.start, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.broadcaster.shutdown()
        cls.store.close()
        cls.temp_dir.cleanup()

    def test_health_endpoint(self):
        url = f"{self.base_url}/api/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")

    def test_sessions_endpoint(self):
        url = f"{self.base_url}/api/sessions"
        with urllib.request.urlopen(url, timeout=2) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["session_id"], "s1")
            self.assertEqual(data[0]["status"], "RUNNING")

    def test_session_events_endpoint(self):
        url = f"{self.base_url}/api/sessions/s1/events?after_sequence=0&limit=10"
        with urllib.request.urlopen(url, timeout=2) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["sequence_num"], 1)
            self.assertEqual(data[1]["sequence_num"], 2)

    def test_session_events_404_on_missing_session(self):
        url = f"{self.base_url}/api/sessions/s-nonexistent/events"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=2)
        self.assertEqual(ctx.exception.code, 404)

    def test_static_css_endpoint(self):
        url = f"{self.base_url}/static/css/style.css"
        with urllib.request.urlopen(url, timeout=2) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/css", resp.headers.get("Content-Type", ""))
            content = resp.read().decode("utf-8")
            self.assertIn("--bg", content)

    def test_sse_stream_connected_and_broadcast(self):
        url = f"{self.base_url}/api/events"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))

            # Read initial flush comment
            first_line = resp.readline().decode("utf-8")
            self.assertIn(": connected", first_line)

            # Broadcast an event
            event = NormalizedEvent(
                event_id="e-sse",
                session_id="s1",
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:10:00Z",
                payload={"text": "broadcast message"},
                sequence_num=3,
            )
            self.broadcaster.broadcast(event)

            # Read lines until broadcast event frame arrives (ignoring heartbeats)
            lines = []
            for _ in range(20):
                line = resp.readline().decode("utf-8")
                stripped = line.strip()
                if stripped and not stripped.startswith(": ping"):
                    lines.append(stripped)
                    if "event: USER_PROMPT" in lines and any(l.startswith("data: ") for l in lines):
                        break

            self.assertIn("id: s1:3", lines)
            self.assertIn("event: USER_PROMPT", lines)
            data_line = [l for l in lines if l.startswith("data: ")][0]
            payload = json.loads(data_line[6:])
            self.assertEqual(payload.get("text"), "broadcast message")


if __name__ == "__main__":
    unittest.main()
