"""Structured JSON logging to stdout, plus a heartbeat helper so a multi-day
harvest is observable (a line every 60s with live counts).
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from typing import Any

import structlog

_configured = False


def configure() -> None:
    global _configured
    if _configured:
        return
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> Any:
    configure()
    return structlog.get_logger(name)


class Heartbeat:
    """Emit ``log.info("heartbeat", **counts())`` every ``interval`` seconds.

    Use as a context manager around a long-running loop::

        with Heartbeat(log, lambda: {"processed": n}):
            ...
    """

    def __init__(
        self,
        log: Any,
        counts: Callable[[], dict[str, Any]],
        interval: float = 60.0,
    ) -> None:
        self._log = log
        self._counts = counts
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "Heartbeat":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._log.info("heartbeat", **self._counts())
            except Exception:  # noqa: BLE001 - a heartbeat must never crash the job
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
