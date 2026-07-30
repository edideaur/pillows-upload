"""Structured (JSON) and human logging configuration helpers."""

# Copyright (c) 2024 pillows-upload contributors
# SPDX-License-Identifier: MIT

from __future__ import annotations

import datetime
import json
import logging
import os
import sys

JSON_LOG_ENV = "PILLOWS_JSON_LOG"

_installed_handlers: list[logging.Handler] = []


class _JSONFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Surface structured extras passed via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key.startswith("x_"):
                payload[key[2:]] = value
        if record.exc_info and record.exc_text is None:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int, *, json_log: bool = False) -> None:
    """Install a single root handler at ``level``.

    Plain text uses ``%(message)s``; when ``json_log`` is true each record is
    emitted as a JSON object on stderr. Idempotent: only our own prior handler
    is replaced, so external handlers (e.g. pytest's caplog) are preserved.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    root = logging.getLogger()
    for handler in list(_installed_handlers):
        root.removeHandler(handler)
        handler.close()
        _installed_handlers.remove(handler)
    handler = logging.StreamHandler(sys.stderr)
    if json_log:
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    _installed_handlers.append(handler)
    root.setLevel(level)


def configure_cli_logging(*, quiet: bool, verbose: bool, json_log: bool) -> None:
    """Configure logging from CLI verbosity/json flags."""
    level = logging.ERROR if quiet else logging.DEBUG if verbose else logging.INFO
    configure_logging(level, json_log=json_log)
