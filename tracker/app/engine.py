from dataclasses import asdict, replace
from datetime import datetime, timezone
import threading
import time
from typing import Optional

from tracker.domain.models import (
    AgentSession,
    NormalizedEvent,
    SessionStatus,
    apply_events,
    merge_discovered_session,
    reset_session_state,
)
from tracker.ports.broadcaster import EventBroadcasterPort
from tracker.ports.session_store import SessionStorePort
from tracker.ports.transcript_source import TranscriptSourcePort


def _session_to_dict(s: AgentSession) -> dict:
    return {
        "session_id": s.session_id,
        "parent_session_id": s.parent_session_id,
        "agent_type": s.agent_type,
        "model_name": s.model_name,
        "working_dir": s.working_dir,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "status": s.status.value,
        "tokens": asdict(s.tokens),
        "reset_epoch": s.reset_epoch,
    }


class Engine:
    def __init__(
        self,
        adapter: TranscriptSourcePort,
        store: SessionStorePort,
        broadcaster: EventBroadcasterPort,
        poll_interval: float = 0.5,
    ):
        self.adapter = adapter
        self.store = store
        self.broadcaster = broadcaster
        self.poll_interval = poll_interval
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def poll_once(self) -> None:
        discovered_sessions = self.adapter.discover_sessions()
        discovered_map = {s.session_id: s for s in discovered_sessions}

        stored_sessions = {s.session_id: s for s in self.store.list_sessions()}
        new_session_ids = set()

        for s_id, disc_session in discovered_map.items():
            if s_id in stored_sessions:
                merged = merge_discovered_session(stored_sessions[s_id], disc_session)
            else:
                merged = disc_session
                new_session_ids.add(s_id)
            stored_sessions[s_id] = merged

        # Tail discovered sessions on disk
        for s_id in list(discovered_map.keys()):
            session = stored_sessions[s_id]
            cursor = self.store.get_cursor(s_id)
            batch = self.adapter.tail_session(s_id, cursor=cursor)

            old_session = session
            if batch.reset_detected:
                session = reset_session_state(session)

            if batch.events:
                session = apply_events(session, batch.events)

            needs_commit = (
                (s_id in new_session_ids)
                or batch.reset_detected
                or bool(batch.events)
                or (cursor != batch.cursor)
                or (session != old_session)
            )

            if needs_commit:
                self.store.commit_ingest_batch(
                    session,
                    batch.events,
                    batch.cursor,
                    reset_existing=batch.reset_detected,
                )
                stored_sessions[s_id] = session

                # Post-commit broadcast sequence
                if s_id in new_session_ids:
                    init_event = NormalizedEvent(
                        event_id=f"init-{s_id}",
                        session_id=s_id,
                        event_type="SESSION_INIT",
                        timestamp=session.started_at,
                        payload=_session_to_dict(session),
                        sequence_num=0,
                    )
                    self.broadcaster.broadcast(init_event)

                if batch.reset_detected:
                    reset_event = NormalizedEvent(
                        event_id=f"reset-{s_id}-{session.reset_epoch}",
                        session_id=s_id,
                        event_type="SESSION_RESET",
                        timestamp=session.started_at,
                        payload={"reset_epoch": session.reset_epoch},
                        sequence_num=0,
                    )
                    self.broadcaster.broadcast(reset_event)

                for ev in batch.events:
                    self.broadcaster.broadcast(ev)

                if session != old_session and s_id not in new_session_ids:
                    state_event = NormalizedEvent(
                        event_id=f"state-{s_id}-{int(time.time() * 1000)}",
                        session_id=s_id,
                        event_type="STATE_CHANGE",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        payload=_session_to_dict(session),
                        sequence_num=0,
                    )
                    self.broadcaster.broadcast(state_event)

        # Detect archived/deleted sessions
        for s_id, session in list(stored_sessions.items()):
            if s_id not in discovered_map:
                if session.status in (
                    SessionStatus.INITIALIZING,
                    SessionStatus.RUNNING,
                    SessionStatus.WAITING_USER,
                ):
                    ended_time = datetime.now(timezone.utc).isoformat()
                    completed_session = replace(
                        session,
                        status=SessionStatus.COMPLETED,
                        ended_at=ended_time,
                    )
                    self.store.upsert_session(completed_session)
                    stored_sessions[s_id] = completed_session

                    state_event = NormalizedEvent(
                        event_id=f"archived-{s_id}-{int(time.time() * 1000)}",
                        session_id=s_id,
                        event_type="STATE_CHANGE",
                        timestamp=ended_time,
                        payload=_session_to_dict(completed_session),
                        sequence_num=0,
                    )
                    self.broadcaster.broadcast(state_event)

    def _run_loop(self) -> None:
        while self._running:
            try:
                self.poll_once()
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
