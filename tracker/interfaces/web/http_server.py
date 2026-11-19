from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import re
from typing import Any, Optional
import urllib.parse

from tracker.domain.models import AgentSession, NormalizedEvent

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


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


def _event_to_dict(e: NormalizedEvent) -> dict:
    return {
        "event_id": e.event_id,
        "session_id": e.session_id,
        "event_type": e.event_type,
        "timestamp": e.timestamp,
        "payload": e.payload,
        "sequence_num": e.sequence_num,
    }


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # Suppress stdlib request logging during automated test suite runs
        pass

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Route: GET /api/health
        if path == "/api/health":
            self._send_json(200, {"status": "ok"})
            return

        # Route: GET /api/sessions
        if path == "/api/sessions":
            sessions = self.server.store.list_sessions()
            self._send_json(200, [_session_to_dict(s) for s in sessions])
            return

        # Route: GET /api/sessions/<session_id>/events
        session_events_match = re.match(r"^/api/sessions/([^/]+)/events$", path)
        if session_events_match:
            session_id = session_events_match.group(1)
            session = self.server.store.get_session(session_id)
            if session is None:
                self.send_error(404, "Session not found")
                return

            try:
                after_sequence = int(query.get("after_sequence", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
                latest_val = query.get("latest", ["false"])[0].lower()
                latest = latest_val in ("true", "1", "yes")
            except (ValueError, IndexError):
                self.send_error(400, "Invalid query parameters")
                return

            events = self.server.store.get_session_events(
                session_id,
                limit=limit,
                after_sequence=after_sequence,
                latest=latest,
            )
            self._send_json(200, [_event_to_dict(e) for e in events])
            return

        # Route: GET /api/events (SSE)
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            client_q = self.server.broadcaster.register_client()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event = client_q.get(timeout=self.server.ping_interval)
                        if event is None:
                            break
                        payload_str = json.dumps(event.payload)
                        frame = (
                            f"id: {event.session_id}:{event.sequence_num}\n"
                            f"event: {event.event_type}\n"
                            f"data: {payload_str}\n\n"
                        )
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.server.broadcaster.unregister_client(client_q)
            return

        # Route: GET /
        if path == "/" or path == "/index.html":
            index_path = self.server.web_dir / "index.html"
            if index_path.is_file():
                content = index_path.read_bytes()
            else:
                content = b"<!DOCTYPE html><html><body>Agent Dashboard</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return

        # Route: GET /static/*
        if path.startswith("/static/"):
            rel_path = path[len("/static/"):]
            static_root = (self.server.web_dir / "static").resolve()
            target_path = (static_root / rel_path).resolve()

            if not target_path.is_file() or not target_path.is_relative_to(static_root):
                self.send_error(404, "Static file not found")
                return

            mime_type = MIME_TYPES.get(target_path.suffix.lower(), "application/octet-stream")
            content = target_path.read_bytes()

            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return

        # Default 404
        self.send_error(404, "Not Found")


class DashboardServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        store: Any = None,
        broadcaster: Any = None,
        web_dir: Optional[Path] = None,
        ping_interval: float = 15.0,
    ):
        self.host = host
        self.store = store
        self.broadcaster = broadcaster
        self.web_dir = Path(web_dir) if web_dir is not None else Path(__file__).resolve().parent
        self.ping_interval = ping_interval

        self._server = ThreadingHTTPServer((self.host, port), DashboardRequestHandler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._server.store = self.store
        self._server.broadcaster = self.broadcaster
        self._server.web_dir = self.web_dir
        self._server.ping_interval = self.ping_interval

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._server.serve_forever(poll_interval=0.005)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
