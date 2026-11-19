from datetime import datetime
import json
import os
import sys
from typing import Any, Dict, Mapping, Optional, Sequence, TextIO

from tracker.domain.models import AgentSession, NormalizedEvent, SessionStatus

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_STATUS_COLORS = {
    SessionStatus.INITIALIZING: "\033[36m",    # Cyan
    SessionStatus.RUNNING: "\033[1;32m",       # Bold Green
    SessionStatus.WAITING_USER: "\033[1;33m",  # Bold Yellow
    SessionStatus.COMPLETED: "\033[90m",       # Gray
    SessionStatus.ERROR: "\033[1;31m",         # Bold Red
}

_EVENT_COLORS = {
    "USER_PROMPT": "\033[1;36m",        # Bold Cyan
    "THINKING": "\033[35m",             # Magenta
    "TOOL_CALL": "\033[1;33m",          # Bold Yellow
    "TOOL_RESULT": "\033[32m",          # Green
    "ASSISTANT_MESSAGE": "\033[1;32m",  # Bold Green
    "SESSION_INIT": "\033[36m",         # Cyan
    "STATE_CHANGE": "\033[34m",         # Blue
    "SESSION_RESET": "\033[1;31m",      # Bold Red
    "PARSE_WARNING": "\033[33m",        # Yellow
}


