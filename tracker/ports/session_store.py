from typing import List, Optional, Protocol
from tracker.domain.models import AgentSession, IngestCursor, NormalizedEvent


class SessionStorePort(Protocol):
    def upsert_session(self, session: AgentSession) -> None:
        """Authoritatively stores or updates the reduced AgentSession snapshot."""
        ...

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        ...

    def list_sessions(self) -> List[AgentSession]:
        """Returns all sessions ordered by started_at DESC."""
        ...

    def commit_ingest_batch(
        self,
        session: AgentSession,
        events: List[NormalizedEvent],
        cursor: IngestCursor,
        reset_existing: bool = False,
    ) -> None:
        """Atomically commits session snapshot, batch inserts events (purging existing events if
        reset_existing is True), and commits cursor in a single database transaction."""
        ...

    def get_session_events(
        self,
        session_id: str,
        limit: int = 500,
        after_sequence: int = 0,
        latest: bool = False,
    ) -> List[NormalizedEvent]:
        """Returns events with sequence_num > after_sequence in ascending sequence order.
        When latest=True (and after_sequence=0), returns the most recent limit events in ascending order."""
        ...

    def get_cursor(self, session_id: str) -> Optional[IngestCursor]:
        ...

    def close(self) -> None:
        ...
