from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from typing import Dict, List, Optional

from tracker.adapters.sqlite_store import SqliteSessionStore
from tracker.adapters.sse_broadcaster import StdlibSSEBroadcaster
from tracker.app.engine import Engine
from tracker.domain.models import (
    AgentSession,
    IngestCursor,
    NormalizedEvent,
    SessionStatus,
    TailBatch,
    TokenMetrics,
)


class MockTranscriptAdapter:
    def __init__(self):
        self.sessions: Dict[str, AgentSession] = {}
        self.batches: Dict[str, TailBatch] = {}

    def discover_sessions(self) -> List[AgentSession]:
        return list(self.sessions.values())

    def tail_session(self, session_id: str, cursor: Optional[IngestCursor] = None) -> TailBatch:
        if session_id in self.batches:
            return self.batches[session_id]
        return TailBatch(
            cursor=cursor or IngestCursor(session_id=session_id, dev=1, ino=1, byte_offset=0),
            events=[],
        )


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_engine.db"
        self.store = SqliteSessionStore(db_path=str(self.db_path), in_memory_test=True)
        self.broadcaster = StdlibSSEBroadcaster()
        self.adapter = MockTranscriptAdapter()
        self.engine = Engine(
            adapter=self.adapter,
            store=self.store,
            broadcaster=self.broadcaster,
            poll_interval=0.01,
        )

        self.client_queue = self.broadcaster.register_client()

    def tearDown(self):
        self.engine.stop()
        self.broadcaster.unregister_client(self.client_queue)
        self.broadcaster.shutdown()
        self.store.close()
        self.temp_dir.cleanup()

    def _drain_broadcast_events(self) -> List[NormalizedEvent]:
        events = []
        while not self.client_queue.empty():
            ev = self.client_queue.get_nowait()
            if ev is not None:
                events.append(ev)
        return events

    def test_engine_discovers_and_initializes_session(self):
        session = AgentSession(
            session_id="sess-1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
        )
        self.adapter.sessions["sess-1"] = session

        self.engine.poll_once()

        stored = self.store.get_session("sess-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.session_id, "sess-1")

        broadcasted = self._drain_broadcast_events()
        self.assertEqual(len(broadcasted), 1)
        self.assertEqual(broadcasted[0].event_type, "SESSION_INIT")
        self.assertEqual(broadcasted[0].sequence_num, 0)
        self.assertEqual(broadcasted[0].session_id, "sess-1")

    def test_engine_tails_and_commits_events(self):
        session = AgentSession(
            session_id="sess-2",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
        )
        self.adapter.sessions["sess-2"] = session

        events = [
            NormalizedEvent(
                event_id="e1",
                session_id="sess-2",
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:00:01Z",
                payload={"text": "hello"},
                sequence_num=1,
            ),
            NormalizedEvent(
                event_id="e2",
                session_id="sess-2",
                event_type="THINKING",
                timestamp="2026-11-17T22:00:02Z",
                payload={"delta_tokens": {"input_tokens": 10, "output_tokens": 20}},
                sequence_num=2,
            ),
        ]
        self.adapter.batches["sess-2"] = TailBatch(
            cursor=IngestCursor(
                session_id="sess-2",
                dev=1,
                ino=1,
                byte_offset=250,
                last_sequence_num=2,
            ),
            events=events,
        )

        self.engine.poll_once()

        stored_session = self.store.get_session("sess-2")
        self.assertIsNotNone(stored_session)
        self.assertEqual(stored_session.status, SessionStatus.RUNNING)
        self.assertEqual(stored_session.tokens.input_tokens, 10)
        self.assertEqual(stored_session.tokens.output_tokens, 20)

        stored_events = self.store.get_session_events("sess-2")
        self.assertEqual(len(stored_events), 2)

        broadcasted = self._drain_broadcast_events()
        event_types = [e.event_type for e in broadcasted]
        self.assertIn("SESSION_INIT", event_types)
        self.assertIn("USER_PROMPT", event_types)
        self.assertIn("THINKING", event_types)

    def test_engine_reset_detected(self):
        session = AgentSession(
            session_id="sess-3",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
            tokens=TokenMetrics(input_tokens=100),
            reset_epoch=0,
        )
        self.store.upsert_session(session)
        self.adapter.sessions["sess-3"] = session

        # Reset batch
        new_event = NormalizedEvent(
            event_id="e-new",
            session_id="sess-3",
            event_type="USER_PROMPT",
            timestamp="2026-11-17T22:05:00Z",
            payload={"text": "fresh start"},
            sequence_num=1,
        )
        self.adapter.batches["sess-3"] = TailBatch(
            cursor=IngestCursor(
                session_id="sess-3",
                dev=1,
                ino=1,
                byte_offset=50,
                last_sequence_num=1,
            ),
            events=[new_event],
            reset_detected=True,
        )

        self.engine.poll_once()

        stored = self.store.get_session("sess-3")
        self.assertEqual(stored.reset_epoch, 1)
        self.assertEqual(stored.tokens.input_tokens, 0)

        broadcasted = self._drain_broadcast_events()
        event_types = [e.event_type for e in broadcasted]
        self.assertIn("SESSION_RESET", event_types)
        self.assertIn("USER_PROMPT", event_types)

    def test_engine_detects_archived_session(self):
        session = AgentSession(
            session_id="sess-archived",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
            status=SessionStatus.RUNNING,
        )
        self.store.upsert_session(session)

        # adapter returns no sessions
        self.adapter.sessions.clear()

        self.engine.poll_once()

        stored = self.store.get_session("sess-archived")
        self.assertEqual(stored.status, SessionStatus.COMPLETED)
        self.assertIsNotNone(stored.ended_at)

        broadcasted = self._drain_broadcast_events()
        self.assertEqual(len(broadcasted), 1)
        self.assertEqual(broadcasted[0].event_type, "STATE_CHANGE")
        self.assertEqual(broadcasted[0].session_id, "sess-archived")
        self.assertEqual(broadcasted[0].payload["status"], "COMPLETED")

    def test_engine_start_stop_lifecycle(self):
        self.engine.start()
        self.assertTrue(self.engine._running)
        time.sleep(0.05)
        self.engine.stop()
        self.assertFalse(self.engine._running)


if __name__ == "__main__":
    unittest.main()
