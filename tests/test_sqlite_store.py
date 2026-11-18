import json
from pathlib import Path
import tempfile
import threading
import unittest

from tracker.domain.models import (
    AgentSession,
    IngestCursor,
    NormalizedEvent,
    SessionStatus,
    TokenMetrics,
)
from tracker.adapters.sqlite_store import SqliteSessionStore


class TestSqliteSessionStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.store = SqliteSessionStore(db_path=str(self.db_path), in_memory_test=True)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_upsert_and_get_session(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/home/user",
            started_at="2026-11-17T22:00:00Z",
            ended_at=None,
            status=SessionStatus.INITIALIZING,
            tokens=TokenMetrics(input_tokens=100, output_tokens=50, cache_read_tokens=20),
            metadata={"tag": "test"},
            reset_epoch=0,
        )
        self.store.upsert_session(session)

        retrieved = self.store.get_session("s1")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.session_id, "s1")
        self.assertEqual(retrieved.agent_type, "AGY")
        self.assertEqual(retrieved.status, SessionStatus.INITIALIZING)
        self.assertEqual(retrieved.tokens.input_tokens, 100)
        self.assertEqual(retrieved.metadata.get("tag"), "test")
        self.assertEqual(retrieved.reset_epoch, 0)

    def test_list_sessions_ordered_by_started_at_desc(self):
        s1 = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini",
            working_dir="/dir1",
            started_at="2026-11-17T21:00:00Z",
        )
        s2 = AgentSession(
            session_id="s2",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini",
            working_dir="/dir2",
            started_at="2026-11-17T22:00:00Z",
        )
        self.store.upsert_session(s1)
        self.store.upsert_session(s2)

        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 2)
        # s2 is newer, so it must be first
        self.assertEqual(sessions[0].session_id, "s2")
        self.assertEqual(sessions[1].session_id, "s1")

    def test_commit_ingest_batch_atomic(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini",
            working_dir="/dir",
            started_at="2026-11-17T22:00:00Z",
            status=SessionStatus.RUNNING,
            tokens=TokenMetrics(input_tokens=50, output_tokens=25),
            reset_epoch=1,
        )
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
                payload={"input_tokens": 50, "output_tokens": 25},
                sequence_num=2,
            ),
        ]
        cursor = IngestCursor(
            session_id="s1",
            dev=123,
            ino=456,
            byte_offset=1024,
            header_hash="abc",
            last_sequence_num=2,
        )

        self.store.commit_ingest_batch(session, events, cursor, reset_existing=False)

        # Verify session snapshot
        retrieved_session = self.store.get_session("s1")
        self.assertEqual(retrieved_session.status, SessionStatus.RUNNING)
        self.assertEqual(retrieved_session.reset_epoch, 1)

        # Verify cursor
        retrieved_cursor = self.store.get_cursor("s1")
        self.assertIsNotNone(retrieved_cursor)
        self.assertEqual(retrieved_cursor.byte_offset, 1024)
        self.assertEqual(retrieved_cursor.last_sequence_num, 2)

        # Verify events
        retrieved_events = self.store.get_session_events("s1", limit=10, after_sequence=0)
        self.assertEqual(len(retrieved_events), 2)
        self.assertEqual(retrieved_events[0].sequence_num, 1)
        self.assertEqual(retrieved_events[1].sequence_num, 2)

    def test_commit_ingest_batch_idempotency_duplicate_sequence_num(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini",
            working_dir="/dir",
            started_at="2026-11-17T22:00:00Z",
        )
        events = [
            NormalizedEvent(
                event_id="e1",
                session_id="s1",
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:00:01Z",
                payload={"text": "first"},
                sequence_num=1,
            )
        ]
        cursor = IngestCursor(session_id="s1", dev=1, ino=1, byte_offset=100, last_sequence_num=1)
        self.store.commit_ingest_batch(session, events, cursor)

        # Re-commit duplicate sequence_num 1 with different payload
        dup_events = [
            NormalizedEvent(
                event_id="e1-dup",
                session_id="s1",
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:00:01Z",
                payload={"text": "should be ignored"},
                sequence_num=1,
            )
        ]
        self.store.commit_ingest_batch(session, dup_events, cursor)

        retrieved_events = self.store.get_session_events("s1")
        self.assertEqual(len(retrieved_events), 1)
        self.assertEqual(retrieved_events[0].payload.get("text"), "first")

    def test_commit_ingest_batch_reset_existing_purges_old_events(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini",
            working_dir="/dir",
            started_at="2026-11-17T22:00:00Z",
            reset_epoch=0,
        )
        old_events = [
            NormalizedEvent(
                event_id="old1",
                session_id="s1",
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:00:01Z",
                payload={"text": "old"},
                sequence_num=1,
            )
        ]
        cursor = IngestCursor(session_id="s1", dev=1, ino=1, byte_offset=100, last_sequence_num=1)
        self.store.commit_ingest_batch(session, old_events, cursor)

        # Simulate truncation reset
        reset_session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini",
            working_dir="/dir",
            started_at="2026-11-17T22:00:00Z",
            reset_epoch=1,
        )
        new_events = [
            NormalizedEvent(
                event_id="new1",
                session_id="s1",
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:05:01Z",
                payload={"text": "new"},
                sequence_num=1,
            )
        ]
        new_cursor = IngestCursor(session_id="s1", dev=1, ino=2, byte_offset=50, last_sequence_num=1)
        self.store.commit_ingest_batch(reset_session, new_events, new_cursor, reset_existing=True)

        retrieved_events = self.store.get_session_events("s1")
        self.assertEqual(len(retrieved_events), 1)
        self.assertEqual(retrieved_events[0].payload.get("text"), "new")
        self.assertEqual(self.store.get_session("s1").reset_epoch, 1)

    def test_get_session_events_pagination_and_latest(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini",
            working_dir="/dir",
            started_at="2026-11-17T22:00:00Z",
        )
        events = [
            NormalizedEvent(
                event_id=f"e{i}",
                session_id="s1",
                event_type="USER_PROMPT",
                timestamp=f"2026-11-17T22:00:0{i}Z",
                payload={"i": i},
                sequence_num=i,
            )
            for i in range(1, 6)
        ]
        cursor = IngestCursor(session_id="s1", dev=1, ino=1, byte_offset=500, last_sequence_num=5)
        self.store.commit_ingest_batch(session, events, cursor)

        # Pagination after sequence 3
        after_3 = self.store.get_session_events("s1", limit=10, after_sequence=3)
        self.assertEqual([e.sequence_num for e in after_3], [4, 5])

        # Latest 2 events
        latest_2 = self.store.get_session_events("s1", limit=2, latest=True)
        self.assertEqual([e.sequence_num for e in latest_2], [4, 5])

    def test_reader_pool_concurrency(self):
        """Verifies multi-threaded concurrent reads from reader pool."""
        session = AgentSession(
            session_id="s-concurrent",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini",
            working_dir="/dir",
            started_at="2026-11-17T22:00:00Z",
        )
        self.store.upsert_session(session)

        results = []
        errors = []

        def worker():
            try:
                for _ in range(10):
                    s = self.store.get_session("s-concurrent")
                    if s:
                        results.append(s.session_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors during concurrent read: {errors}")
        self.assertEqual(len(results), 50)


if __name__ == "__main__":
    unittest.main()
