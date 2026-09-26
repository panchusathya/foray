"""The worker was silent in production: `logging.basicConfig` ran only in
`main.py`, which uvicorn imports and the CLI does not."""

from __future__ import annotations

import logging

from juicebox.cli import _configure_logging


def _clear():
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


class TestCliLogging:
    def test_installs_a_handler_when_there_is_none(self):
        _clear()
        try:
            _configure_logging()
            assert logging.getLogger().handlers, "the CLI must install a root handler"
            assert logging.getLogger("juicebox.worker").isEnabledFor(logging.INFO)
        finally:
            _clear()
