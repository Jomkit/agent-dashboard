"""Phase 2 Desktop Interface Stub & Protocols (100% Python stdlib)."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DesktopWindowConfig:
    title: str = "Agent Dashboard"
    width: int = 1280
    height: int = 840
    host: str = "127.0.0.1"
    port: int = 0  # 0 binds to ephemeral port when embedded


class DesktopShellPort(Protocol):
    """Protocol contract for Phase 2 native desktop window runners."""

    def launch(self, url: str, config: DesktopWindowConfig) -> int:
        ...
