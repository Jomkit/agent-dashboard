from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from tracker.adapters.sqlite_store import SqliteSessionStore
from tracker.domain.models import AgentSession, NormalizedEvent, SessionStatus, TokenMetrics


class CliClientError(Exception):
    """Base exception for CLI client errors."""


class SessionNotFoundError(CliClientError):
    """Raised when a target session_id cannot be found."""


class AmbiguousSessionError(CliClientError):
    """Raised when a session_id prefix matches multiple sessions."""


def default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "agent-dashboard" / "dashboard.db"


def _session_from_dict(data: Dict[str, Any]) -> AgentSession:
    tokens_raw = data.get("tokens") or {}
    tokens = TokenMetrics(
        input_tokens=int(tokens_raw.get("input_tokens") or 0),
        output_tokens=int(tokens_raw.get("output_tokens") or 0),
        cache_read_tokens=int(tokens_raw.get("cache_read_tokens") or 0),
    )
    raw_status = data.get("status", SessionStatus.INITIALIZING.value)
    try:
        status = SessionStatus(raw_status)
    except ValueError:
        status = SessionStatus.INITIALIZING

    return AgentSession(
        session_id=str(data["session_id"]),
        parent_session_id=data.get("parent_session_id"),
        agent_type=str(data.get("agent_type") or "AGY"),
        model_name=str(data.get("model_name") or ""),
        working_dir=str(data.get("working_dir") or ""),
        started_at=str(data.get("started_at") or ""),
        ended_at=data.get("ended_at"),
        status=status,
        tokens=tokens,
        reset_epoch=int(data.get("reset_epoch") or 0),
    )


def _event_from_dict(data: Dict[str, Any]) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=str(data.get("event_id") or ""),
        session_id=str(data["session_id"]),
        event_type=str(data["event_type"]),
        timestamp=str(data.get("timestamp") or ""),
        payload=dict(data.get("payload") or {}),
        sequence_num=int(data.get("sequence_num") or 0),
    )


def _parse_sse_frame(
    sse_id: Optional[str],
    event_type: Optional[str],
    data_lines: List[str],
) -> Optional[NormalizedEvent]:
    if not sse_id:
        return None
    session_id, sep, seq_str = sse_id.rpartition(":")
    if not sep or not session_id or not (seq_str.isascii() and seq_str.isdigit()):
        return None
    seq = int(seq_str)

    raw_data = "\n".join(data_lines)
    if raw_data:
        try:
            decoded = json.loads(raw_data)
            payload = decoded if isinstance(decoded, dict) else {"value": decoded}
        except json.JSONDecodeError:
            payload = {"raw": raw_data}
    else:
        payload = {}

    timestamp = str(payload.get("timestamp") or datetime.now(timezone.utc).isoformat())
    return NormalizedEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        event_type=event_type or "MESSAGE",
        timestamp=timestamp,
        payload=payload,
        sequence_num=seq,
    )


def iter_sse_events(
    lines: Iterable[bytes],
    target_session_id: str,
    after_sequence: int = 0,
) -> Iterator[NormalizedEvent]:
    """Parses SSE byte lines, filters to target_session_id, and deduplicates sequences."""
    sse_id: Optional[str] = None
    event_type: Optional[str] = None
    data_lines: List[str] = []
    last_seq = after_sequence

    def _flush() -> Optional[NormalizedEvent]:
        nonlocal sse_id, event_type, data_lines, last_seq
        if sse_id is None and event_type is None and not data_lines:
            return None
        ev = _parse_sse_frame(sse_id, event_type, data_lines)
        sse_id = None
        event_type = None
        data_lines = []
        if ev is None or ev.session_id != target_session_id:
            return None
        if ev.event_type == "SESSION_RESET":
            last_seq = 0
            return ev
        if ev.sequence_num != 0 and ev.sequence_num <= last_seq:
            return None
        if ev.sequence_num > last_seq:
            last_seq = ev.sequence_num
        return ev

    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            ev = _flush()
            if ev is not None:
                yield ev
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            sse_id = value
        elif field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)

    ev = _flush()
    if ev is not None:
        yield ev


