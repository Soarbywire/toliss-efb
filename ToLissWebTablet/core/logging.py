from __future__ import annotations

import logging
import queue
import traceback
from typing import Callable


class MainThreadLogger:
    def __init__(self, name: str = "ToLissEFB") -> None:
        self._logger = logging.getLogger(name)
        self._pending: queue.Queue[str] = queue.Queue()

    def xplane(self, message: object) -> None:
        text = str(message)
        self._logger.info(text.rstrip())
        self._pending.put(text if text.endswith("\n") else text + "\n")

    def info(self, message: object) -> None:
        self.xplane(message)

    def warning(self, message: object) -> None:
        self._logger.warning(str(message))
        self.xplane(message)

    def error(self, message: object) -> None:
        self._logger.error(str(message))
        self.xplane(message)

    def exception(self, message: object) -> None:
        detail = f"{message}: {traceback.format_exc().strip()}"
        self._logger.error(detail)
        self.xplane(detail)

    def drain_to_xplane(self, sink: Callable[[str], None], limit: int = 200) -> int:
        """Must be called on the X-Plane main thread."""
        count = 0
        while count < limit:
            try:
                message = self._pending.get_nowait()
            except queue.Empty:
                break
            sink(message)
            count += 1
        return count

