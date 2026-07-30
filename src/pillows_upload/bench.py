"""``bench`` subcommand: a simple throughput load-test harness."""

# Copyright (c) 2026 edideaur
# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import tempfile
import time
from pathlib import Path

from .config import Config
from .constants import (
    BASE_URL,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CONCURRENCY,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    ENV_API_KEY,
)
from .logutils import JSON_LOG_ENV, configure_cli_logging
from .upload import upload_files

logger = logging.getLogger(__name__)

MB_DIVISOR = 1024 * 1024


def _make_dummy_files(directory: Path, count: int, size: int) -> list[str]:
    """Create ``count`` dummy files of ``size`` bytes under ``directory``."""
    paths: list[str] = []
    chunk = b"\0" * 1024
    for i in range(count):
        path = directory / f"bench_{i:04d}.bin"
        with path.open("wb") as fh:
            remaining = size
            while remaining > 0:
                written = fh.write(chunk[: min(len(chunk), remaining)])
                remaining -= written
        paths.append(str(path))
    return paths


def _cmd_bench(argv: list[str] | None = None) -> int:
    """Run a synthetic throughput benchmark against the configured endpoint."""
    p = argparse.ArgumentParser(
        prog="pillows-upload bench",
        description="Generate dummy files and measure upload throughput.",
    )
    p.add_argument("--count", type=int, default=8, help="number of files to generate")
    p.add_argument("--size", type=int, default=8 * 1024 * 1024, help="bytes per file")
    p.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    p.add_argument("-k", "--api-key", default=None, help="API key (PILLOWS_KEY)")
    p.add_argument("--base-url", default=None, help=f"API base URL (default: {BASE_URL})")
    p.add_argument("-r", "--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--dry-run", action="store_true", help="skip real network transfer")
    p.add_argument("--no-cleanup", action="store_true", help="keep generated files")
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
    if not api_key and not args.dry_run:
        logger.error("Error: API key required for a real benchmark (set PILLOWS_KEY).")
        return 1

    tmp_dir = Path(tempfile.mkdtemp(prefix="pillows-bench-"))
    try:
        paths = _make_dummy_files(tmp_dir, args.count, args.size)
        total_bytes = args.size * args.count
        logger.info(
            "Benchmarking %d files x %.2f MB (%.2f MB total) at concurrency=%d",
            args.count,
            args.size / MB_DIVISOR,
            total_bytes / MB_DIVISOR,
            args.concurrency,
        )

        start = time.time()
        results = upload_files(
            paths,
            api_key=api_key,
            base_url=args.base_url or BASE_URL,
            chunk_size=args.chunk_size,
            concurrency=args.concurrency,
            retries=args.retries,
            timeout=args.timeout,
            dry_run=args.dry_run,
            verbose=args.verbose,
            quiet=args.quiet,
            no_progress=args.quiet,
        )
        elapsed = time.time() - start
    finally:
        if not args.no_cleanup:
            for path in tmp_dir.glob("*.bin"):
                with contextlib.suppress(OSError):
                    path.unlink()
            with contextlib.suppress(OSError):
                tmp_dir.rmdir()

    uploaded = len(results)
    mbps = (total_bytes / MB_DIVISOR) / elapsed if elapsed > 0 else 0.0
    logger.info(
        "Benchmark complete: %d/%d uploaded in %.2fs (%.2f MB/s)",
        uploaded,
        args.count,
        elapsed,
        mbps,
    )
    return 0 if uploaded == args.count else 1
