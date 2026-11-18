# Agent Dashboard

A lightweight, model-agnostic, zero-dependency local command center for tracking concurrent LLM AI coding agent sessions (Antigravity CLI / Gemini AI Ultra, with pluggable contracts for Claude Code, Codex, and Aider).

---

## 🏛️ Architecture: Ports and Adapters (Hexagonal Design)

> **Major Design Guideline**: This project strictly adheres to **Ports and Adapters (Hexagonal Architecture)**. All contributors and future development must preserve this architectural boundary.

```
                           +---------------------------------------+
                           |             Hexagonal Core            |
                           |                                       |
+---------------------+    |  +---------------------------------+  |    +------------------------+
| Antigravity Adapter | -> |  |      TranscriptSourcePort       |  |    |     SQLite Store       |
| (JSONL File Tailer) |    |  +---------------------------------+  | -> | (WAL + Pooled Readers) |
+---------------------+    |                  |                    |    +------------------------+
                           |                  v                    |
+---------------------+    |  +---------------------------------+  |    +------------------------+
| Claude Code Adapter | -> |  |   Domain Models & Pure Reducer  |  |    |   Stdlib Broadcaster   |
|     (Pluggable)     |    |  |  (AgentSession, NormalizedEvent)|  | -> |   (Bounded Fan-Out)    |
+---------------------+    |  +---------------------------------+  |    +------------------------+
                           |                  |                    |
                           |                  v                    |    +------------------------+
                           |  +---------------------------------+  | -> |   HTTP / SSE Server    |
                           |  |      EventBroadcasterPort       |  |    |  (Threaded, Zero-Dep)  |
                           |  +---------------------------------+  |    +------------------------+
                           +---------------------------------------+
```

### Key Principles

1. **Pure Domain Core (`tracker/domain/`)**:
   - Contains immutable data contracts (`AgentSession`, `NormalizedEvent`, `IngestCursor`, `TokenMetrics`) and pure functional state reducers (`apply_events`, `merge_discovered_session`, `reset_session_state`).
   - **Zero external imports**: No imports from `tracker/adapters`, `tracker/app`, database drivers, or networking modules.
2. **Abstract Port Contracts (`tracker/ports/`)**:
   - Explicit `typing.Protocol` interfaces defining contracts for ingestion (`TranscriptSourcePort`), persistence (`SessionStorePort`), and streaming distribution (`EventBroadcasterPort`).
3. **Pluggable Adapters (`tracker/adapters/`)**:
   - `AntigravityTranscriptAdapter`: Discovers sessions and streams events via bounded binary tailing of `transcript.jsonl` with truncation/swap detection.
   - `SqliteSessionStore`: WAL mode, single writer lock, bounded `LifoQueue` reader pool, atomic transactions.
   - `StdlibSSEBroadcaster`: Bounded client queues (`maxsize=256`), slow client eviction, clean shutdown.
   - `DashboardServer`: Python stdlib `http.server.ThreadingHTTPServer` serving REST endpoints, SSE streams, and static assets.
4. **Application Orchestrator (`tracker/app/`)**:
   - `Engine`: Connects any source adapter to any store and broadcaster using deterministic polling cycles (`poll_once()`).

---

## 🚀 Features & Guardrails

- **Zero Third-Party Dependencies**: 100% standard library Python 3.10+ backend (`http.server`, `sqlite3`, `dataclasses`, `queue`, `unittest`). No `fastapi`, `requests`, `pydantic`, or `watchdog`.
- **Zero-Build Minimalist Frontend**: Custom uncompressed stylesheet and native ESM components (~22 KB total payload, under the 30 KB budget). Zero npm, no webpack/vite, no external CDNs.
- **Strict Content Security**: Light-DOM Web Components exclusively use `textContent` and `createElement` DOM nodes. `.innerHTML`, `eval()`, and inline scripts are forbidden.
- **High Performance**: Complete unit and integration test suite (44 tests) executes in `< 200ms` (typically ~150ms).
- **Subagent Hierarchy Resolution**: Automatically tracks parent-child subagent tree and displays nested status in the sidebar.

