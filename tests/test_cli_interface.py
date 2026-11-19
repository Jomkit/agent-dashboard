import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tracker.adapters.sqlite_store import SqliteSessionStore
from tracker.domain.models import AgentSession, IngestCursor, NormalizedEvent, SessionStatus, TokenMetrics
from tracker.interfaces.cli.app import _preprocess_argv, build_parser, main
from tracker.interfaces.cli.client import (
    AmbiguousSessionError,
    DashboardClient,
    SessionNotFoundError,
    iter_sse_events,
)
from tracker.interfaces.cli.formatter import (
    format_event,
    format_session_detail,
    format_table,
    should_use_color,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class TestCLIInterface(unittest.TestCase):
    def setUp(self):
        self.session = AgentSession(
            session_id="sess-12345678",
            parent_session_id=None,
            agent_type="AGY",
            model_name="gemini-3.8-flash",
            working_dir="/home/user/project",
            started_at="2026-11-18T12:00:00Z",
            status=SessionStatus.RUNNING,
            tokens=TokenMetrics(input_tokens=1500, output_tokens=350),
        )
        self.events = [
            NormalizedEvent(
                event_id="e1",
                session_id="sess-12345678",
                event_type="USER_PROMPT",
                timestamp="2026-11-18T12:00:01Z",
                payload={"text": "Write a test"},
                sequence_num=1,
            ),
            NormalizedEvent(
                event_id="e2",
                session_id="sess-12345678",
                event_type="THINKING",
                timestamp="2026-11-18T12:00:02Z",
                payload={"thinking": "Analyzing...", "input_tokens": 1500, "output_tokens": 50},
                sequence_num=2,
            ),
            NormalizedEvent(
                event_id="e3",
                session_id="sess-12345678",
                event_type="TOOL_CALL",
                timestamp="2026-11-18T12:00:03Z",
                payload={"tool_name": "read_file", "args": {"path": "main.py"}},
                sequence_num=3,
            ),
        ]

    def test_legacy_argv_preprocessing(self):
        self.assertEqual(_preprocess_argv([]), ["serve"])
        self.assertEqual(_preprocess_argv(["--port", "9000"]), ["serve", "--port", "9000"])
        self.assertEqual(_preprocess_argv(["--host", "0.0.0.0"]), ["serve", "--host", "0.0.0.0"])
        self.assertEqual(_preprocess_argv(["list"]), ["list"])
        self.assertEqual(_preprocess_argv(["tail", "sess-1"]), ["tail", "sess-1"])
        self.assertEqual(_preprocess_argv(["inspect", "sess-1"]), ["inspect", "sess-1"])
        self.assertEqual(_preprocess_argv(["--help"]), ["--help"])

    def test_should_use_color_detection(self):
        class MockTTY:
            def __init__(self, tty: bool):
                self._tty = tty

            def isatty(self):
                return self._tty

        self.assertTrue(should_use_color(MockTTY(True), {}))
        self.assertFalse(should_use_color(MockTTY(False), {}))
        self.assertFalse(should_use_color(MockTTY(True), {"NO_COLOR": "1"}))
        self.assertTrue(should_use_color(MockTTY(True), {"NO_COLOR": ""}))
        self.assertFalse(should_use_color(MockTTY(True), {"TERM": "dumb"}))

    def test_format_table_ansi_alignment(self):
        sessions = [
            self.session,
            AgentSession(
                session_id="sess-87654321",
                parent_session_id=None,
                agent_type="CLAUDE",
                model_name="claude-3-7-sonnet",
                working_dir="/tmp",
                started_at="2026-11-18T12:05:00Z",
                status=SessionStatus.WAITING_USER,
                tokens=TokenMetrics(input_tokens=10, output_tokens=20),
            ),
        ]
        out_color = format_table(sessions, color=True)
        lines = [ANSI_RE.sub("", line) for line in out_color.splitlines()]
        # Check alignment: header and rows have identical column starting offsets
        token_offsets = [line.index("Tokens" if i == 0 else "↑") for i, line in enumerate(lines) if i != 1]
        self.assertEqual(len(set(token_offsets)), 1)

    def test_format_event_rendering(self):
        for ev in self.events:
            formatted = format_event(ev, color=False)
            self.assertIn(ev.event_type, formatted)
            self.assertNotIn("\x1b[", formatted)

    def test_cli_list_command_table_and_json(self):
        mock_client = MagicMock(spec=DashboardClient)
        mock_client.list_sessions.return_value = [self.session]

        stdout_table = io.StringIO()
        code = main(argv=["list"], client=mock_client, stdout=stdout_table)
        self.assertEqual(code, 0)
        self.assertIn("sess-12345678", stdout_table.getvalue())
        self.assertIn("RUNNING", stdout_table.getvalue())

        stdout_json = io.StringIO()
        code = main(argv=["list", "--json"], client=mock_client, stdout=stdout_json)
        self.assertEqual(code, 0)
        data = json.loads(stdout_json.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["session_id"], "sess-12345678")

    def test_cli_subcommands_accept_shared_flags(self):
        mock_client = MagicMock(spec=DashboardClient)
        mock_client.list_sessions.return_value = [self.session]
        stdout = io.StringIO()
        # Test flag after subcommand
        code = main(argv=["list", "--url", "http://127.0.0.1:9090", "--no-color"], client=mock_client, stdout=stdout)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[", stdout.getvalue())

        # Test flag before subcommand
        stdout_before = io.StringIO()
        code = main(argv=["--url", "http://127.0.0.1:9090", "--no-color", "list"], client=mock_client, stdout=stdout_before)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b[", stdout_before.getvalue())

    def test_cli_inspect_respects_limit(self):
        mock_client = MagicMock(spec=DashboardClient)
        mock_client.resolve_session.return_value = self.session
        mock_client.get_session_events.return_value = self.events[:2]

        stdout = io.StringIO()
        code = main(argv=["inspect", "sess-12", "--limit", "2"], client=mock_client, stdout=stdout)
        self.assertEqual(code, 0)
        mock_client.get_session_events.assert_called_once_with("sess-12345678", limit=2, latest=True)

    def test_cli_inspect_command(self):
        mock_client = MagicMock(spec=DashboardClient)
        mock_client.resolve_session.return_value = self.session
        mock_client.get_session_events.return_value = self.events
        mock_client.get_all_session_events.return_value = self.events

        stdout = io.StringIO()
        code = main(argv=["inspect", "sess-12"], client=mock_client, stdout=stdout)
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("Session sess-12345678", output)
        self.assertIn("USER_PROMPT", output)
        self.assertIn("TOOL_CALL", output)

    def test_cli_tail_command_no_follow(self):
        mock_client = MagicMock(spec=DashboardClient)
        mock_client.tail_events.return_value = iter(self.events)

        stdout = io.StringIO()
        code = main(argv=["tail", "sess-12", "--no-follow"], client=mock_client, stdout=stdout)
        self.assertEqual(code, 0)
        output = stdout.getvalue()
        self.assertIn("USER_PROMPT", output)
        self.assertIn("THINKING", output)

    def test_resolve_session_empty_id_raises(self):
        client = DashboardClient()
        with self.assertRaises(SessionNotFoundError):
            client.resolve_session("")

    def test_cli_session_not_found_exits_1(self):
        mock_client = MagicMock(spec=DashboardClient)
        mock_client.resolve_session.side_effect = SessionNotFoundError("Session not found: none")

        stderr = io.StringIO()
        code = main(argv=["inspect", "none"], client=mock_client, stderr=stderr)
        self.assertEqual(code, 1)
        self.assertIn("Session not found", stderr.getvalue())

    def test_client_offline_fallback_with_sqlite_store(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test.db"
            store = SqliteSessionStore(db_path=str(db_path), in_memory_test=True)
            store.upsert_session(self.session)
            cursor = IngestCursor(session_id="sess-12345678", dev=1, ino=1, byte_offset=100, last_sequence_num=3)
            store.commit_ingest_batch(self.session, self.events, cursor)

            # Client pointing to non-running daemon port
            client = DashboardClient(base_url="http://127.0.0.1:59999", store=store)
            self.assertFalse(client.is_daemon_running())

            sessions = client.list_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].session_id, "sess-12345678")

            resolved = client.resolve_session("sess-12")
            self.assertEqual(resolved.session_id, "sess-12345678")

            events = client.get_session_events("sess-12345678", limit=10)
            self.assertEqual(len(events), 3)

            store.close()

    def test_client_offline_missing_db_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            non_existent_db = Path(tmp_dir) / "sub" / "missing.db"
            client = DashboardClient(base_url="http://127.0.0.1:59999", db_path=str(non_existent_db))
            sessions = client.list_sessions()
            self.assertEqual(sessions, [])
            self.assertFalse(non_existent_db.exists())

    def test_sse_event_parsing_and_filtering(self):
        lines = [
            b": connected\n",
            b"\n",
            b": ping\n",
            b"\n",
            b"id: sess-1:1\n",
            b"event: USER_PROMPT\n",
            b"data: {\"text\": \"hello\"}\n",
            b"\n",
            b"id: sess-other:1\n",
            b"event: USER_PROMPT\n",
            b"data: {\"text\": \"ignore me\"}\n",
            b"\n",
            b"id: sess-1:2\n",
            b"event: ASSISTANT_MESSAGE\n",
            b"data: {\"content\": \"world\"}\n",
            b"\n",
        ]
        parsed = list(iter_sse_events(lines, target_session_id="sess-1"))
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].session_id, "sess-1")
        self.assertEqual(parsed[0].sequence_num, 1)
        self.assertEqual(parsed[0].event_type, "USER_PROMPT")
        self.assertEqual(parsed[1].sequence_num, 2)
        self.assertEqual(parsed[1].event_type, "ASSISTANT_MESSAGE")


if __name__ == "__main__":
    unittest.main()
