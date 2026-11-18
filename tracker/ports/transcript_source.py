from typing import List, Optional, Protocol
from tracker.domain.models import AgentSession, IngestCursor, TailBatch


class TranscriptSourcePort(Protocol):
    def discover_sessions(self) -> List[AgentSession]:
        """Discovers existing and active session directories on disk."""
        ...

    def tail_session(self, session_id: str, cursor: Optional[IngestCursor] = None) -> TailBatch:
        """Tails session from cursor (or byte 0 if None). Must return TailBatch with cursor.last_sequence_num
        advanced to final emitted event sequence_num (or reset to 0 if reset_detected=True, or preserved if empty).
        If transcript file does not exist yet, returns TailBatch(cursor or IngestCursor(session_id, 0, 0, 0), [])."""
        ...
