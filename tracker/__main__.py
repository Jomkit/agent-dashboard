import argparse
import os
from pathlib import Path
import signal
import sys
import time
from typing import Optional

from tracker.adapters.antigravity_adapter import AntigravityTranscriptAdapter
from tracker.adapters.http_server import DashboardServer
from tracker.adapters.sqlite_store import SqliteSessionStore
from tracker.adapters.sse_broadcaster import StdlibSSEBroadcaster
from tracker.app.engine import Engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Dashboard Observer")
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

    args = parser.parse_args()

    # Expand user paths
    db_path = Path(args.db_path).expanduser().resolve()
    brain_dir = Path(args.brain_dir).expanduser().resolve()

    # Initialize ports & adapters
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
    import threading
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


if __name__ == "__main__":
    main()
