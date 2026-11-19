import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import List, Optional, Sequence, TextIO

from tracker.adapters.antigravity_adapter import AntigravityTranscriptAdapter
from tracker.adapters.sqlite_store import SqliteSessionStore
from tracker.adapters.sse_broadcaster import StdlibSSEBroadcaster
from tracker.app.engine import Engine
from tracker.domain.models import SessionStatus
from tracker.interfaces.cli.client import (
    AmbiguousSessionError,
    CliClientError,
    DashboardClient,
    SessionNotFoundError,
)
from tracker.interfaces.cli.formatter import (
    format_event,
    format_session_detail,
    format_table,
    should_use_color,
)
from tracker.interfaces.web.http_server import DashboardServer

DEFAULT_URL = (
    os.environ.get("AGENT_DASHBOARD_URL")
    or os.environ.get("AGENT_TRACKER_URL")
    or "http://127.0.0.1:8080"
)
KNOWN_SUBCOMMANDS = {"serve", "list", "tail", "inspect"}


def _add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="HTTP port to bind (default: 8080, 0 for ephemeral)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=str(Path.home() / ".local" / "share" / "agent-dashboard" / "dashboard.db"),
        help="Path to SQLite database file",
    )
    parser.add_argument(
        "--brain-dir",
        type=str,
        default=str(Path.home() / ".gemini" / "antigravity-cli" / "brain"),
        help="Path to Antigravity brain sessions directory",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds for engine (default: 0.5)",
    )


