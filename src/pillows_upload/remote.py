"""Best-effort remote ``ls``/``rm`` subcommands for pillows.su.

These hit plausible management endpoints; they are best-effort and will report
a clear error if the server does not expose the operation.
"""

# Copyright (c) 2024 pillows-upload contributors
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import logging
import os

import niquests

from .config import Config
from .constants import BASE_URL, ENV_API_KEY
from .logutils import JSON_LOG_ENV, configure_cli_logging
from .utils import make_session

logger = logging.getLogger(__name__)

BAD_STATUS = 400


def _remote_request(method: str, url: str, *, api_key: str, timeout: int) -> int:
    session = make_session()
    try:
        resp = session.request(
            method,
            url,
            headers={"x-api-key": api_key},
            timeout=timeout,
        )
    except niquests.RequestException:
        logger.exception("Request failed")
        return 1
    finally:
        session.close()

    status_code = int(resp.status_code or 0) if resp is not None else 0
    body = (resp.text or "")[:500] if resp is not None else ""
    if status_code >= BAD_STATUS:
        logger.error("HTTP %d: %s", status_code, body)
        return 1
    logger.info("%s", resp.text)
    return 0


def _cmd_ls(argv: list[str] | None = None) -> int:
    """List remote files (best-effort)."""
    p = argparse.ArgumentParser(prog="pillows-upload ls", description="List remote files.")
    p.add_argument("-k", "--api-key", default=None, help="API key (PILLOWS_KEY)")
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {BASE_URL})")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout (s)")
    p.add_argument("--config", default=None)
    p.add_argument(
        "--json-log",
        action="store_true",
        help="emit structured JSON log lines instead of plain text",
    )
    args = p.parse_args(argv)
    configure_cli_logging(
        quiet=False,
        verbose=False,
        json_log=args.json_log or os.environ.get(JSON_LOG_ENV) == "1",
    )
    config = Config(args.config)
    api_key = args.api_key or os.environ.get(ENV_API_KEY) or config.get("api_key")
    if not api_key:
        logger.error("Error: API key required (set PILLOWS_KEY).")
        return 1
    base = args.base_url or config.get("base_url") or BASE_URL
    return _remote_request("GET", f"{base}/api/files", api_key=api_key, timeout=args.timeout)


def _cmd_rm(argv: list[str] | None = None) -> int:
    """Delete a remote file by id (best-effort)."""
    p = argparse.ArgumentParser(prog="pillows-upload rm", description="Delete a remote file.")
    p.add_argument("file_id", help="remote file id to delete")
    p.add_argument("-k", "--api-key", default=None, help="API key (PILLOWS_KEY)")
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {BASE_URL})")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout (s)")
    p.add_argument("--config", default=None)
    p.add_argument(
        "--json-log",
        action="store_true",
        help="emit structured JSON log lines instead of plain text",
    )
    args = p.parse_args(argv)
    configure_cli_logging(
        quiet=False,
        verbose=False,
        json_log=args.json_log or os.environ.get(JSON_LOG_ENV) == "1",
    )
    config = Config(args.config)
    api_key = args.api_key or os.environ.get(ENV_API_KEY) or config.get("api_key")
    if not api_key:
        logger.error("Error: API key required (set PILLOWS_KEY).")
        return 1
    base = args.base_url or config.get("base_url") or BASE_URL
    return _remote_request(
        "DELETE",
        f"{base}/api/files/{args.file_id}",
        api_key=api_key,
        timeout=args.timeout,
    )
