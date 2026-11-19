# Desktop App Interface (Phase 2)

This directory defines the Phase 2 desktop app wrapper.

## Architecture
- Embeds or connects to the local `DashboardServer(host="127.0.0.1", port=0)`.
- Implements `DesktopShellPort` (`tracker/interfaces/desktop/__init__.py`).
- Wraps the UI into a native OS window (via `pywebview` or lightweight native webview) without browser chrome.
