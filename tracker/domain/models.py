from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionStatus(str, Enum):
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class TokenMetrics:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True)
class AgentSession:
    session_id: str
    parent_session_id: Optional[str]
    agent_type: str  # "AGY", "CLAUDE", "CODEX"
    model_name: str
    working_dir: str
    started_at: str  # ISO-8601 UTC
    ended_at: Optional[str] = None
    status: SessionStatus = SessionStatus.INITIALIZING
    tokens: TokenMetrics = field(default_factory=TokenMetrics)
    metadata: Dict[str, Any] = field(default_factory=dict)
    reset_epoch: int = 0  # Monotonically increments on log truncation to invalidate client caches


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    session_id: str
    event_type: str  # "SESSION_INIT", "USER_PROMPT", "THINKING", "ASSISTANT_MESSAGE", "TOOL_CALL", "TOOL_RESULT", "STATE_CHANGE", "SESSION_RESET", "PARSE_WARNING"
    timestamp: str
    payload: Dict[str, Any]
    sequence_num: int = 0  # 1-based monotonic counter per session (0 = unpersisted synthetic event)


@dataclass(frozen=True)
class IngestCursor:
    session_id: str
    dev: int
    ino: int
    byte_offset: int
    header_hash: str = ""
    last_sequence_num: int = 0  # Highest sequence_num committed for this session


@dataclass(frozen=True)
class TailBatch:
    cursor: IngestCursor
    events: List[NormalizedEvent]
    reset_detected: bool = False  # Set True when file truncation/inode swap occurs


def merge_discovered_session(existing: Optional[AgentSession], discovered: AgentSession) -> AgentSession:
    """Preserves reduced runtime state while updating structural discovery fields."""
    if existing is None:
        return discovered
    return replace(
        existing,
        parent_session_id=discovered.parent_session_id or existing.parent_session_id,
        model_name=existing.model_name or discovered.model_name,
        working_dir=existing.working_dir or discovered.working_dir,
        started_at=existing.started_at or discovered.started_at,
    )


def reset_session_state(session: AgentSession) -> AgentSession:
    """Resets session metrics and status on log truncation, incrementing reset_epoch."""
    return replace(
        session,
        status=SessionStatus.INITIALIZING,
        tokens=TokenMetrics(),
        ended_at=None,
        reset_epoch=session.reset_epoch + 1,
    )


def apply_events(session: AgentSession, events: List[NormalizedEvent]) -> AgentSession:
    """Pure domain reducer: updates session state and token metrics based on events."""
    cur = session
    for event in events:
        if event.session_id != cur.session_id:
            raise ValueError(f"Event session_id {event.session_id} != {cur.session_id}")

        if event.event_type == "SESSION_INIT":
            model_name = str(event.payload.get("model_name") or cur.model_name)
            working_dir = str(event.payload.get("working_dir") or cur.working_dir)
            cur = replace(cur, model_name=model_name, working_dir=working_dir, status=SessionStatus.RUNNING, ended_at=None)

        elif event.event_type in ("USER_PROMPT", "TOOL_CALL", "TOOL_RESULT", "ASSISTANT_MESSAGE"):
            cur = replace(cur, status=SessionStatus.RUNNING, ended_at=None)

        elif event.event_type == "THINKING":
            delta_tokens = event.payload.get("delta_tokens") or {}
            delta_in = int(event.payload.get("input_tokens") or delta_tokens.get("input_tokens") or 0)
            delta_out = int(event.payload.get("output_tokens") or delta_tokens.get("output_tokens") or 0)
            delta_cache = int(event.payload.get("cache_read_tokens") or delta_tokens.get("cache_read_tokens") or 0)
            tokens = cur.tokens
            if delta_in or delta_out or delta_cache:
                tokens = TokenMetrics(
                    input_tokens=cur.tokens.input_tokens + delta_in,
                    output_tokens=cur.tokens.output_tokens + delta_out,
                    cache_read_tokens=cur.tokens.cache_read_tokens + delta_cache,
                )
            cur = replace(cur, status=SessionStatus.RUNNING, ended_at=None, tokens=tokens)

        elif event.event_type == "STATE_CHANGE":
            new_status = event.payload.get("status")
            if isinstance(new_status, str) and new_status in SessionStatus.__members__:
                status_enum = SessionStatus[new_status]
                ended_at = event.timestamp if status_enum in (SessionStatus.COMPLETED, SessionStatus.ERROR) else None
                cur = replace(cur, status=status_enum, ended_at=ended_at)

        elif event.event_type == "SESSION_RESET":
            cur = reset_session_state(cur)

    return cur
