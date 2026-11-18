from typing import Any, Protocol
from tracker.domain.models import NormalizedEvent


class EventBroadcasterPort(Protocol):
    def register_client(self, client_queue: Any = None) -> Any:
        """Subscribes (or creates and registers) a bounded client queue (maxsize=256) for events."""
        ...

    def unregister_client(self, client_queue: Any) -> None:
        """Unsubscribes a client queue."""
        ...

    def broadcast(self, event: NormalizedEvent) -> None:
        """Fan-outs an event to all registered clients. On queue overflow, evicts slow client cleanly."""
        ...

    def shutdown(self) -> None:
        """Signals all registered client queues with a termination sentinel and closes broadcaster."""
        ...
