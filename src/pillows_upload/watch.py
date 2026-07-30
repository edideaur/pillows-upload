"""``watch`` subcommand: daemon that uploads new files as they appear."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import logging
import os
import time

from .config import Config
from .constants import (
    BASE_URL,
    DEFAULT_BACKOFF,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_RETRIES,
    DEFAULT_STATE_FILE,
    DEFAULT_TIMEOUT,
    ENV_API_KEY,
)
from .logutils import JSON_LOG_ENV, configure_cli_logging
from .upload import upload_files

logger = logging.getLogger(__name__)


def _cmd_watch(argv: list[str] | None = None) -> int:
    """Watch a directory and upload new files until interrupted."""
    p = argparse.ArgumentParser(
        prog="pillows-upload watch",
        description="Poll a directory and upload new files as they appear.",
    )
    p.add_argument("path", help="directory to watch")
    p.add_argument("--interval", type=float, default=5.0, help="poll interval (s)")
    p.add_argument("--ext", nargs="*", help="only upload these extensions")
    p.add_argument("--delete-after", action="store_true", help="delete local file after upload")
    p.add_argument("-k", "--api-key", default=None, help="API key (PILLOWS_KEY)")
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {BASE_URL})")
    p.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    p.add_argument("-r", "--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--backoff", type=int, default=DEFAULT_BACKOFF)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--state-file", default=None, help=f"resume state (default: {DEFAULT_STATE_FILE})")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config", default=None)
    p.add_argument(
        "--json-log",
        action="store_true",
        help="emit structured JSON log lines instead of plain text",
    )
    args = p.parse_args(argv)

    configure_cli_logging(
        quiet=args.quiet,
        verbose=args.verbose,
        json_log=args.json_log or os.environ.get(JSON_LOG_ENV) == "1",
    )

    config = Config(args.config)
    api_key = args.api_key or os.environ.get(ENV_API_KEY) or config.get("api_key")
    if not api_key:
        logger.error("Error: API key required. Set PILLOWS_KEY, use -k, or add it to config.")
        return 1

    state_file = args.state_file or config.get("state_file") or DEFAULT_STATE_FILE
    watch_path = args.path
    logger.info("Watching %s every %.1fs (ctrl-c to stop)...", watch_path, args.interval)

    try:
        while True:
            uploaded = upload_files(
                [watch_path],
                api_key=api_key,
                base_url=args.base_url or BASE_URL,
                chunk_size=args.chunk_size,
                concurrency=args.concurrency,
                retries=args.retries,
                backoff=args.backoff,
                timeout=args.timeout,
                extensions=args.ext,
                quiet=True,
                no_progress=True,
                resume=True,
                state_file=state_file,
                delete=args.delete_after,
            )
            if uploaded and not args.quiet:
                logger.info("Uploaded %d new file(s)", len(uploaded))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Stopped watching %s", watch_path)
        return 0
    except Exception:
        logger.exception("Watch loop crashed")
        return 1
