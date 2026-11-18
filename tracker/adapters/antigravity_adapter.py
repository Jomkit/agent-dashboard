import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from tracker.domain.models import (
    AgentSession,
    IngestCursor,
    NormalizedEvent,
    SessionStatus,
    TailBatch,
    TokenMetrics,
)

MAX_LINE_BYTES = 1048576  # 1 MiB
HEADER_HASH_BYTES = 256
_SESSION_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AntigravityTranscriptAdapter:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).resolve()

    def discover_sessions(self) -> List[AgentSession]:
        if not self.base_dir.exists():
            return []

        discovered: Dict[str, AgentSession] = {}
        child_to_parent: Dict[str, str] = {}

        # Pass 1: Discover session roots
        for entry in self.base_dir.iterdir():
            if not entry.is_dir():
                continue
            if not _SESSION_ID_REGEX.match(entry.name):
                continue

            sys_gen = entry / ".system_generated"
            transcript_file = sys_gen / "logs" / "transcript.jsonl"
            # Require marker directory or transcript file
            if not sys_gen.is_dir() and not transcript_file.exists():
                continue

            session_id = entry.name
            discovered[session_id] = AgentSession(
                session_id=session_id,
                parent_session_id=None,
                agent_type="AGY",
                model_name="gemini",
                working_dir=str(entry),
                started_at="",
            )

        # Pass 2: Link subagents from .system_generated/subagents/*.json
        for session_id in list(discovered.keys()):
            subagents_dir = self.base_dir / session_id / ".system_generated" / "subagents"
            if not subagents_dir.is_dir():
                continue
            for sub_file in subagents_dir.glob("*.json"):
                try:
                    with open(sub_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        child_id = data.get("conversationId")
                        if child_id and child_id in discovered:
                            child_to_parent[child_id] = session_id
                except (OSError, json.JSONDecodeError):
                    continue

        # Apply parent linkages
        result: List[AgentSession] = []
        for session_id, session in discovered.items():
            parent_id = child_to_parent.get(session_id)
            if parent_id:
                from dataclasses import replace
                session = replace(session, parent_session_id=parent_id)
            result.append(session)

        return result

    def tail_session(self, session_id: str, cursor: Optional[IngestCursor] = None) -> TailBatch:
        if not _SESSION_ID_REGEX.match(session_id):
            raise ValueError(f"Invalid session_id: {session_id}")

        session_path = (self.base_dir / session_id).resolve()
        if not session_path.is_relative_to(self.base_dir):
            raise ValueError(f"Path traversal detected: {session_id}")

        transcript_path = session_path / ".system_generated" / "logs" / "transcript.jsonl"
        if not transcript_path.exists():
            default_cursor = cursor or IngestCursor(session_id=session_id, dev=0, ino=0, byte_offset=0, last_sequence_num=0)
            return TailBatch(cursor=default_cursor, events=[], reset_detected=False)

        stat = os.stat(transcript_path)
        new_header_hash = ""
        if stat.st_size >= HEADER_HASH_BYTES:
            with open(transcript_path, "rb") as f:
                new_header_hash = hashlib.sha256(f.read(HEADER_HASH_BYTES)).hexdigest()

        # Check for truncation or inode swap
        reset_detected = False
        if cursor is not None:
            if cursor.dev != 0 or cursor.ino != 0:
                dev_ino_changed = (stat.st_dev, stat.st_ino) != (cursor.dev, cursor.ino)
                file_truncated = stat.st_size < cursor.byte_offset
                hash_mismatched = bool(cursor.header_hash and new_header_hash and cursor.header_hash != new_header_hash)
                if dev_ino_changed or file_truncated or hash_mismatched:
                    reset_detected = True

        if reset_detected:
            current_offset = 0
            next_seq = 1
        else:
            current_offset = cursor.byte_offset if cursor else 0
            next_seq = (cursor.last_sequence_num if cursor else 0) + 1

        events: List[NormalizedEvent] = []
        with open(transcript_path, "rb") as f:
            f.seek(current_offset)
            while True:
                line_bytes = f.readline(MAX_LINE_BYTES + 1)
                if not line_bytes:
                    break

                # If incomplete trailing line without \n at EOF, do not advance offset
                if not line_bytes.endswith(b"\n"):
                    if len(line_bytes) <= MAX_LINE_BYTES:
                        break  # Incomplete line mid-write; wait for next poll
                    else:
                        # Oversized line > MAX_LINE_BYTES: scan forward in 64 KiB chunks for \n
                        found_newline = False
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            nl_idx = chunk.find(b"\n")
                            if nl_idx != -1:
                                found_newline = True
                                # Seek backward to byte immediately following \n
                                f.seek(f.tell() - len(chunk) + nl_idx + 1)
                                break
                        if not found_newline:
                            break  # Hit EOF without newline; keep previous offset

                        events.append(
                            NormalizedEvent(
                                event_id=f"warn-{session_id}-{next_seq}",
                                session_id=session_id,
                                event_type="PARSE_WARNING",
                                timestamp="",
                                payload={"warning": "Oversized line exceeded 1 MiB"},
                                sequence_num=next_seq,
                            )
                        )
                        next_seq += 1
                        current_offset = f.tell()
                        continue

                current_offset = f.tell()

                try:
                    obj = json.loads(line_bytes.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    events.append(
                        NormalizedEvent(
                            event_id=f"warn-{session_id}-{next_seq}",
                            session_id=session_id,
                            event_type="PARSE_WARNING",
                            timestamp="",
                            payload={"warning": "Malformed JSON in transcript line"},
                            sequence_num=next_seq,
                        )
                    )
                    next_seq += 1
                    continue

                step_type = obj.get("type")
                created_at = obj.get("created_at", "")
                step_idx = obj.get("step_index", 0)

                if step_type == "USER_INPUT":
                    events.append(
                        NormalizedEvent(
                            event_id=f"{step_idx}-input",
                            session_id=session_id,
                            event_type="USER_PROMPT",
                            timestamp=created_at,
                            payload={"content": obj.get("content", "")},
                            sequence_num=next_seq,
                        )
                    )
                    next_seq += 1

                elif step_type == "PLANNER_RESPONSE":
                    # Emit THINKING with token metrics
                    events.append(
                        NormalizedEvent(
                            event_id=f"{step_idx}-think",
                            session_id=session_id,
                            event_type="THINKING",
                            timestamp=created_at,
                            payload={
                                "model_name": obj.get("model", ""),
                                "thinking": obj.get("thinking", ""),
                                "input_tokens": obj.get("input_tokens", 0),
                                "output_tokens": obj.get("output_tokens", 0),
                                "cache_read_tokens": obj.get("cache_read_tokens", 0),
                            },
                            sequence_num=next_seq,
                        )
                    )
                    next_seq += 1

                    # Emit ASSISTANT_MESSAGE if text content is present
                    if obj.get("content"):
                        events.append(
                            NormalizedEvent(
                                event_id=f"{step_idx}-msg",
                                session_id=session_id,
                                event_type="ASSISTANT_MESSAGE",
                                timestamp=created_at,
                                payload={"content": obj.get("content")},
                                sequence_num=next_seq,
                            )
                        )
                        next_seq += 1

                    # Emit TOOL_CALL for each tool call
                    tool_calls = obj.get("tool_calls", [])
                    for i, tc in enumerate(tool_calls):
                        events.append(
                            NormalizedEvent(
                                event_id=f"{step_idx}-tc-{i}",
                                session_id=session_id,
                                event_type="TOOL_CALL",
                                timestamp=created_at,
                                payload={"tool_name": tc.get("name"), "args": tc.get("args")},
                                sequence_num=next_seq,
                            )
                        )
                        next_seq += 1

                    # If no tool calls, model is waiting for user
                    if not tool_calls:
                        events.append(
                            NormalizedEvent(
                                event_id=f"{step_idx}-wait",
                                session_id=session_id,
                                event_type="STATE_CHANGE",
                                timestamp=created_at,
                                payload={"status": SessionStatus.WAITING_USER.value},
                                sequence_num=next_seq,
                            )
                        )
                        next_seq += 1

                elif step_type == "GENERIC":
                    is_done = obj.get("status") == "DONE"
                    events.append(
                        NormalizedEvent(
                            event_id=f"{step_idx}-result",
                            session_id=session_id,
                            event_type="TOOL_RESULT",
                            timestamp=created_at,
                            payload={"status": obj.get("status"), "content": obj.get("content")},
                            sequence_num=next_seq,
                        )
                    )
                    next_seq += 1

                    if not is_done:
                        events.append(
                            NormalizedEvent(
                                event_id=f"{step_idx}-err",
                                session_id=session_id,
                                event_type="STATE_CHANGE",
                                timestamp=created_at,
                                payload={"status": SessionStatus.ERROR.value},
                                sequence_num=next_seq,
                            )
                        )
                        next_seq += 1

        new_cursor = IngestCursor(
            session_id=session_id,
            dev=stat.st_dev,
            ino=stat.st_ino,
            byte_offset=current_offset,
            header_hash=new_header_hash,
            last_sequence_num=next_seq - 1 if (events or not reset_detected) else 0,
        )

        return TailBatch(cursor=new_cursor, events=events, reset_detected=reset_detected)
