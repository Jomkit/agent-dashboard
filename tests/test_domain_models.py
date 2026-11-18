import unittest
from tracker.domain.models import (
    AgentSession,
    IngestCursor,
    NormalizedEvent,
    SessionStatus,
    TailBatch,
    TokenMetrics,
    apply_events,
    merge_discovered_session,
    reset_session_state,
)


class TestDomainModels(unittest.TestCase):
    def test_session_status_enum(self):
        self.assertEqual(SessionStatus.INITIALIZING.value, "INITIALIZING")
        self.assertEqual(SessionStatus.RUNNING.value, "RUNNING")
        self.assertEqual(SessionStatus.WAITING_USER.value, "WAITING_USER")
        self.assertEqual(SessionStatus.COMPLETED.value, "COMPLETED")
        self.assertEqual(SessionStatus.ERROR.value, "ERROR")

    def test_agent_session_immutability(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/home/user",
            started_at="2026-11-17T22:00:00Z",
        )
        self.assertEqual(session.status, SessionStatus.INITIALIZING)
        self.assertEqual(session.tokens.input_tokens, 0)
        self.assertEqual(session.reset_epoch, 0)

        # Confirm frozen / immutability
        with self.assertRaises(Exception):
            session.model_name = "other"  # type: ignore

    def test_merge_discovered_session_new(self):
        discovered = AgentSession(
            session_id="s1",
            parent_session_id="parent-1",
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/home/user",
            started_at="2026-11-17T22:00:00Z",
        )
        merged = merge_discovered_session(None, discovered)
        self.assertEqual(merged.session_id, "s1")
        self.assertEqual(merged.parent_session_id, "parent-1")

    def test_merge_discovered_session_preserves_runtime_state(self):
        existing = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/home/user",
            started_at="2026-11-17T22:00:00Z",
            status=SessionStatus.RUNNING,
            tokens=TokenMetrics(input_tokens=100, output_tokens=50),
            reset_epoch=2,
        )
        discovered = AgentSession(
            session_id="s1",
            parent_session_id="parent-99",
            agent_type="AGY",
            model_name="",
            working_dir="",
            started_at="",
        )
        merged = merge_discovered_session(existing, discovered)
        # Parent ID should be updated
        self.assertEqual(merged.parent_session_id, "parent-99")
        # Existing runtime state must NOT be clobbered
        self.assertEqual(merged.status, SessionStatus.RUNNING)
        self.assertEqual(merged.tokens.input_tokens, 100)
        self.assertEqual(merged.tokens.output_tokens, 50)
        self.assertEqual(merged.reset_epoch, 2)
        self.assertEqual(merged.model_name, "gemini-3.8-flash")

    def test_reset_session_state(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/home/user",
            started_at="2026-11-17T22:00:00Z",
            ended_at="2026-11-17T22:05:00Z",
            status=SessionStatus.COMPLETED,
            tokens=TokenMetrics(input_tokens=500, output_tokens=200),
            reset_epoch=0,
        )
        reset = reset_session_state(session)
        self.assertEqual(reset.status, SessionStatus.INITIALIZING)
        self.assertEqual(reset.tokens.input_tokens, 0)
        self.assertEqual(reset.tokens.output_tokens, 0)
        self.assertIsNone(reset.ended_at)
        self.assertEqual(reset.reset_epoch, 1)

    def test_apply_events_session_init(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="",
            working_dir="",
            started_at="2026-11-17T22:00:00Z",
        )
        events = [
            NormalizedEvent(
                event_id="e1",
                session_id="s1",
                event_type="SESSION_INIT",
                timestamp="2026-11-17T22:00:01Z",
                payload={"model_name": "gemini-3.8-flash", "working_dir": "/workspace"},
                sequence_num=1,
            )
        ]
        updated = apply_events(session, events)
        self.assertEqual(updated.model_name, "gemini-3.8-flash")
        self.assertEqual(updated.working_dir, "/workspace")
        self.assertEqual(updated.status, SessionStatus.RUNNING)

    def test_apply_events_thinking_tokens(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
            tokens=TokenMetrics(input_tokens=10, output_tokens=5, cache_read_tokens=2),
        )
        events = [
            NormalizedEvent(
                event_id="e2",
                session_id="s1",
                event_type="THINKING",
                timestamp="2026-11-17T22:00:02Z",
                payload={"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 20},
                sequence_num=2,
            )
        ]
        updated = apply_events(session, events)
        self.assertEqual(updated.tokens.input_tokens, 110)
        self.assertEqual(updated.tokens.output_tokens, 55)
        self.assertEqual(updated.tokens.cache_read_tokens, 22)
        self.assertEqual(updated.status, SessionStatus.RUNNING)

    def test_apply_events_state_change_completed(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
            status=SessionStatus.RUNNING,
        )
        events = [
            NormalizedEvent(
                event_id="e3",
                session_id="s1",
                event_type="STATE_CHANGE",
                timestamp="2026-11-17T22:01:00Z",
                payload={"status": "COMPLETED"},
                sequence_num=3,
            )
        ]
        updated = apply_events(session, events)
        self.assertEqual(updated.status, SessionStatus.COMPLETED)
        self.assertEqual(updated.ended_at, "2026-11-17T22:01:00Z")

    def test_apply_events_resume_from_completed(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
            ended_at="2026-11-17T22:01:00Z",
            status=SessionStatus.COMPLETED,
        )
        events = [
            NormalizedEvent(
                event_id="e4",
                session_id="s1",
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:02:00Z",
                payload={"text": "Continue task"},
                sequence_num=4,
            )
        ]
        updated = apply_events(session, events)
        self.assertEqual(updated.status, SessionStatus.RUNNING)
        self.assertIsNone(updated.ended_at)

    def test_apply_events_session_reset(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
            status=SessionStatus.RUNNING,
            tokens=TokenMetrics(input_tokens=100, output_tokens=50),
            reset_epoch=0,
        )
        events = [
            NormalizedEvent(
                event_id="e-reset",
                session_id="s1",
                event_type="SESSION_RESET",
                timestamp="2026-11-17T22:03:00Z",
                payload={},
                sequence_num=0,
            )
        ]
        updated = apply_events(session, events)
        self.assertEqual(updated.status, SessionStatus.INITIALIZING)
        self.assertEqual(updated.tokens.input_tokens, 0)
        self.assertEqual(updated.reset_epoch, 1)

    def test_apply_events_mismatched_session_id_raises(self):
        session = AgentSession(
            session_id="s1",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/workspace",
            started_at="2026-11-17T22:00:00Z",
        )
        events = [
            NormalizedEvent(
                event_id="e1",
                session_id="s2",  # Mismatch!
                event_type="USER_PROMPT",
                timestamp="2026-11-17T22:00:01Z",
                payload={},
                sequence_num=1,
            )
        ]
        with self.assertRaises(ValueError):
            apply_events(session, events)


if __name__ == "__main__":
    unittest.main()
