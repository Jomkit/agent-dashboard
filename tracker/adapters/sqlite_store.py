from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
from typing import Any, Dict, Generator, List, Optional

from tracker.domain.models import (
    AgentSession,
    IngestCursor,
    NormalizedEvent,
    SessionStatus,
    TokenMetrics,
)


class SqliteSessionStore:
    def __init__(self, db_path: Optional[str] = None, in_memory_test: bool = False):
        if db_path is None:
            data_dir = Path.home() / ".local" / "share" / "agent-dashboard"
            data_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(data_dir / "dashboard.db")
        else:
            self.db_path = os.path.expanduser(db_path)
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.in_memory_test = in_memory_test
        self._write_lock = threading.Lock()
        self._closed = False
        self._reader_pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=16)

        # Dedicated writer connection
        self._writer_conn = self._create_connection()
        self._init_schema()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        if not self.in_memory_test:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
        else:
            conn.execute("PRAGMA synchronous = OFF;")
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            with self._writer_conn:
                self._writer_conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        parent_session_id TEXT,
                        agent_type TEXT,
                        model_name TEXT,
                        working_dir TEXT,
                        started_at TEXT,
                        ended_at TEXT,
                        status TEXT,
                        input_tokens INTEGER DEFAULT 0,
                        output_tokens INTEGER DEFAULT 0,
                        cache_read_tokens INTEGER DEFAULT 0,
                        metadata_json TEXT,
                        reset_epoch INTEGER DEFAULT 0
                    );
                """)
                self._writer_conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        session_id TEXT,
                        sequence_num INTEGER,
                        event_id TEXT,
                        event_type TEXT,
                        timestamp TEXT,
                        payload_json TEXT,
                        PRIMARY KEY (session_id, sequence_num)
                    );
                """)
                self._writer_conn.execute("""
                    CREATE TABLE IF NOT EXISTS cursors (
                        session_id TEXT PRIMARY KEY,
                        dev INTEGER,
                        ino INTEGER,
                        byte_offset INTEGER,
                        header_hash TEXT,
                        last_sequence_num INTEGER DEFAULT 0
                    );
                """)

    @contextmanager
    def reader(self) -> Generator[sqlite3.Connection, None, None]:
        if self._closed:
            raise RuntimeError("SqliteSessionStore is closed")
        try:
            conn = self._reader_pool.get_nowait()
        except queue.Empty:
            conn = self._create_connection()

        try:
            yield conn
        finally:
            if not self._closed:
                try:
                    self._reader_pool.put_nowait(conn)
                except queue.Full:
                    conn.close()
            else:
                conn.close()

    def upsert_session(self, session: AgentSession) -> None:
        with self._write_lock:
            with self._writer_conn:
                self._writer_conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, parent_session_id, agent_type, model_name,
                        working_dir, started_at, ended_at, status,
                        input_tokens, output_tokens, cache_read_tokens, metadata_json, reset_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        parent_session_id = coalesce(excluded.parent_session_id, sessions.parent_session_id),
                        agent_type = excluded.agent_type,
                        model_name = excluded.model_name,
                        working_dir = excluded.working_dir,
                        started_at = excluded.started_at,
                        ended_at = excluded.ended_at,
                        status = excluded.status,
                        input_tokens = excluded.input_tokens,
                        output_tokens = excluded.output_tokens,
                        cache_read_tokens = excluded.cache_read_tokens,
                        metadata_json = excluded.metadata_json,
                        reset_epoch = excluded.reset_epoch
                """,
                    (
                        session.session_id,
                        session.parent_session_id,
                        session.agent_type,
                        session.model_name,
                        session.working_dir,
                        session.started_at,
                        session.ended_at,
                        session.status.value,
                        session.tokens.input_tokens,
                        session.tokens.output_tokens,
                        session.tokens.cache_read_tokens,
                        json.dumps(session.metadata),
                        session.reset_epoch,
                    ),
                )

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        with self.reader() as conn:
            cursor = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_session(row)

    def list_sessions(self) -> List[AgentSession]:
        with self.reader() as conn:
            cursor = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC")
            return [self._row_to_session(row) for row in cursor.fetchall()]

    def commit_ingest_batch(
        self,
        session: AgentSession,
        events: List[NormalizedEvent],
        cursor: IngestCursor,
        reset_existing: bool = False,
    ) -> None:
        with self._write_lock:
            with self._writer_conn:
                # 1. Upsert session
                self._writer_conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, parent_session_id, agent_type, model_name,
                        working_dir, started_at, ended_at, status,
                        input_tokens, output_tokens, cache_read_tokens, metadata_json, reset_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        parent_session_id = coalesce(excluded.parent_session_id, sessions.parent_session_id),
                        agent_type = excluded.agent_type,
                        model_name = excluded.model_name,
                        working_dir = excluded.working_dir,
                        started_at = excluded.started_at,
                        ended_at = excluded.ended_at,
                        status = excluded.status,
                        input_tokens = excluded.input_tokens,
                        output_tokens = excluded.output_tokens,
                        cache_read_tokens = excluded.cache_read_tokens,
                        metadata_json = excluded.metadata_json,
                        reset_epoch = excluded.reset_epoch
                """,
                    (
                        session.session_id,
                        session.parent_session_id,
                        session.agent_type,
                        session.model_name,
                        session.working_dir,
                        session.started_at,
                        session.ended_at,
                        session.status.value,
                        session.tokens.input_tokens,
                        session.tokens.output_tokens,
                        session.tokens.cache_read_tokens,
                        json.dumps(session.metadata),
                        session.reset_epoch,
                    ),
                )

                # 2. If reset_existing, purge old events
                if reset_existing:
                    self._writer_conn.execute("DELETE FROM events WHERE session_id = ?", (session.session_id,))

                # 3. Batch insert events idempotently
                for event in events:
                    self._writer_conn.execute(
                        """
                        INSERT OR IGNORE INTO events (
                            session_id, sequence_num, event_id, event_type, timestamp, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        (
                            event.session_id,
                            event.sequence_num,
                            event.event_id,
                            event.event_type,
                            event.timestamp,
                            json.dumps(event.payload),
                        ),
                    )

                # 4. Upsert cursor
                self._writer_conn.execute(
                    """
                    INSERT INTO cursors (
                        session_id, dev, ino, byte_offset, header_hash, last_sequence_num
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        dev = excluded.dev,
                        ino = excluded.ino,
                        byte_offset = excluded.byte_offset,
                        header_hash = excluded.header_hash,
                        last_sequence_num = excluded.last_sequence_num
                """,
                    (
                        cursor.session_id,
                        cursor.dev,
                        cursor.ino,
                        cursor.byte_offset,
                        cursor.header_hash,
                        cursor.last_sequence_num,
                    ),
                )

    def get_session_events(
        self,
        session_id: str,
        limit: int = 500,
        after_sequence: int = 0,
        latest: bool = False,
    ) -> List[NormalizedEvent]:
        with self.reader() as conn:
            if latest and after_sequence == 0:
                cursor = conn.execute(
                    """
                    SELECT * FROM events
                    WHERE session_id = ?
                    ORDER BY sequence_num DESC
                    LIMIT ?
                """,
                    (session_id, limit),
                )
                rows = cursor.fetchall()
                # Reverse descending slice to chronological ascending order
                rows.reverse()
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM events
                    WHERE session_id = ? AND sequence_num > ?
                    ORDER BY sequence_num ASC
                    LIMIT ?
                """,
                    (session_id, after_sequence, limit),
                )
                rows = cursor.fetchall()

            return [self._row_to_event(row) for row in rows]

    def get_cursor(self, session_id: str) -> Optional[IngestCursor]:
        with self.reader() as conn:
            cursor = conn.execute("SELECT * FROM cursors WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return IngestCursor(
                session_id=row["session_id"],
                dev=row["dev"],
                ino=row["ino"],
                byte_offset=row["byte_offset"],
                header_hash=row["header_hash"] or "",
                last_sequence_num=row["last_sequence_num"],
            )

    def close(self) -> None:
        self._closed = True
        # Drain and close all pooled reader connections
        while True:
            try:
                conn = self._reader_pool.get_nowait()
                conn.close()
            except queue.Empty:
                break
        with self._write_lock:
            self._writer_conn.close()

    def _row_to_session(self, row: sqlite3.Row) -> AgentSession:
        return AgentSession(
            session_id=row["session_id"],
            parent_session_id=row["parent_session_id"],
            agent_type=row["agent_type"] or "AGY",
            model_name=row["model_name"] or "",
            working_dir=row["working_dir"] or "",
            started_at=row["started_at"] or "",
            ended_at=row["ended_at"],
            status=SessionStatus(row["status"]) if row["status"] else SessionStatus.INITIALIZING,
            tokens=TokenMetrics(
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
                cache_read_tokens=row["cache_read_tokens"] or 0,
            ),
            metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            reset_epoch=row["reset_epoch"] or 0,
        )

    def _row_to_event(self, row: sqlite3.Row) -> NormalizedEvent:
        return NormalizedEvent(
            event_id=row["event_id"],
            session_id=row["session_id"],
            event_type=row["event_type"],
            timestamp=row["timestamp"],
            payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
            sequence_num=row["sequence_num"],
        )
