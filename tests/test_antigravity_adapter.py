import json
import os
from pathlib import Path
import tempfile
import unittest

from tracker.adapters.antigravity_adapter import AntigravityTranscriptAdapter
from tracker.domain.models import IngestCursor, SessionStatus


class TestAntigravityTranscriptAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_session_dir(self, session_id: str, with_sys_gen: bool = True):
        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        if with_sys_gen:
            sys_gen = session_dir / ".system_generated"
            sys_gen.mkdir(parents=True, exist_ok=True)
            (sys_gen / "logs").mkdir(parents=True, exist_ok=True)
            (sys_gen / "subagents").mkdir(parents=True, exist_ok=True)
        return session_dir

    def test_discover_sessions_filters_non_session_dirs(self):
        # Valid session
        self._create_session_dir("valid-session-1")
        # Non-session directory (e.g. cache or tmp)
        (self.base_dir / "random_folder").mkdir(parents=True, exist_ok=True)

        adapter = AntigravityTranscriptAdapter(base_dir=self.base_dir)
        sessions = adapter.discover_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "valid-session-1")

    def test_discover_sessions_2_pass_subagent_linkage(self):
        parent_dir = self._create_session_dir("parent-session")
        child_dir = self._create_session_dir("child-session")

        # Write subagent descriptor in parent's directory
        subagent_json = (
            parent_dir / ".system_generated" / "subagents" / "child-session.json"
        )
        with open(subagent_json, "w", encoding="utf-8") as f:
            json.dump({
                "conversationId": "child-session",
                "subagentDescriptor": {"typeName": "researcher", "role": "Researcher"},
                "state": "SUBAGENT_STATE_ALIVE",
            }, f)

        adapter = AntigravityTranscriptAdapter(base_dir=self.base_dir)
        sessions = {s.session_id: s for s in adapter.discover_sessions()}

        self.assertIn("parent-session", sessions)
        self.assertIn("child-session", sessions)
        self.assertIsNone(sessions["parent-session"].parent_session_id)
        self.assertEqual(sessions["child-session"].parent_session_id, "parent-session")

    def test_tail_session_uncreated_transcript_returns_empty_batch(self):
        self._create_session_dir("s-no-transcript")
        adapter = AntigravityTranscriptAdapter(base_dir=self.base_dir)

        batch = adapter.tail_session("s-no-transcript", cursor=None)
        self.assertEqual(len(batch.events), 0)
        self.assertFalse(batch.reset_detected)
        self.assertEqual(batch.cursor.dev, 0)
        self.assertEqual(batch.cursor.ino, 0)
        self.assertEqual(batch.cursor.byte_offset, 0)

    def test_tail_session_reads_and_normalizes_events(self):
        s_dir = self._create_session_dir("s1")
        transcript_path = s_dir / ".system_generated" / "logs" / "transcript.jsonl"

        lines = [
            json.dumps({
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-17T22:00:00Z",
                "content": "Please implement feature",
            }) + "\n",
            json.dumps({
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-17T22:00:01Z",
                "model": "gemini-3.8-flash",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 20,
                "thinking": "Planning the steps...",
                "content": "I will inspect files now.",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": "/home/user/file.py"}}
                ],
            }) + "\n",
            json.dumps({
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-17T22:00:02Z",
                "content": "File content read successfully",
            }) + "\n",
        ]
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        adapter = AntigravityTranscriptAdapter(base_dir=self.base_dir)
        batch = adapter.tail_session("s1", cursor=None)

        self.assertFalse(batch.reset_detected)
        self.assertGreater(batch.cursor.byte_offset, 0)
        self.assertEqual(batch.cursor.last_sequence_num, len(batch.events))

        event_types = [e.event_type for e in batch.events]
        self.assertIn("USER_PROMPT", event_types)
        self.assertIn("THINKING", event_types)
        self.assertIn("ASSISTANT_MESSAGE", event_types)
        self.assertIn("TOOL_CALL", event_types)
        self.assertIn("TOOL_RESULT", event_types)

        # Verify 1-based sequential sequence numbering
        for idx, event in enumerate(batch.events, start=1):
            self.assertEqual(event.sequence_num, idx)

    def test_tail_session_partial_line_preserves_offset(self):
        s_dir = self._create_session_dir("s-partial")
        transcript_path = s_dir / ".system_generated" / "logs" / "transcript.jsonl"

        complete_line = json.dumps({
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-11-17T22:00:00Z",
            "content": "Prompt 1",
        }) + "\n"
        incomplete_line = '{"step_index": 1, "type": "PLANNER_RES'  # Missing \n!

        with open(transcript_path, "wb") as f:
            f.write(complete_line.encode("utf-8") + incomplete_line.encode("utf-8"))

        adapter = AntigravityTranscriptAdapter(base_dir=self.base_dir)
        batch1 = adapter.tail_session("s-partial", cursor=None)

        # Should only emit the complete line
        self.assertEqual(len(batch1.events), 1)
        self.assertEqual(batch1.cursor.byte_offset, len(complete_line.encode("utf-8")))

        # Complete the line on disk (USER_INPUT)
        with open(transcript_path, "wb") as f:
            f.write(complete_line.encode("utf-8"))
            f.write((json.dumps({
                "step_index": 1,
                "type": "USER_INPUT",
                "created_at": "2026-11-17T22:00:01Z",
                "content": "Prompt 2",
            }) + "\n").encode("utf-8"))

        batch2 = adapter.tail_session("s-partial", cursor=batch1.cursor)
        self.assertEqual(len(batch2.events), 1)
        self.assertEqual(batch2.cursor.last_sequence_num, 2)

    def test_tail_session_truncation_detection(self):
        s_dir = self._create_session_dir("s-trunc")
        transcript_path = s_dir / ".system_generated" / "logs" / "transcript.jsonl"

        # Write long initial content (> 256 bytes)
        content_1 = (json.dumps({
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-11-17T22:00:00Z",
            "content": "X" * 300,
        }) + "\n").encode("utf-8")

        with open(transcript_path, "wb") as f:
            f.write(content_1)

        adapter = AntigravityTranscriptAdapter(base_dir=self.base_dir)
        batch1 = adapter.tail_session("s-trunc", cursor=None)
        self.assertFalse(batch1.reset_detected)
        self.assertGreater(batch1.cursor.byte_offset, 256)

        # Truncate file to shorter content
        content_2 = (json.dumps({
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-11-17T22:05:00Z",
            "content": "truncated short",
        }) + "\n").encode("utf-8")

        with open(transcript_path, "wb") as f:
            f.write(content_2)

        batch2 = adapter.tail_session("s-trunc", cursor=batch1.cursor)
        self.assertTrue(batch2.reset_detected)
        self.assertEqual(batch2.cursor.last_sequence_num, 1)
        self.assertEqual(batch2.events[0].sequence_num, 1)

    def test_tail_session_path_traversal_rejected(self):
        adapter = AntigravityTranscriptAdapter(base_dir=self.base_dir)
        with self.assertRaises(ValueError):
            adapter.tail_session("../outside_dir", cursor=None)


if __name__ == "__main__":
    unittest.main()
