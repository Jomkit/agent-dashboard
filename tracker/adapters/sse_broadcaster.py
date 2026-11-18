import queue
import threading
from typing import Any, Optional, Set

from tracker.domain.models import NormalizedEvent


class StdlibSSEBroadcaster:
    def __init__(self, queue_maxsize: int = 256):
        self.queue_maxsize = queue_maxsize
        self._lock = threading.Lock()
        self._clients: Set[queue.Queue] = set()
        self._closed = False

    def register_client(self, client_queue: Optional[Any] = None) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("Broadcaster is closed")
            q = client_queue if client_queue is not None else queue.Queue(maxsize=self.queue_maxsize)
            self._clients.add(q)
            return q

    def unregister_client(self, client_queue: Any) -> None:
        with self._lock:
            self._clients.discard(client_queue)

    def broadcast(self, event: NormalizedEvent) -> None:
        with self._lock:
            if self._closed:
                return
            evicted: Set[queue.Queue] = set()
            for q in list(self._clients):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # Slow client eviction: drain queue and push None sentinel
                    evicted.add(q)

            for q in evicted:
                self._clients.discard(q)
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for q in self._clients:
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._clients.clear()