def should_use_color(
    stream: Optional[TextIO] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR", "") != "":
        return False
    if env.get("TERM", "") == "dumb":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(isatty and isatty())


def _style(text: str, code: str, use_color: bool) -> str:
    if not use_color or not code:
        return text
    return f"{code}{text}{_RESET}"


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0 or len(text) <= max_len:
        return text
    if max_len == 1:
        return "…"
    return text[: max_len - 1] + "…"


def _format_timestamp(ts: str, time_only: bool = False) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S" if time_only else "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts[:19]


def _format_tokens(session: AgentSession) -> str:
    return f"↑{session.tokens.input_tokens:,} ↓{session.tokens.output_tokens:,}"


def format_table(
    sessions: Sequence[AgentSession],
    color: Optional[bool] = None,
) -> str:
    use_color = should_use_color() if color is None else color
    if not sessions:
        return "No sessions found."

    rows = []
    for s in sessions:
        rows.append(
            {
                "id": _truncate(s.session_id, 36),
                "model": _truncate(s.model_name or s.agent_type or "-", 24),
                "status_plain": s.status.value,
                "status_obj": s.status,
                "tokens": _format_tokens(s),
                "started": _format_timestamp(s.started_at, time_only=False) or "-",
            }
        )

    w_id = max(len("ID"), *(len(r["id"]) for r in rows))
    w_model = max(len("Model"), *(len(r["model"]) for r in rows))
    w_status = max(len("Status"), *(len(r["status_plain"]) for r in rows))
    w_tokens = max(len("Tokens"), *(len(r["tokens"]) for r in rows))
    w_started = max(len("Started"), *(len(r["started"]) for r in rows))

    header = (
        f"{'ID':<{w_id}}  "
        f"{'Model':<{w_model}}  "
        f"{'Status':<{w_status}}  "
        f"{'Tokens':<{w_tokens}}  "
        f"{'Started':<{w_started}}"
    )
    divider = f"{'-' * w_id}  {'-' * w_model}  {'-' * w_status}  {'-' * w_tokens}  {'-' * w_started}"
    lines = [
        _style(header, _BOLD, use_color),
        _style(divider, _DIM, use_color),
    ]

    for r in rows:
        # Pre-pad plain status before applying ANSI colors so column alignment is strictly maintained
        status_padded = f"{r['status_plain']:<{w_status}}"
        status_colored = _style(
            status_padded,
            _STATUS_COLORS.get(r["status_obj"], ""),
            use_color,
        )
        lines.append(
            f"{r['id']:<{w_id}}  "
            f"{r['model']:<{w_model}}  "
            f"{status_colored}  "
            f"{r['tokens']:<{w_tokens}}  "
            f"{r['started']:<{w_started}}"
        )

    return "\n".join(lines)


def format_session_detail(
    session: AgentSession,
    color: Optional[bool] = None,
) -> str:
    use_color = should_use_color() if color is None else color
    status_badge = _style(session.status.value, _STATUS_COLORS.get(session.status, ""), use_color)
    lines = [
        _style(f"Session {session.session_id}", _BOLD, use_color),
        f"  Agent / Model : {session.agent_type} / {session.model_name or '-'}",
        f"  Status        : {status_badge}",
        f"  Parent ID     : {session.parent_session_id or '-'}",
        f"  Working Dir   : {session.working_dir or '-'}",
        f"  Started       : {session.started_at or '-'}",
        f"  Ended         : {session.ended_at or '-'}",
        (
            f"  Tokens        : ↑{session.tokens.input_tokens:,} in  "
            f"↓{session.tokens.output_tokens:,} out  "
            f"({session.tokens.cache_read_tokens:,} cache read)"
        ),
        f"  Reset Epoch   : {session.reset_epoch}",
    ]
    return "\n".join(lines)


def _extract_first(payload: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        val = payload.get(key)
        if val is not None and val != "":
            return val
    return None


def _stringify_value(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(_stringify_value(item))
        return "\n".join(p for p in parts if p)
    return json.dumps(val, indent=2, ensure_ascii=False, sort_keys=True)


def _extract_event_meta_and_body(event: NormalizedEvent) -> tuple[str, str, bool]:
    payload = event.payload or {}
    et = event.event_type

    if et in ("USER_PROMPT", "ASSISTANT_MESSAGE"):
        text = _extract_first(payload, ("text", "content", "prompt", "message"))
        body = _stringify_value(text) if text is not None else json.dumps(payload, indent=2, sort_keys=True)
        return "", body, False

    if et == "THINKING":
        delta = payload.get("delta_tokens") or {}
        d_in = int(payload.get("input_tokens") or delta.get("input_tokens") or 0)
        d_out = int(payload.get("output_tokens") or delta.get("output_tokens") or 0)
        d_cache = int(payload.get("cache_read_tokens") or delta.get("cache_read_tokens") or 0)
        token_parts = []
        if d_in:
            token_parts.append(f"+{d_in} in")
        if d_out:
            token_parts.append(f"+{d_out} out")
        if d_cache:
            token_parts.append(f"+{d_cache} cache")
        suffix = f" ({', '.join(token_parts)})" if token_parts else ""
        text = _extract_first(payload, ("text", "thinking", "content", "summary"))
        body = _stringify_value(text)
        if not body and not suffix:
            body = json.dumps(payload, indent=2, sort_keys=True)
        return suffix, body, False

    if et == "TOOL_CALL":
        tool = _extract_first(payload, ("tool_name", "name", "tool")) or "unknown"
        call_id = _extract_first(payload, ("call_id", "tool_use_id", "id"))
        suffix = f" {tool}" + (f" [{call_id}]" if call_id else "")
        args = _extract_first(payload, ("arguments", "input", "args", "params"))
        body = _stringify_value(args) if args is not None else json.dumps(payload, indent=2, sort_keys=True)
        return suffix, body, False

    if et == "TOOL_RESULT":
        tool = _extract_first(payload, ("tool_name", "name", "tool"))
        is_err = bool(payload.get("is_error") or payload.get("error"))
        status_word = "error" if is_err else "ok"
        suffix = f" {tool} ({status_word})" if tool else f" ({status_word})"
        out = _extract_first(payload, ("output", "result", "content", "error", "stderr", "stdout"))
        body = _stringify_value(out) if out is not None else json.dumps(payload, indent=2, sort_keys=True)
        return suffix, body, is_err

    if et == "STATE_CHANGE":
        new_status = payload.get("status", "UNKNOWN")
        return f" -> {new_status}", "", False

    if et == "SESSION_INIT":
        model = payload.get("model_name", "")
        cwd = payload.get("working_dir", "")
        parts = [p for p in (model, cwd) if p]
        return "", " | ".join(parts), False

    return "", json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) if payload else "", False


def format_event(
    event: NormalizedEvent,
    color: Optional[bool] = None,
    max_body_chars: Optional[int] = 4000,
) -> str:
    use_color = should_use_color() if color is None else color
    ts_str = _format_timestamp(event.timestamp, time_only=True)
    ts_prefix = f"[{ts_str}] " if ts_str else ""
    seq_prefix = f"#{event.sequence_num} " if event.sequence_num else ""

    suffix, body, is_err = _extract_event_meta_and_body(event)
    color_code = "\033[1;31m" if is_err else _EVENT_COLORS.get(event.event_type, _BOLD)
    badge = _style(event.event_type, color_code, use_color)
    meta = _style(f"{ts_prefix}{seq_prefix}", _DIM, use_color)
    header = f"{meta}{badge}{suffix}"

    if not body:
        return header

    if max_body_chars is not None and len(body) > max_body_chars:
        omitted = len(body) - max_body_chars
        body = f"{body[:max_body_chars]}\n... [truncated {omitted} chars]"

    bar = _style("│", color_code, use_color)
    body_lines = [f"{bar} {line}" if line else bar for line in body.splitlines()]
    return "\n".join([header, *body_lines])
