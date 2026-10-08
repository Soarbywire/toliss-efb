from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import queue
import threading
import time
from typing import Any, Callable


class BridgeTimeout(TimeoutError):
    pass


@dataclass(slots=True)
class BridgeRequest:
    request_id: int
    operation: str
    payload: Any = None
    reply: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))
    created_at: float = field(default_factory=time.monotonic)


class SimBridge:
    """A single MPSC request queue consumed exclusively by the flight loop.

    Every synchronous call owns its reply queue.  There are deliberately no
    shared response lanes, so concurrent HTTP requests cannot exchange results.
    """

    def __init__(self) -> None:
        self._requests: queue.Queue[BridgeRequest] = queue.Queue()
        self._ids = itertools.count(1)
        self._closed = threading.Event()

    def call(self, operation: str, payload: Any = None, timeout: float = 1.5) -> Any:
        if self._closed.is_set():
            raise RuntimeError("sim bridge is stopped")
        request = BridgeRequest(next(self._ids), operation, payload)
        self._requests.put(request)
        try:
            ok, value = request.reply.get(timeout=timeout)
        except queue.Empty as exc:
            raise BridgeTimeout(f"X-Plane did not answer {operation!r} within {timeout:.1f}s") from exc
        if ok:
            return value
        raise value

    def submit(self, operation: str, payload: Any = None) -> int:
        """Queue a main-thread action whose result is intentionally ignored."""
        if self._closed.is_set():
            raise RuntimeError("sim bridge is stopped")
        request = BridgeRequest(next(self._ids), operation, payload)
        self._requests.put(request)
        return request.request_id

    def process_pending(self, dispatcher: Callable[[str, Any], Any], limit: int = 256) -> int:
        """Run from the X-Plane flight loop and nowhere else."""
        processed = 0
        while processed < limit:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                break
            try:
                result = dispatcher(request.operation, request.payload)
                answer = (True, result)
            except Exception as exc:  # return the original exception to the caller
                answer = (False, exc)
            try:
                request.reply.put_nowait(answer)
            except queue.Full:
                pass
            processed += 1
        return processed

    def close(self) -> None:
        self._closed.set()
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                break
            try:
                request.reply.put_nowait((False, RuntimeError("sim bridge stopped")))
            except queue.Full:
                pass

