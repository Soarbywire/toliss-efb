from __future__ import annotations

from dataclasses import dataclass
import threading

from .bridge import SimBridge
from .config import ConfigStore
from .logging import MainThreadLogger


@dataclass(slots=True)
class AppContext:
    bridge: SimBridge
    config: ConfigStore
    log: MainThreadLogger
    stop_event: threading.Event

