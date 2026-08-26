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

    def test_does_not_stomp_an_existing_handler(self):
        _clear()
        try:
            existing = logging.StreamHandler()
            logging.getLogger().addHandler(existing)
            _configure_logging()
            assert logging.getLogger().handlers == [existing]
        finally:
            _clear()

    def test_log_level_env_is_honoured(self, monkeypatch):
        _clear()
        try:
            monkeypatch.setenv("LOG_LEVEL", "WARNING")
            _configure_logging()
            assert not logging.getLogger("juicebox.worker").isEnabledFor(logging.INFO)
            assert logging.getLogger("juicebox.worker").isEnabledFor(logging.WARNING)
        finally:
            _clear()