def _run_serve(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path).expanduser().resolve()
    brain_dir = Path(args.brain_dir).expanduser().resolve()

    store = SqliteSessionStore(db_path=str(db_path))
    broadcaster = StdlibSSEBroadcaster()
    adapter = AntigravityTranscriptAdapter(base_dir=brain_dir)
    engine = Engine(
        adapter=adapter,
        store=store,
        broadcaster=broadcaster,
        poll_interval=args.poll_interval,
    )
    server = DashboardServer(
        host=args.host,
        port=args.port,
        store=store,
        broadcaster=broadcaster,
    )

    running = True

    def _shutdown_handler(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    print(f"Starting Agent Dashboard on http://{args.host}:{server.port}...")
    print(f"Monitoring brain directory: {brain_dir}")
    print(f"Database: {db_path}")

    engine.start()
    server_thread = threading.Thread(target=server.start, daemon=True)
    server_thread.start()

    try:
        while running:
            time.sleep(0.5)
    finally:
        print("\nShutting down Agent Dashboard...")
        server.stop()
        engine.stop()
        broadcaster.shutdown()
        store.close()
        print("Shutdown complete.")
    return 0


def _preprocess_argv(argv: Sequence[str]) -> List[str]:
    """Preserves `python3 -m tracker [--port ...]` defaulting to `serve` when no subcommand is present."""
    args_list = list(argv)
    if not args_list:
        return ["serve"]
    if any(cmd in args_list for cmd in KNOWN_SUBCOMMANDS) or "-h" in args_list or "--help" in args_list:
        return args_list
    # No subcommand present (e.g. `python3 -m tracker --port 8080`)
    return ["serve", *args_list]


def build_parser() -> argparse.ArgumentParser:
    # Root common parser sets the defaults
    common_root = argparse.ArgumentParser(add_help=False)
    common_root.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Dashboard daemon base URL (default: {DEFAULT_URL})",
    )
    common_root.add_argument(
        "--db",
        default=None,
        help="SQLite DB path override when daemon is offline",
    )
    common_root.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="Disable ANSI color output",
    )

    # Subparser common parser suppresses defaults so pre-subcommand values are not overwritten
    common_sub = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common_sub.add_argument(
        "--url",
        help=f"Dashboard daemon base URL (default: {DEFAULT_URL})",
    )
    common_sub.add_argument(
        "--db",
        help="SQLite DB path override when daemon is offline",
    )
    common_sub.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output",
    )

    parser = argparse.ArgumentParser(
        prog="tracker",
        description="Agent Dashboard Observer & CLI",
        parents=[common_root],
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run the dashboard ingestion engine and HTTP/SSE server", parents=[common_sub])
    _add_serve_arguments(p_serve)
    p_serve.set_defaults(func=_run_serve)

    p_list = sub.add_parser("list", help="List tracked agent sessions", parents=[common_sub])
    p_list.add_argument("--status", type=str.upper, choices=[s.value for s in SessionStatus], default=None, help="Filter by status")
    p_list.add_argument("--limit", type=int, default=None, help="Maximum sessions to display")
    p_list.add_argument("--json", action="store_true", help="Output raw JSON")

    p_inspect = sub.add_parser("inspect", help="Inspect session metadata and recent events", parents=[common_sub])
    p_inspect.add_argument("session_id", help="Session ID or unambiguous prefix")
    p_inspect.add_argument("--limit", type=int, default=50, help="Number of recent events to fetch (default: 50, 0 for all)")
    p_inspect.add_argument("--json", action="store_true", help="Output raw JSON")

    p_tail = sub.add_parser("tail", help="Tail session transcript events", parents=[common_sub])
    p_tail.add_argument("session_id", help="Session ID or unambiguous prefix")
    p_tail.add_argument("--limit", type=int, default=20, help="Initial history events to display")
    p_tail.add_argument("--no-follow", action="store_true", default=False, help="Print recent history and exit without following")
    p_tail.add_argument("--json", action="store_true", help="Emit newline-delimited JSON")

    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    client: Optional[DashboardClient] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    normalized = _preprocess_argv(raw_argv)

    parser = build_parser()
    try:
        args = parser.parse_args(normalized)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    if args.command == "serve":
        return _run_serve(args)

    use_color = False if args.no_color else should_use_color(stream=out)
    owns_client = client is None
    active_client = client or DashboardClient(base_url=args.url, db_path=args.db)

    try:
        if args.command == "list":
            status_enum = SessionStatus(args.status) if args.status else None
            sessions = active_client.list_sessions(status=status_enum, limit=args.limit)
            if args.json:
                data = [
                    {
                        "session_id": s.session_id,
                        "model_name": s.model_name,
                        "status": s.status.value,
                        "tokens": {
                            "input_tokens": s.tokens.input_tokens,
                            "output_tokens": s.tokens.output_tokens,
                            "cache_read_tokens": s.tokens.cache_read_tokens,
                        },
                        "started_at": s.started_at,
                    }
                    for s in sessions
                ]
                print(json.dumps(data, indent=2), file=out)
            else:
                print(format_table(sessions, color=use_color), file=out)
            return 0

        if args.command == "inspect":
            session = active_client.resolve_session(args.session_id)
            if args.limit and args.limit > 0:
                events = active_client.get_session_events(session.session_id, limit=args.limit, latest=True)
            else:
                events = active_client.get_all_session_events(session.session_id)

            if args.json:
                payload = {
                    "session_id": session.session_id,
                    "model_name": session.model_name,
                    "status": session.status.value,
                    "working_dir": session.working_dir,
                    "started_at": session.started_at,
                    "tokens": {
                        "input_tokens": session.tokens.input_tokens,
                        "output_tokens": session.tokens.output_tokens,
                        "cache_read_tokens": session.tokens.cache_read_tokens,
                    },
                    "events": [
                        {
                            "sequence_num": e.sequence_num,
                            "event_type": e.event_type,
                            "payload": e.payload,
                        }
                        for e in events
                    ],
                }
                print(json.dumps(payload, indent=2), file=out)
            else:
                print(format_session_detail(session, color=use_color), file=out)
                if events:
                    print("", file=out)
                    for ev in events:
                        print(format_event(ev, color=use_color, max_body_chars=None), file=out)
                        print("", file=out)
            return 0

        if args.command == "tail":
            follow = not args.no_follow
            for ev in active_client.tail_events(args.session_id, follow=follow, initial_limit=args.limit):
                if args.json:
                    item = {
                        "session_id": ev.session_id,
                        "sequence_num": ev.sequence_num,
                        "event_type": ev.event_type,
                        "payload": ev.payload,
                    }
                    print(json.dumps(item), file=out, flush=True)
                else:
                    print(format_event(ev, color=use_color), file=out, flush=True)
                    print("", file=out, flush=True)
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2

    except CliClientError as exc:
        print(f"Error: {exc}", file=err)
        return 1
    except KeyboardInterrupt:
        print("", file=out)
        return 130
    finally:
        if owns_client:
            active_client.close()