class DashboardClient:
    """Zero-dependency client preferring daemon HTTP/SSE with SQLite read-only fallback."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        db_path: Optional[str] = None,
        store: Optional[SqliteSessionStore] = None,
        health_timeout: float = 0.5,
        request_timeout: float = 5.0,
        poll_interval: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.db_path = db_path
        self._injected_store = store
        self._owned_store: Optional[SqliteSessionStore] = None
        self.health_timeout = health_timeout
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval

    def __enter__(self) -> "DashboardClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owned_store is not None:
            self._owned_store.close()
            self._owned_store = None

    def is_daemon_running(self) -> bool:
        req = urllib.request.Request(f"{self.base_url}/api/health", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.health_timeout) as resp:
                if getattr(resp, "status", 200) != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8"))
                return isinstance(data, dict) and data.get("status") == "ok"
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return False

    def _get_sqlite_store(self) -> Optional[SqliteSessionStore]:
        if self._injected_store is not None:
            return self._injected_store
        if self._owned_store is not None:
            return self._owned_store
        effective_path = Path(self.db_path).expanduser() if self.db_path else default_db_path()
        if not effective_path.is_file():
            return None
        self._owned_store = SqliteSessionStore(db_path=str(effective_path))
        return self._owned_store

    def _http_get_json(self, path_and_query: str) -> Any:
        req = urllib.request.Request(f"{self.base_url}{path_and_query}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise CliClientError(f"Failed to connect to daemon at {self.base_url}: {e}") from e

    def list_sessions(
        self,
        status: Optional[SessionStatus] = None,
        limit: Optional[int] = None,
        use_http: Optional[bool] = None,
    ) -> List[AgentSession]:
        online = self.is_daemon_running() if use_http is None else use_http
        if online:
            raw = self._http_get_json("/api/sessions")
            sessions = [_session_from_dict(item) for item in raw]
        else:
            store = self._get_sqlite_store()
            sessions = store.list_sessions() if store is not None else []

        if status is not None:
            sessions = [s for s in sessions if s.status == status]
        if limit is not None:
            sessions = sessions[: max(0, limit)]
        return sessions

    def resolve_session(
        self,
        session_id_or_prefix: str,
        use_http: Optional[bool] = None,
    ) -> AgentSession:
        if not session_id_or_prefix:
            raise SessionNotFoundError("Session ID or prefix must not be empty")

        online = self.is_daemon_running() if use_http is None else use_http
        if not online:
            store = self._get_sqlite_store()
            if store is None:
                raise SessionNotFoundError(f"Session not found: {session_id_or_prefix}")
            exact = store.get_session(session_id_or_prefix)
            if exact is not None:
                return exact

        sessions = self.list_sessions(use_http=online)
        for session in sessions:
            if session.session_id == session_id_or_prefix:
                return session

        matches = [s for s in sessions if s.session_id.startswith(session_id_or_prefix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(m.session_id for m in matches[:5])
            raise AmbiguousSessionError(f"Ambiguous session prefix '{session_id_or_prefix}': {ids}")
        raise SessionNotFoundError(f"Session not found: {session_id_or_prefix}")

    def get_session_events(
        self,
        session_id: str,
        limit: int = 500,
        after_sequence: int = 0,
        latest: bool = False,
        use_http: Optional[bool] = None,
    ) -> List[NormalizedEvent]:
        online = self.is_daemon_running() if use_http is None else use_http
        if online:
            encoded_id = urllib.parse.quote(session_id, safe="")
            params = urllib.parse.urlencode(
                {
                    "limit": str(limit),
                    "after_sequence": str(after_sequence),
                    "latest": "true" if latest else "false",
                }
            )
            try:
                raw = self._http_get_json(f"/api/sessions/{encoded_id}/events?{params}")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise SessionNotFoundError(f"Session not found: {session_id}") from exc
                raise
            return [_event_from_dict(item) for item in raw]

        store = self._get_sqlite_store()
        if store is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        if store.get_session(session_id) is None:
            raise SessionNotFoundError(f"Session not found: {session_id}")
        return store.get_session_events(
            session_id,
            limit=limit,
            after_sequence=after_sequence,
            latest=latest,
        )

    def get_all_session_events(
        self,
        session_id: str,
        page_size: int = 500,
        use_http: Optional[bool] = None,
    ) -> List[NormalizedEvent]:
        online = self.is_daemon_running() if use_http is None else use_http
        all_events: List[NormalizedEvent] = []
        after_seq = 0
        while True:
            batch = self.get_session_events(
                session_id,
                limit=page_size,
                after_sequence=after_seq,
                latest=False,
                use_http=online,
            )
            if not batch:
                break
            all_events.extend(batch)
            next_seq = batch[-1].sequence_num
            if len(batch) < page_size or next_seq <= after_seq:
                break
            after_seq = next_seq
        return all_events

    def tail_events(
        self,
        session_id: str,
        follow: bool = True,
        initial_limit: int = 50,
        sleep_fn: Callable[[float], None] = time.sleep,
        use_http: Optional[bool] = None,
    ) -> Iterator[NormalizedEvent]:
        online = self.is_daemon_running() if use_http is None else use_http
        session = self.resolve_session(session_id, use_http=online)
        backlog = self.get_session_events(
            session.session_id,
            limit=initial_limit,
            after_sequence=0,
            latest=True,
            use_http=online,
        )
        last_seq = 0
        for ev in backlog:
            if ev.sequence_num > last_seq:
                last_seq = ev.sequence_num
            yield ev

        if not follow:
            return

        if online:
            req = urllib.request.Request(
                f"{self.base_url}/api/events",
                headers={"Accept": "text/event-stream"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=None) as resp:
                    yield from iter_sse_events(resp, target_session_id=session.session_id, after_sequence=last_seq)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                raise CliClientError(f"SSE stream error from {self.base_url}: {e}") from e
            return

        store = self._get_sqlite_store()
        if store is None:
            return
        last_reset_epoch = session.reset_epoch
        while True:
            sleep_fn(self.poll_interval)
            cur_session = store.get_session(session.session_id)
            if cur_session is not None and cur_session.reset_epoch > last_reset_epoch:
                last_reset_epoch = cur_session.reset_epoch
                last_seq = 0
            new_events = store.get_session_events(
                session.session_id,
                limit=500,
                after_sequence=last_seq,
                latest=False,
            )
            for ev in new_events:
                if ev.event_type == "SESSION_RESET":
                    last_seq = 0
                elif ev.sequence_num > last_seq:
                    last_seq = ev.sequence_num
                yield ev