---

## 📦 Project Structure

```text
agent-dashboard/
├── tracker/
│   ├── __init__.py
│   ├── __main__.py                   # CLI entrypoint
│   ├── domain/                       # Pure core models and reducers
│   │   ├── __init__.py
│   │   └── models.py
│   ├── ports/                        # Protocol contracts
│   │   ├── __init__.py
│   │   ├── transcript_source.py
│   │   ├── session_store.py
│   │   └── broadcaster.py
│   ├── app/                          # Application orchestrator
│   │   ├── __init__.py
│   │   └── engine.py
│   ├── adapters/                     # Concrete I/O implementations
│   │   ├── __init__.py
│   │   ├── antigravity_adapter.py
│   │   ├── sqlite_store.py
│   │   ├── sse_broadcaster.py
│   │   └── http_server.py
│   └── web/                          # Static assets and Web Components
│       ├── index.html
│       └── static/
│           ├── css/
│           │   └── style.css         # Custom semantic CSS (~3.6 KB)
│           └── js/
│               ├── app.js            # Frontend entrypoint
│               ├── store.js          # Reactive EventTarget micro-store
│               ├── sse.js            # SSE stream listener
│               └── components/
│                   ├── session-list.js     # <session-list> Web Component
│                   └── transcript-view.js  # <transcript-view> Web Component
└── tests/
    ├── __init__.py
    ├── test_architecture_guardrails.py   # AST stdlib & hexagonal boundary gate
    ├── test_domain_models.py             # Reducer & domain model tests
    ├── test_sqlite_store.py              # WAL & reader pool tests
    ├── test_antigravity_adapter.py       # Binary tailer & recovery tests
    ├── test_http_server.py               # REST & SSE endpoint integration tests
    ├── test_engine.py                    # Orchestrator lifecycle tests
    └── test_web_ui_contracts.py          # Security & size budget tests
```

---

## 🛠️ Getting Started

### Prerequisites
- Python 3.10 or higher.
- A modern web browser supporting ES modules and EventSource.

### Running the Dashboard

Launch the observer pointing to your local Antigravity brain directory:

```bash
python3 -m tracker --port 8080
```

Custom configuration options:

```bash
python3 -m tracker \
  --port 8080 \
  --host 127.0.0.1 \
  --brain-dir ~/.gemini/antigravity-cli/brain \
  --db-path ~/.local/share/agent-dashboard/dashboard.db \
  --poll-interval 0.5
```

Open your browser to:
```
http://127.0.0.1:8080
```

---

## 🧪 Running the Test Suite

Run the full automated test suite:

```bash
python3 -m unittest discover -s tests -t .
```

Verify architecture guardrails and package boundaries:

```bash
python3 -m unittest tests/test_architecture_guardrails.py
```

Verify frontend security and payload budget (< 30 KB):

```bash
python3 -m unittest tests/test_web_ui_contracts.py
```

---

## 🔌 Adding New Agent Adapters

To add support for a new AI coding agent (e.g. Claude Code, Codex):
1. Implement `TranscriptSourcePort` from `tracker/ports/transcript_source.py`:
   - `discover_sessions() -> List[AgentSession]`
   - `tail_session(session_id: str, cursor: Optional[IngestCursor]) -> TailBatch`
2. Map the agent's log entries to `NormalizedEvent` instances:
   - `USER_PROMPT`, `THINKING`, `TOOL_CALL`, `TOOL_RESULT`, `ASSISTANT_MESSAGE`, `STATE_CHANGE`
3. Wire the new adapter into `tracker/__main__.py` or pass via CLI flag. The rest of the system (SQLite store, SSE broadcaster, Web UI) works without modification.
